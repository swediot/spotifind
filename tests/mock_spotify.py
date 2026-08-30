"""A stand-in Spotify, good enough to test everything except Spotify itself.

The sandbox this was built in cannot reach api.spotify.com, so every claim
the tests make is about *our* behaviour: matching, caching, and above all
what the client does when Spotify pushes back. The response shapes come from
the Web API reference (search, audiobook object) as of the February 2026
changes — search limit 10, no ``available_markets`` field.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import httpx


@dataclass
class MockBook:
    name: str
    authors: list[str]
    narrators: list[str] = field(default_factory=lambda: ["A Narrator"])
    markets: tuple[str, ...] = ("US", "GB", "CA", "IE", "NZ", "AU",
                                "DE", "AT", "CH", "LI", "FR", "BE", "NL", "LU")
    edition: str = "Unabridged"
    chapters: int = 30
    languages: tuple[str, ...] = ("en",)

    def to_json(self, index: int) -> dict:
        # Editions differ by language and narrator, so the id must too —
        # otherwise two real editions look like one row to any code that
        # de-duplicates by Spotify id.
        seed = self.name + "".join(self.languages) + "".join(self.narrators)
        slug = re.sub(r"[^a-z0-9]+", "", seed.lower())[:22].ljust(22, "x")
        return {
            "id": slug,
            "name": self.name,
            "authors": [{"name": a} for a in self.authors],
            "narrators": [{"name": n} for n in self.narrators],
            "languages": list(self.languages),
            "edition": self.edition,
            "explicit": False,
            "total_chapters": self.chapters,
            "description": f"{self.name} by {', '.join(self.authors)}.",
            "external_urls": {"spotify": f"https://open.spotify.com/show/{slug}"},
            "type": "audiobook",
            "uri": f"spotify:show:{slug}",
        }


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 1}


class MockSpotify:
    """Scriptable transport.

    ``script`` is a list of behaviours consumed one request at a time; when it
    runs out, the catalogue answers normally. Each entry is either an int
    status code, or ``(status, headers)``.
    """

    def __init__(
        self,
        catalogue: list[MockBook] | None = None,
        *,
        market: str = "US",
        script: list | None = None,
        limit_cap: int = 10,
    ) -> None:
        self.catalogue = catalogue or []
        self.market = market
        self.script = list(script or [])
        self.limit_cap = limit_cap
        self.requests: list[httpx.Request] = []
        self.queries: list[str] = []
        self.max_concurrent = 0
        self._in_flight = 0

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- the handler -------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self._in_flight += 1
        self.max_concurrent = max(self.max_concurrent, self._in_flight)
        try:
            self.requests.append(request)

            if self.script:
                entry = self.script.pop(0)
                status, headers = entry if isinstance(entry, tuple) else (entry, {})
                if status != 200:
                    return httpx.Response(status, headers=headers,
                                          json={"error": {"status": status, "message": "scripted"}})

            if not request.url.path.endswith("/search"):
                return httpx.Response(404, json={"error": {"status": 404}})

            params = request.url.params
            query = params.get("q", "")
            self.queries.append(query)
            limit = int(params.get("limit", 5))
            assert limit <= self.limit_cap, f"search limit {limit} exceeds the API maximum"
            assert "audiobook" in params.get("type", ""), "type must ask for audiobooks"
            market = params.get("market") or self.market

            wanted = _tokens(query)
            hits = []
            for book in self.catalogue:
                if market not in book.markets:
                    continue
                hay = _tokens(book.name + " " + " ".join(book.authors))
                # Spotify's search is loose; anything with real overlap comes back.
                overlap = len(wanted & hay)
                if overlap and overlap / max(1, len(wanted)) >= 0.4:
                    hits.append((overlap, book))
            hits.sort(key=lambda pair: -pair[0])
            items = [book.to_json(i) for i, (_, book) in enumerate(hits[:limit])]

            return httpx.Response(200, json={
                "audiobooks": {
                    "href": str(request.url),
                    "items": items,
                    "limit": limit,
                    "offset": 0,
                    "total": len(hits),
                    "next": None,
                    "previous": None,
                }
            })
        finally:
            self._in_flight -= 1


def response_json(response: httpx.Response) -> dict:
    return json.loads(response.content.decode())
