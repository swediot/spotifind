"""A very small, very polite Spotify Web API client.

Small because this tool needs exactly one endpoint: ``GET /v1/search`` with
``type=audiobook``. Polite because the brief was "do not get me banned":

* every call goes through the :class:`~spotifind.ratelimit.RateLimiter`,
* every call is counted against the :class:`~spotifind.budget.DailyBudget`,
  because the limit that actually bites is a daily count, not a rate,
* one request at a time, never concurrent,
* 429 is not retried blindly — the limiter slows the whole run down and stops
  it if Spotify keeps saying no,
* 5xx gets a bounded exponential backoff, and then gives up on that book
  rather than the whole run,
* the User-Agent identifies the tool, so a Spotify engineer looking at logs
  can see what this is.

Note for anyone extending this: as of the February 2026 API changes the
search ``limit`` maxes out at 10 (it used to be 50), the batch
"Get Several Audiobooks" endpoint is gone, and ``available_markets`` has
been removed from objects. Availability is therefore expressed by whether a
result comes back *at all* for the token's market — which is exactly the
question being asked, so nothing is lost.

A second note, learned the hard way: the reference page's list of markets
where audiobooks exist is **out of date**. It still names six (US, UK, CA,
IE, NZ, AU) and has not been updated for the 2024–25 European expansion.
Do not infer availability from documentation — call ``probe`` and look.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Sequence

import httpx

from .matching import Candidate
from .ratelimit import RateLimitAbort, RateLimiter

if TYPE_CHECKING:
    from .budget import DailyBudget

API_BASE = "https://api.spotify.com/v1"

USER_AGENT = (
    "spotifind/1.0 (personal to-read list checker; single-threaded; "
    "0.5 req/s, 600 req/day; +https://github.com/)"
)

# Search's maximum page size since the February 2026 API changes.
MAX_SEARCH_LIMIT = 10

# Undocumented for the new generic library endpoint; this was the limit on the
# per-type save endpoints it replaced, so it is the safest assumption.
MAX_LIBRARY_URIS = 50


class SpotifyError(RuntimeError):
    pass


class Forbidden(SpotifyError):
    """403 — almost always an app-permissions or market problem, not a bug."""


@dataclass
class ClientStats:
    requests: int = 0
    searches: int = 0
    retries_5xx: int = 0
    rate_limited: int = 0
    empty_results: int = 0
    saved: int = 0
    removed: int = 0


class SpotifyClient:
    def __init__(
        self,
        token_provider: Callable[[], str],
        limiter: RateLimiter,
        *,
        market: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
        max_5xx_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        on_throttle: Callable[[float | None], None] | None = None,
        budget: "DailyBudget | None" = None,
    ) -> None:
        self.token_provider = token_provider
        self.limiter = limiter
        # Counts every request across runs and refuses once a day's worth has
        # gone out. None only in tests and in code that never talks to Spotify.
        self.budget = budget
        # With a user token Spotify uses the account's own country and
        # ignores this; it only bites in client-credentials mode.
        self.market = market
        self.max_5xx_retries = max_5xx_retries
        # Told about every 429, so a long silent sleep can say why it is
        # silent instead of looking like a hang.
        self.on_throttle = on_throttle
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.stats = ClientStats()
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SpotifyClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- the one request path ---------------------------------------------

    def _get(self, path: str, params: dict) -> dict:
        return self._request("GET", path, params=params)

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json_body: dict | None = None) -> dict:
        attempt = 0
        refreshed = False
        while True:
            self.limiter.acquire()
            if self.budget is not None:
                # Raises BudgetExhausted rather than send a request the day's
                # quota has no room for. Retries count too: Spotify does.
                self.budget.spend()
            self.stats.requests += 1
            token = self.token_provider()
            try:
                response = self._client.request(
                    method,
                    f"{API_BASE}{path}",
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.RequestError as exc:
                attempt += 1
                if attempt > self.max_5xx_retries:
                    raise SpotifyError(f"Network error talking to Spotify: {exc}") from exc
                self._backoff(attempt)
                continue

            status = response.status_code

            if status in (200, 201, 202, 204):
                self.limiter.note_success()
                if status == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except ValueError:
                    return {}

            if status == 429:
                self.stats.rate_limited += 1
                retry_after = _retry_after(response)
                if self.on_throttle:
                    self.on_throttle(retry_after)
                if self.budget is not None:
                    # A Retry-After of hours has to outlive this run, or the
                    # next one knocks on the same closed door.
                    self.budget.note_retry_after(retry_after)
                # Raises RateLimitAbort once Spotify has said no too often.
                self.limiter.penalise(retry_after)
                continue

            if status == 401:
                # Token expired mid-run. One forced refresh, then try again;
                # a second 401 means the credentials are genuinely wrong.
                if refreshed or not hasattr(self.token_provider, "force_refresh"):
                    raise SpotifyError(
                        "Spotify rejected the access token (401). "
                        "Run `spotifind login` again."
                    )
                refreshed = True
                self.token_provider.force_refresh()
                continue

            if status == 403:
                raise Forbidden(
                    f"Spotify returned 403 for {method} {path}. Usual causes: the "
                    "token is missing the scope this call needs (library writes "
                    "need `user-library-modify` — run `spotifind login --for-saving`), "
                    "or the app is not permitted to use this endpoint. Response: "
                    f"{response.text[:200]}"
                )

            if status == 404:
                return {}

            if 500 <= status < 600:
                attempt += 1
                self.stats.retries_5xx += 1
                if attempt > self.max_5xx_retries:
                    raise SpotifyError(f"Spotify kept returning {status} — giving up on this call.")
                self._backoff(attempt)
                continue

            raise SpotifyError(f"Unexpected {status} from Spotify: {response.text[:300]}")

    def _backoff(self, attempt: int) -> None:
        delay = min(30.0, (2 ** attempt)) + self.rng.uniform(0, 0.5)
        self.sleep(delay)

    # -- the one endpoint --------------------------------------------------

    def search_audiobooks(self, query: str, limit: int = MAX_SEARCH_LIMIT) -> list[Candidate]:
        params = {
            "q": query,
            "type": "audiobook",
            "limit": max(1, min(limit, MAX_SEARCH_LIMIT)),
        }
        if self.market:
            params["market"] = self.market

        payload = self._get("/search", params)
        self.stats.searches += 1
        items = ((payload or {}).get("audiobooks") or {}).get("items") or []
        # Spotify pads pages with nulls for items unavailable in the market.
        candidates = [_to_candidate(item) for item in items if item]
        if not candidates:
            self.stats.empty_results += 1
        return candidates

    # -- writing to the library -------------------------------------------
    #
    # The February 2026 changes replaced the per-type save endpoints with one
    # generic PUT /me/library taking Spotify URIs. The per-request maximum is
    # not documented; MAX_LIBRARY_URIS is set to the old per-type limit, which
    # is the most defensible guess available.

    def library_contains(self, uris: Sequence[str]) -> dict[str, bool]:
        """Which of these are already saved? Avoids pointless writes."""
        uris = [u for u in uris if u]
        if not uris:
            return {}
        payload = self._request(
            "GET", "/me/library/contains", params={"uris": ",".join(uris)}
        )
        # The response is a bare array aligned with the request order.
        flags = payload if isinstance(payload, list) else payload.get("items", [])
        return {uri: bool(flag) for uri, flag in zip(uris, flags)}

    def library_save(self, uris: Sequence[str]) -> None:
        uris = [u for u in uris if u]
        if not uris:
            return
        if len(uris) > MAX_LIBRARY_URIS:
            raise ValueError(f"at most {MAX_LIBRARY_URIS} uris per call")
        # Query parameter, not body: a JSON body gets 400 "Missing required
        # field: uris" from the real API.
        self._request("PUT", "/me/library", params={"uris": ",".join(uris)})
        self.stats.saved += len(uris)

    def library_remove(self, uris: Sequence[str]) -> None:
        uris = [u for u in uris if u]
        if not uris:
            return
        if len(uris) > MAX_LIBRARY_URIS:
            raise ValueError(f"at most {MAX_LIBRARY_URIS} uris per call")
        self._request("DELETE", "/me/library", params={"uris": ",".join(uris)})
        self.stats.removed += len(uris)


def audiobook_uri(spotify_id: str) -> str:
    """Spotify addresses audiobooks as `show` URIs, not `audiobook` ones.

    An audiobook's id lives in the show namespace — an audiobook link is
    open.spotify.com/show/<id> — so the library endpoint wants
    `spotify:show:<id>`. Getting this wrong is a silent no-op, not an error.
    """
    spotify_id = (spotify_id or "").strip()
    if not spotify_id:
        return ""
    if spotify_id.startswith("spotify:"):
        return spotify_id
    if "open.spotify.com" in spotify_id:
        tail = spotify_id.rstrip("/").split("/")[-1].split("?")[0]
        return f"spotify:show:{tail}"
    return f"spotify:show:{spotify_id}"


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _names(items) -> list[str]:
    out = []
    for item in items or []:
        if isinstance(item, dict):
            name = item.get("name")
        else:
            name = item
        if name:
            out.append(str(name))
    return out


def _to_candidate(item: dict) -> Candidate:
    return Candidate(
        id=item.get("id", ""),
        name=item.get("name", ""),
        authors=_names(item.get("authors")),
        narrators=_names(item.get("narrators")),
        publisher=item.get("publisher", "") or "",
        edition=item.get("edition", "") or "",
        total_chapters=item.get("total_chapters"),
        url=(item.get("external_urls") or {}).get("spotify", ""),
        description=(item.get("description") or "")[:500],
        explicit=bool(item.get("explicit")),
        languages=[str(code) for code in (item.get("languages") or []) if code],
        raw=item,
    )


__all__ = [
    "SpotifyClient",
    "SpotifyError",
    "Forbidden",
    "RateLimitAbort",
    "MAX_SEARCH_LIMIT",
    "USER_AGENT",
]
