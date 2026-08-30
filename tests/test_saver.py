"""Writing to the library — the only part of this tool that changes anything.

The bias throughout: never save something the user didn't see listed, never
save twice, and always be able to take it back out.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from spotifind import saver  # noqa: E402
from spotifind.auth import static_token_source  # noqa: E402
from spotifind.cache import Cache  # noqa: E402
from spotifind.ratelimit import FakeClock, LimiterConfig, RateLimiter  # noqa: E402
from spotifind.spotify import (  # noqa: E402
    Forbidden,
    SpotifyClient,
    audiobook_uri,
)

HEADER = ("title,authors,status,confidence,spotify_title,spotify_authors,narrators,"
          "language,edition,chapters,spotify_id,spotify_url,other_editions,"
          "other_languages,title_score,author_matched,newly_found,from_cache,"
          "date_added,error\n")


def row(title, authors, status, spotify_id):
    url = f"https://open.spotify.com/show/{spotify_id}" if spotify_id else ""
    return (f"{title},{authors},{status},strong,{title},{authors},N,en,Unabridged,"
            f"30,{spotify_id},{url},,,1.000,yes,,,,\n")


@pytest.fixture
def report(tmp_path):
    path = tmp_path / "spotify-availability.csv"
    path.write_text(HEADER + "".join([
        row("Piranesi", "Susanna Clarke", "on_spotify", "aaa111"),
        row("Project Hail Mary", "Andy Weir", "on_spotify", "bbb222"),
        row("Circe", "Madeline Miller", "check", "ccc333"),
        row("Babel", "R.F. Kuang", "probably", "ddd444"),
        row("The Overstory", "Richard Powers", "not_found", ""),
    ]), encoding="utf-8")
    return path


class LibraryMock:
    """Records what was saved and removed; can be told to already hold some."""

    def __init__(self, already: set[str] | None = None, script: list | None = None):
        self.already = set(already or ())
        self.saved: list[str] = []
        self.removed: list[str] = []
        self.contains_calls = 0
        self.save_calls = 0
        self.script = list(script or [])

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.script:
            status = self.script.pop(0)
            if status != 200:
                return httpx.Response(status, json={"error": {"status": status}})

        path = request.url.path
        if path.endswith("/me/library/contains"):
            self.contains_calls += 1
            uris = request.url.params.get("uris", "").split(",")
            return httpx.Response(200, json=[u in self.already for u in uris])

        if path.endswith("/me/library"):
            body = json.loads(request.content.decode() or "{}")
            uris = body.get("uris", [])
            if request.method == "PUT":
                self.save_calls += 1
                self.saved.extend(uris)
                self.already |= set(uris)
                return httpx.Response(200, json={})
            if request.method == "DELETE":
                self.removed.extend(uris)
                self.already -= set(uris)
                return httpx.Response(200, json={})

        return httpx.Response(404, json={"error": {"status": 404}})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def make_client(mock: LibraryMock) -> SpotifyClient:
    clock = FakeClock()
    limiter = RateLimiter(config=LimiterConfig(), clock=clock.time,
                          sleep=clock.sleep, rng=random.Random(0))
    return SpotifyClient(static_token_source("t"), limiter,
                         transport=mock.transport, sleep=clock.sleep)


# -- URIs ------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("aaa111", "spotify:show:aaa111"),
    ("https://open.spotify.com/show/aaa111", "spotify:show:aaa111"),
    ("https://open.spotify.com/show/aaa111?si=xyz", "spotify:show:aaa111"),
    ("spotify:show:aaa111", "spotify:show:aaa111"),
    ("", ""),
])
def test_audiobook_uri(raw, expected):
    assert audiobook_uri(raw) == expected


def test_audiobooks_use_the_show_namespace():
    """An audiobook id is a show id; `spotify:audiobook:` would silently fail."""
    assert audiobook_uri("aaa111").startswith("spotify:show:")


# -- reading the report ----------------------------------------------------

def test_only_confirmed_rows_by_default(report):
    items = saver.read_report(report)
    assert [i.title for i in items] == ["Piranesi", "Project Hail Mary"]


def test_widening_the_selection(report):
    items = saver.read_report(report, include=("on_spotify", "probably", "check"))
    assert len(items) == 4


def test_rows_with_no_match_are_never_savable(report):
    items = saver.read_report(report, include=("on_spotify", "probably", "check", "not_found"))
    assert all(i.uri for i in items)
    assert "The Overstory" not in {i.title for i in items}


def test_duplicate_ids_are_collapsed(tmp_path):
    path = tmp_path / "r.csv"
    path.write_text(HEADER + row("A", "X", "on_spotify", "same1")
                    + row("A (again)", "X", "on_spotify", "same1"), encoding="utf-8")
    assert len(saver.read_report(path)) == 1


def test_a_missing_report_says_run_check(tmp_path):
    with pytest.raises(saver.ReportError, match="Run `spotifind check`"):
        saver.read_report(tmp_path / "nope.csv")


def test_the_wrong_kind_of_csv_is_refused(tmp_path):
    path = tmp_path / "wrong.csv"
    path.write_text("Title,Authors\nPiranesi,Susanna Clarke\n", encoding="utf-8")
    with pytest.raises(saver.ReportError, match="does not look like a spotifind report"):
        saver.read_report(path)


def test_an_old_report_without_ids_is_refused(tmp_path):
    path = tmp_path / "old.csv"
    path.write_text("title,authors,status\nPiranesi,Susanna Clarke,on_spotify\n",
                    encoding="utf-8")
    with pytest.raises(saver.ReportError, match="older version"):
        saver.read_report(path)


# -- saving ----------------------------------------------------------------

def test_saving_sends_show_uris(report, tmp_path):
    mock = LibraryMock()
    items = saver.read_report(report)
    with make_client(mock) as client, Cache(tmp_path / "c.db") as cache:
        summary = saver.save_to_library(items, client, cache)
    assert mock.saved == ["spotify:show:aaa111", "spotify:show:bbb222"]
    assert summary.saved_count == 2
    assert summary.status == "ok"


def test_books_already_in_your_library_are_skipped(report, tmp_path):
    mock = LibraryMock(already={"spotify:show:aaa111"})
    items = saver.read_report(report)
    with make_client(mock) as client, Cache(tmp_path / "c.db") as cache:
        summary = saver.save_to_library(items, client, cache)
    assert mock.saved == ["spotify:show:bbb222"]
    assert [s.title for s in summary.already_there] == ["Piranesi"]


def test_a_second_save_run_sends_nothing(report, tmp_path):
    mock = LibraryMock()
    items = saver.read_report(report)
    with Cache(tmp_path / "c.db") as cache:
        with make_client(mock) as client:
            saver.save_to_library(items, client, cache)
        first = len(mock.saved)
        with make_client(mock) as client:
            second = saver.save_to_library(items, client, cache)
    assert len(mock.saved) == first, "already-saved books must not be re-sent"
    assert second.saved_count == 0
    assert len(second.already_there) == 2


def test_no_skip_sends_everything_without_checking(report, tmp_path):
    mock = LibraryMock(already={"spotify:show:aaa111"})
    items = saver.read_report(report)
    with make_client(mock) as client, Cache(tmp_path / "c.db") as cache:
        saver.save_to_library(items, client, cache, skip_existing=False)
    assert mock.contains_calls == 0
    assert len(mock.saved) == 2


def test_saving_is_batched(tmp_path):
    many = [saver.Savable(f"Book {i}", "A", f"spotify:show:id{i}", "on_spotify")
            for i in range(45)]
    mock = LibraryMock()
    with make_client(mock) as client, Cache(tmp_path / "c.db") as cache:
        saver.save_to_library(many, client, cache, chunk_size=20)
    assert mock.save_calls == 3, "45 books in chunks of 20 is three requests"
    assert len(mock.saved) == 45


def test_the_chunk_size_cannot_exceed_the_api_maximum(tmp_path):
    many = [saver.Savable(f"B{i}", "A", f"spotify:show:i{i}", "on_spotify")
            for i in range(60)]
    mock = LibraryMock()
    with make_client(mock) as client, Cache(tmp_path / "c.db") as cache:
        saver.save_to_library(many, client, cache, chunk_size=9999)
    assert mock.save_calls == 2  # clamped to 50 per request
    assert len(mock.saved) == 60


def test_a_403_stops_and_explains_the_scope(report, tmp_path):
    """The predictable failure: signed in before --for-saving existed."""
    mock = LibraryMock(script=[403])
    items = saver.read_report(report)
    with make_client(mock) as client, Cache(tmp_path / "c.db") as cache:
        summary = saver.save_to_library(items, client, cache)
    assert summary.status == "failed"
    assert "user-library-modify" in summary.note
    assert summary.saved_count == 0


def test_a_partial_failure_keeps_what_already_succeeded(tmp_path):
    many = [saver.Savable(f"B{i}", "A", f"spotify:show:i{i}", "on_spotify")
            for i in range(40)]
    # First chunk's contains + save succeed, then a 403 on the second chunk.
    mock = LibraryMock(script=[200, 200, 403])
    with make_client(mock) as client, Cache(tmp_path / "c.db") as cache:
        summary = saver.save_to_library(many, client, cache, chunk_size=20)
        assert summary.status == "failed"
        assert summary.saved_count == 20
        # And the 20 that worked are recorded, so undo can reach them.
        assert len(cache.saved_uris()) == 20


# -- undo ------------------------------------------------------------------

def test_undo_removes_exactly_what_was_saved(report, tmp_path):
    mock = LibraryMock()
    items = saver.read_report(report)
    with Cache(tmp_path / "c.db") as cache:
        with make_client(mock) as client:
            saver.save_to_library(items, client, cache)
        with make_client(mock) as client:
            summary = saver.undo(client, cache)
    assert sorted(mock.removed) == ["spotify:show:aaa111", "spotify:show:bbb222"]
    assert summary.saved_count == 2


def test_undo_does_not_touch_books_you_saved_yourself(report, tmp_path):
    """Only what this tool added is fair game."""
    mock = LibraryMock(already={"spotify:show:aaa111", "spotify:show:mine999"})
    items = saver.read_report(report)
    with Cache(tmp_path / "c.db") as cache:
        with make_client(mock) as client:
            saver.save_to_library(items, client, cache)
        with make_client(mock) as client:
            saver.undo(client, cache)
    assert "spotify:show:mine999" not in mock.removed
    assert "spotify:show:aaa111" not in mock.removed, "was already yours, not ours"
    assert mock.removed == ["spotify:show:bbb222"]


def test_undo_twice_is_harmless(report, tmp_path):
    mock = LibraryMock()
    items = saver.read_report(report)
    with Cache(tmp_path / "c.db") as cache:
        with make_client(mock) as client:
            saver.save_to_library(items, client, cache)
        with make_client(mock) as client:
            saver.undo(client, cache)
        removed_once = list(mock.removed)
        with make_client(mock) as client:
            second = saver.undo(client, cache)
    assert mock.removed == removed_once
    assert second.saved_count == 0


def test_saving_again_after_undo_works(report, tmp_path):
    mock = LibraryMock()
    items = saver.read_report(report)
    with Cache(tmp_path / "c.db") as cache:
        with make_client(mock) as client:
            saver.save_to_library(items, client, cache)
        with make_client(mock) as client:
            saver.undo(client, cache)
        with make_client(mock) as client:
            again = saver.save_to_library(items, client, cache)
    assert again.saved_count == 2
