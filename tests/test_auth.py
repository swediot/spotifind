"""Sign-in plumbing — the paste fallback especially.

The browser-opening path can't be tested here, but the thing that rescues a
login when it fails absolutely can be: parsing the URL out of an address bar.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from spotifind import auth  # noqa: E402


# -- parsing a pasted redirect --------------------------------------------

def test_a_whole_redirect_url_parses():
    parsed = auth.parse_redirect(
        "http://127.0.0.1:8888/callback?code=AQD123&state=abc"
    )
    assert parsed == {"code": "AQD123", "state": "abc"}


def test_a_pasted_url_with_surrounding_junk_still_parses():
    """People paste with quotes, spaces and newlines. Cope."""
    for raw in (
        '  http://127.0.0.1:8888/callback?code=AQD123&state=abc  ',
        '"http://127.0.0.1:8888/callback?code=AQD123&state=abc"',
        "'http://127.0.0.1:8888/callback?code=AQD123&state=abc'\n",
    ):
        assert auth.parse_redirect(raw)["code"] == "AQD123"


def test_just_the_query_string_works_too():
    assert auth.parse_redirect("?code=AQD123&state=abc")["code"] == "AQD123"
    assert auth.parse_redirect("code=AQD123&state=abc")["code"] == "AQD123"


def test_an_error_redirect_is_parsed_not_swallowed():
    parsed = auth.parse_redirect(
        "http://127.0.0.1:8888/callback?error=access_denied&state=abc"
    )
    assert parsed["error"] == "access_denied"
    assert "code" not in parsed


def test_url_encoded_values_are_decoded():
    parsed = auth.parse_redirect("http://127.0.0.1:8888/callback?code=a%2Fb%2Bc&state=x")
    assert parsed["code"] == "a/b+c"


def test_empty_input_is_empty_not_an_exception():
    assert auth.parse_redirect("") == {}
    assert auth.parse_redirect("   ") == {}
    assert auth.parse_redirect(None) == {}


# -- the authorise flow, without a browser --------------------------------

def test_manual_login_completes_from_a_pasted_url(monkeypatch, tmp_path, capsys):
    """The whole point: no callback server, no browser, still signs in."""
    captured = {}

    def fake_input(_prompt):
        # Echo back a redirect carrying the state the flow just generated.
        state = captured["state"]
        return f"http://127.0.0.1:8888/callback?code=THECODE&state={state}"

    def fake_post(self, url, data=None, **kwargs):
        captured["exchange"] = data
        return _FakeResponse(200, {
            "access_token": "at", "refresh_token": "rt", "expires_in": 3600,
        })

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("httpx.Client.post", fake_post)
    monkeypatch.setattr(auth, "open_in_browser", lambda url: False)
    monkeypatch.setattr(auth, "copy_to_clipboard", lambda text: False)

    real_pair = auth._pkce_pair

    def spy_pair():
        verifier, challenge = real_pair()
        captured["verifier"] = verifier
        return verifier, challenge

    monkeypatch.setattr(auth, "_pkce_pair", spy_pair)

    # The state is generated inside authorise_user, so capture it off the
    # printed URL rather than reaching into the function.
    import urllib.parse

    class _Capture:
        def __init__(self):
            self.lines = []

        def __call__(self, *args, **kwargs):
            text = " ".join(str(a) for a in args)
            self.lines.append(text)
            if "authorize?" in text:
                query = urllib.parse.urlparse(text.strip()).query
                captured["state"] = urllib.parse.parse_qs(query)["state"][0]

    printer = _Capture()
    monkeypatch.setattr("builtins.print", printer)

    token_path = tmp_path / "token.json"
    token = auth.authorise_user("client123", token_path=token_path, manual=True)

    assert token.access_token == "at"
    assert token.refresh_token == "rt"
    assert captured["exchange"]["code"] == "THECODE"
    assert captured["exchange"]["code_verifier"] == captured["verifier"]
    assert json.loads(token_path.read_text())["refresh_token"] == "rt"


def test_the_saved_token_file_is_not_world_readable(tmp_path):
    auth._save(tmp_path / "token.json", {"refresh_token": "secret"})
    mode = os.stat(tmp_path / "token.json").st_mode & 0o777
    assert mode == 0o600


def test_a_state_mismatch_is_refused(monkeypatch, tmp_path):
    """A code from an older login attempt must not be accepted."""
    monkeypatch.setattr("builtins.input",
                        lambda _p: "http://127.0.0.1:8888/callback?code=X&state=STALE")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(auth, "open_in_browser", lambda url: False)
    monkeypatch.setattr(auth, "copy_to_clipboard", lambda text: False)

    with pytest.raises(auth.AuthError, match="State mismatch"):
        auth.authorise_user("client123", token_path=tmp_path / "t.json", manual=True)


def test_giving_up_at_the_prompt_explains_the_manual_flag(monkeypatch, tmp_path):
    monkeypatch.setattr("builtins.input", lambda _p: "")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(auth, "open_in_browser", lambda url: False)
    monkeypatch.setattr(auth, "copy_to_clipboard", lambda text: False)

    with pytest.raises(auth.AuthError, match="--manual"):
        auth.authorise_user("client123", token_path=tmp_path / "t.json", manual=True)


def test_a_denied_authorisation_says_what_spotify_said(monkeypatch, tmp_path):
    state_holder = {}

    def fake_input(_prompt):
        return f"http://127.0.0.1:8888/callback?error=access_denied&state={state_holder['s']}"

    import urllib.parse

    def fake_print(*args, **kwargs):
        text = " ".join(str(a) for a in args)
        if "authorize?" in text:
            state_holder["s"] = urllib.parse.parse_qs(
                urllib.parse.urlparse(text.strip()).query)["state"][0]

    monkeypatch.setattr("builtins.print", fake_print)
    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(auth, "open_in_browser", lambda url: False)
    monkeypatch.setattr(auth, "copy_to_clipboard", lambda text: False)

    with pytest.raises(auth.AuthError, match="access_denied"):
        auth.authorise_user("client123", token_path=tmp_path / "t.json", manual=True)


def test_the_authorize_url_carries_pkce_and_no_scope(monkeypatch, tmp_path):
    """No scopes are requested: catalogue search needs none."""
    seen = {}
    import urllib.parse

    def fake_print(*args, **kwargs):
        text = " ".join(str(a) for a in args)
        if "authorize?" in text:
            seen.update(urllib.parse.parse_qs(
                urllib.parse.urlparse(text.strip()).query))

    monkeypatch.setattr("builtins.print", fake_print)
    monkeypatch.setattr("builtins.input", lambda _p: "")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(auth, "open_in_browser", lambda url: False)
    monkeypatch.setattr(auth, "copy_to_clipboard", lambda text: False)

    with pytest.raises(auth.AuthError):
        auth.authorise_user("client123", token_path=tmp_path / "t.json", manual=True)

    assert seen["code_challenge_method"] == ["S256"]
    assert seen["client_id"] == ["client123"]
    assert seen["redirect_uri"] == ["http://127.0.0.1:8888/callback"]
    assert "scope" not in seen
    # 43 characters is a SHA-256 digest in base64url without padding.
    assert len(seen["code_challenge"][0]) == 43


def test_a_network_failure_is_a_sentence_not_a_traceback(monkeypatch, tmp_path):
    """A dropped connection mid-exchange must not print forty lines of httpx."""
    import httpx

    state_holder = {}
    import urllib.parse

    def fake_print(*args, **kwargs):
        text = " ".join(str(a) for a in args)
        if "authorize?" in text:
            state_holder["s"] = urllib.parse.parse_qs(
                urllib.parse.urlparse(text.strip()).query)["state"][0]

    def boom(self, url, **kwargs):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr("builtins.print", fake_print)
    monkeypatch.setattr("builtins.input",
                        lambda _p: f"http://127.0.0.1:8888/callback?code=C&state={state_holder['s']}")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("httpx.Client.post", boom)
    monkeypatch.setattr(auth, "open_in_browser", lambda url: False)
    monkeypatch.setattr(auth, "copy_to_clipboard", lambda text: False)

    with pytest.raises(auth.AuthError, match="could not reach accounts.spotify.com"):
        auth.authorise_user("client123", token_path=tmp_path / "t.json", manual=True)


def test_a_refresh_network_failure_is_also_handled(monkeypatch):
    import httpx

    def boom(self, url, **kwargs):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr("httpx.Client.post", boom)
    with pytest.raises(auth.AuthError, match="Token refresh could not reach"):
        auth.refresh_user_token("cid", "rt")


def test_a_redirect_uri_mismatch_gets_a_hint(monkeypatch, tmp_path):
    """The single most common setup mistake deserves a pointer."""
    state_holder = {}
    import urllib.parse

    def fake_print(*args, **kwargs):
        text = " ".join(str(a) for a in args)
        if "authorize?" in text:
            state_holder["s"] = urllib.parse.parse_qs(
                urllib.parse.urlparse(text.strip()).query)["state"][0]

    def fake_post(self, url, data=None, **kwargs):
        return _FakeResponse(400, {"error": "invalid_grant",
                                   "error_description": "Invalid redirect_uri"})

    monkeypatch.setattr("builtins.print", fake_print)
    monkeypatch.setattr("builtins.input",
                        lambda _p: f"http://127.0.0.1:8888/callback?code=C&state={state_holder['s']}")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("httpx.Client.post", fake_post)
    monkeypatch.setattr(auth, "open_in_browser", lambda url: False)
    monkeypatch.setattr(auth, "copy_to_clipboard", lambda text: False)

    with pytest.raises(auth.AuthError, match="match the dashboard exactly"):
        auth.authorise_user("client123", token_path=tmp_path / "t.json", manual=True)


def test_the_default_redirect_is_a_loopback_ip_not_localhost():
    """Spotify rejects `localhost`; only literal loopback IPs may use http."""
    assert auth.DEFAULT_REDIRECT.startswith("http://127.0.0.1:")
    assert "localhost" not in auth.DEFAULT_REDIRECT


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload
