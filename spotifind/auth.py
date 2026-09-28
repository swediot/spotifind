"""Getting a token, without asking you to paste a client secret anywhere.

Two flows:

* **user** (default) — Authorization Code with PKCE. No client secret, so
  nothing long-lived and dangerous ends up in a config file; the refresh
  token is written to ``~/.config/spotifind/token.json`` with mode 0600.
  A user token means Spotify answers with *your* account's market, which is
  the whole point: the question is what you can actually play.

* **app** — Client Credentials. Needs a client secret, cannot see your
  account, but *can* be pointed at a market you are not in. Useful for
  answering "is this in the US catalogue at all", which a user token cannot
  do: with a user token, the account's country overrides the market
  parameter.

No scopes are requested. Searching the catalogue needs none, and asking for
permissions a tool does not use is bad manners.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Sequence

import httpx

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

# Spotify requires HTTPS redirect URIs, with an explicit exception for
# loopback *IP literals*. "localhost" is rejected; "127.0.0.1" is not.
DEFAULT_REDIRECT = "http://127.0.0.1:8888/callback"

DEFAULT_TOKEN_PATH = Path(
    os.environ.get("SPOTIFIND_TOKEN_PATH")
    or (Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "spotifind" / "token.json")
)


class AuthError(RuntimeError):
    pass


@dataclass
class Token:
    access_token: str
    expires_at: float
    refresh_token: str = ""
    scopes: list[str] = field(default_factory=list)

    @property
    def expired(self) -> bool:
        # 60s of slack so a long request cannot straddle the expiry.
        return time.time() >= self.expires_at - 60


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------
# PKCE user flow
# --------------------------------------------------------------------------

def _post_token(data: dict, *, headers: dict | None = None, what: str = "Request") -> httpx.Response:
    """POST to the token endpoint, turning network failures into AuthError.

    Without this, a flaky connection at exactly the wrong moment greets you
    with forty lines of httpx traceback instead of a sentence.
    """
    try:
        with httpx.Client(timeout=30) as client:
            return client.post(TOKEN_URL, data=data, headers=headers or {})
    except httpx.RequestError as exc:
        raise AuthError(
            f"{what} could not reach accounts.spotify.com ({exc.__class__.__name__}). "
            "Check your internet connection and try again."
        ) from exc


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.result = {k: v[0] for k, v in params.items()}
        ok = "code" in params
        body = (
            "<html><body style='font:16px/1.5 system-ui;padding:3rem'>"
            + ("<h1>Signed in.</h1><p>You can close this tab and go back to the terminal.</p>"
               if ok else
               f"<h1>Sign-in failed.</h1><pre>{params.get('error', ['unknown'])[0]}</pre>")
            + "</body></html>"
        )
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):  # silence the default stderr logging
        return


def open_in_browser(url: str) -> bool:
    """Try hard to open a browser. Returns whether anything worked.

    `webbrowser.open` can fail silently — notably from a conda Python on
    macOS — and when it does, a login that is merely *waiting* looks like a
    login that has hung. So fall back to the platform opener, and be honest
    in the return value rather than assuming it worked.
    """
    try:
        if webbrowser.open(url):
            return True
    except Exception:
        pass

    opener = {"darwin": "open", "win32": "start"}.get(sys.platform, "xdg-open")
    try:
        completed = subprocess.run(
            [opener, url],
            check=False, shell=(opener == "start"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def copy_to_clipboard(text: str) -> bool:
    """Put the URL on the clipboard, so nobody has to retype 200 characters."""
    commands = {
        "darwin": ["pbcopy"],
        "win32": ["clip"],
    }.get(sys.platform, ["xclip", "-selection", "clipboard"])
    try:
        proc = subprocess.run(commands, input=text.encode(), timeout=10,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def parse_redirect(pasted: str) -> dict:
    """Pull the query parameters out of a redirect URL the user pasted.

    The browser lands on 127.0.0.1 and — if the little local server is not
    listening — shows "can't connect". The address bar still holds the
    authorisation code, so pasting that URL is a complete substitute for the
    callback ever arriving.
    """
    pasted = (pasted or "").strip().strip('"').strip("'")
    if not pasted:
        return {}
    query = urllib.parse.urlparse(pasted).query or pasted.split("?", 1)[-1]
    return {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}


def _prompt_for_redirect(state: str) -> dict:
    """Last resort: ask for the URL the browser ended up on."""
    if not sys.stdin.isatty():
        return {}
    print(
        "\nThe browser never came back to this script.\n"
        "That is fine — the address bar has what we need.\n\n"
        "In the browser, copy the WHOLE address of the page you landed on\n"
        "(it starts http://127.0.0.1:8888/callback?code=... and the page itself\n"
        "may well say it cannot connect — that does not matter), and paste it\n"
        "here. Press Enter on an empty line to give up.\n"
    )
    try:
        pasted = input("Redirect URL: ")
    except (EOFError, KeyboardInterrupt):
        return {}
    return parse_redirect(pasted)


def authorise_user(
    client_id: str,
    *,
    redirect_uri: str = DEFAULT_REDIRECT,
    token_path: Path = DEFAULT_TOKEN_PATH,
    open_browser: bool = True,
    timeout: float = 300.0,
    manual: bool = False,
    scopes: Sequence[str] = (),
) -> Token:
    """Run the browser sign-in once and persist the refresh token."""
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    # Searching the catalogue needs no scope at all; writing to the library
    # does. Ask for nothing you are not about to use — a consent screen that
    # requests library access for a read-only tool is a bad look and a bad
    # habit.
    if scopes:
        params["scope"] = " ".join(scopes)
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    server = None
    listening = False
    if not manual:
        parsed = urllib.parse.urlparse(redirect_uri)
        _CallbackHandler.result = {}
        try:
            server = HTTPServer(
                (parsed.hostname or "127.0.0.1", parsed.port or 80), _CallbackHandler
            )
            threading.Thread(target=server.serve_forever, daemon=True).start()
            listening = True
        except OSError as exc:
            # Port in use, or blocked. Not fatal — the paste path still works.
            print(f"\nCould not listen on {redirect_uri} ({exc}).\n"
                  "Falling back to pasting the redirect URL by hand.")

    opened = open_in_browser(url) if open_browser else False
    copied = copy_to_clipboard(url)

    print()
    if opened:
        print("A browser should have opened. Approve the request there.")
    else:
        print("Could not open a browser for you.")
    print("If it did not open, use this URL" + (" (already on your clipboard, just paste it)"
                                                if copied else "") + ":")
    # Flush left and alone on its line: an indented URL is harder to select in
    # a terminal, and this one wraps over several lines at 80 columns.
    print()
    print(url)
    print()
    if listening:
        print(f"Waiting up to {int(timeout / 60)} minutes for you to approve it… "
              "(Ctrl-C to stop)")

    result: dict = {}
    if listening:
        deadline = time.time() + timeout
        try:
            while not _CallbackHandler.result and time.time() < deadline:
                time.sleep(0.2)
            result = dict(_CallbackHandler.result)
        finally:
            server.shutdown()
            server.server_close()

    if not result:
        result = _prompt_for_redirect(state)

    if not result:
        raise AuthError(
            "No authorisation code received. Run `spotifind login --manual` to "
            "skip the local callback server and paste the redirect URL instead."
        )
    if result.get("state") != state:
        raise AuthError(
            "State mismatch on the Spotify redirect — aborting. (If you pasted a "
            "URL from an earlier login attempt, run `spotifind login` again and "
            "use the fresh one.)"
        )
    if "code" not in result:
        raise AuthError(f"Spotify refused the sign-in: {result.get('error', 'unknown')}")

    response = _post_token(
        {
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
        what="Token exchange",
    )
    if response.status_code != 200:
        hint = ""
        if response.status_code == 400 and "redirect_uri" in response.text:
            hint = ("\nThe redirect URI must match the dashboard exactly, including "
                    "the /callback path.")
        raise AuthError(
            f"Token exchange failed ({response.status_code}): "
            f"{response.text[:300]}{hint}"
        )

    payload = response.json()
    # Spotify reports what it actually granted, which can differ from what was
    # asked for. Record that, not the request.
    granted = (payload.get("scope") or " ".join(scopes)).split()
    token = Token(
        access_token=payload["access_token"],
        expires_at=time.time() + float(payload.get("expires_in", 3600)),
        refresh_token=payload.get("refresh_token", ""),
        scopes=granted,
    )
    _save(token_path, {
        "client_id": client_id,
        "refresh_token": token.refresh_token,
        "scopes": granted,
    })
    return token


def refresh_user_token(client_id: str, refresh_token: str, token_path: Path = DEFAULT_TOKEN_PATH) -> Token:
    response = _post_token(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
        what="Token refresh",
    )
    if response.status_code != 200:
        raise AuthError(
            f"Could not refresh the Spotify token ({response.status_code}). "
            "Run `spotifind login` to sign in again."
        )
    payload = response.json()
    # Spotify may or may not hand back a new refresh token; keep the old one
    # if it does not.
    new_refresh = payload.get("refresh_token") or refresh_token
    if new_refresh != refresh_token:
        _save(token_path, {"client_id": client_id, "refresh_token": new_refresh})
    return Token(
        access_token=payload["access_token"],
        expires_at=time.time() + float(payload.get("expires_in", 3600)),
        refresh_token=new_refresh,
    )


def stored_client_id(token_path: Path = DEFAULT_TOKEN_PATH) -> str:
    """The client id recorded by ``spotifind login``, or "" if there is none."""
    stored = _load(token_path)
    return str((stored or {}).get("client_id") or "")


def load_user_token(
    client_id: str,
    token_path: Path = DEFAULT_TOKEN_PATH,
    *,
    required_scopes: Sequence[str] = (),
) -> Token:
    stored = _load(token_path)
    if not stored or not stored.get("refresh_token"):
        raise AuthError(
            f"No saved Spotify sign-in at {token_path}. Run `spotifind login` first."
        )
    if stored.get("client_id") and stored["client_id"] != client_id:
        raise AuthError(
            "The saved sign-in belongs to a different Spotify app. "
            "Run `spotifind login` again with this client id."
        )

    # Catch a missing scope here, before a run gets half way and dies on a
    # 403. A token's scopes are fixed at sign-in; refreshing never adds one.
    missing = [s for s in required_scopes if s not in (stored.get("scopes") or [])]
    if missing:
        raise AuthError(
            "Your saved Spotify sign-in does not grant "
            + ", ".join(missing)
            + ".\nThat permission is decided when you sign in, so it needs a new "
            "sign-in:\n\n    spotifind login --for-saving\n"
        )

    token = refresh_user_token(client_id, stored["refresh_token"], token_path)
    if not token.scopes:
        token.scopes = list(stored.get("scopes") or [])
    return token


# --------------------------------------------------------------------------
# Client-credentials app flow
# --------------------------------------------------------------------------

class TokenSource:
    """Hands out an access token, refreshing it when it goes stale.

    Callable so the client can just do ``self.token_provider()``; it also
    exposes ``force_refresh()`` for the one case the client cannot infer —
    a 401 on a token we still believed was live.
    """

    def __init__(self, fetch: "callable", *, label: str = "") -> None:
        self._fetch = fetch
        self._token: Token | None = None
        self.label = label

    def __call__(self) -> str:
        if self._token is None or self._token.expired:
            self._token = self._fetch()
        return self._token.access_token

    def force_refresh(self) -> str:
        self._token = self._fetch()
        return self._token.access_token


#: Everything spotifind ever asks for. `check` and `probe` ask for nothing.
SAVE_SCOPES = ("user-library-modify", "user-library-read")


def user_token_source(
    client_id: str,
    token_path: Path = DEFAULT_TOKEN_PATH,
    *,
    required_scopes: Sequence[str] = (),
) -> TokenSource:
    return TokenSource(
        lambda: load_user_token(client_id, token_path, required_scopes=required_scopes),
        label="user",
    )


def app_token_source(client_id: str, client_secret: str) -> TokenSource:
    return TokenSource(lambda: app_token(client_id, client_secret), label="app")


def static_token_source(access_token: str) -> TokenSource:
    return TokenSource(
        lambda: Token(access_token=access_token, expires_at=time.time() + 3600),
        label="static",
    )


def app_token(client_id: str, client_secret: str) -> Token:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    response = _post_token(
        {"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {basic}"},
        what="Client-credentials token",
    )
    if response.status_code != 200:
        raise AuthError(f"Client-credentials token failed ({response.status_code}): {response.text[:300]}")
    payload = response.json()
    return Token(
        access_token=payload["access_token"],
        expires_at=time.time() + float(payload.get("expires_in", 3600)),
    )
