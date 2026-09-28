"""Shared test setup."""

from __future__ import annotations

import pytest

from spotifind import auth, cli


@pytest.fixture(autouse=True)
def _no_real_user_files(tmp_path, monkeypatch):
    """Never let a test open the real cache or token.

    Commands without an explicit --cache or --token-path fall back to the
    user's own files under ~/.local/share and ~/.config. A test that reached
    them would migrate a real database, or spend a real daily budget.
    """
    monkeypatch.setattr(cli, "DEFAULT_CACHE", tmp_path / "default-cache.sqlite3")
    monkeypatch.setattr(auth, "DEFAULT_TOKEN_PATH", tmp_path / "default-token.json")
