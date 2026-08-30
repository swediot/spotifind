"""The whole thing, against a mock Spotify.

Nothing here has ever spoken to the real api.spotify.com — the sandbox this
was written in cannot reach it. What these tests do establish is that the
client behaves itself when Spotify pushes back, that the cache stops a
second run from asking again, and that the report says what happened.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from mock_spotify import MockBook, MockSpotify  # noqa: E402

from spotifind.auth import static_token_source  # noqa: E402
from spotifind.cache import Cache  # noqa: E402
from spotifind.checker import check_books, estimate_requests  # noqa: E402
from spotifind.csvimport import Book  # noqa: E402
from spotifind.ratelimit import (  # noqa: E402
    FakeClock,
    LimiterConfig,
    RateLimitAbort,
    RateLimiter,
)
from spotifind.report import write_csv, write_html  # noqa: E402
from spotifind.spotify import Forbidden, SpotifyClient, SpotifyError  # noqa: E402

CATALOGUE = [
    MockBook("Piranesi", ["Susanna Clarke"], ["Chiwetel Ejiofor"]),
    MockBook("Project Hail Mary", ["Andy Weir"], ["Ray Porter"]),
    MockBook("The Fifth Season: A Novel", ["N.K. Jemisin"], ["Robin Miles"]),
    MockBook("Klara and the Sun", ["Kazuo Ishiguro"], ["Sura Siu"], markets=("US", "GB")),
    MockBook("Dune Messiah", ["Frank Herbert"], ["Simon Vance"]),
]

WANTED = [
    Book("Piranesi", ["Susanna Clarke"]),
    Book("Project Hail Mary", ["Andy Weir"]),
    Book("The Fifth Season", ["N.K. Jemisin"]),
    Book("Klara and the Sun", ["Kazuo Ishiguro"]),
    Book("Dune", ["Frank Herbert"]),                       # only the sequel is there
    Book("A Book That Does Not Exist", ["Nobody At All"]),
]


@pytest.fixture
def clock():
    return FakeClock()


def make_client(mock: MockSpotify, clock: FakeClock, *, config: LimiterConfig | None = None,
                market: str | None = None) -> SpotifyClient:
    limiter = RateLimiter(
        config=config or LimiterConfig(),
        clock=clock.time,
        sleep=clock.sleep,
        rng=random.Random(0),
    )
    return SpotifyClient(
        static_token_source("test-token"),
        limiter,
        market=market,
        transport=mock.transport,
        sleep=clock.sleep,
        rng=random.Random(0),
    )


def cache_at(tmp_path) -> Cache:
    return Cache(tmp_path / "cache.sqlite3")


# -- the client ------------------------------------------------------------

def test_a_search_returns_parsed_candidates(clock):
    mock = MockSpotify(CATALOGUE)
    with make_client(mock, clock) as client:
        results = client.search_audiobooks("Piranesi Susanna Clarke")
    assert results[0].name == "Piranesi"
    assert results[0].authors == ["Susanna Clarke"]
    assert results[0].narrators == ["Chiwetel Ejiofor"]
    assert results[0].url.startswith("https://open.spotify.com/")


def test_the_request_asks_for_audiobooks_within_the_page_limit(clock):
    mock = MockSpotify(CATALOGUE)
    with make_client(mock, clock) as client:
        client.search_audiobooks("Piranesi", limit=50)   # caller asks for too many
    params = mock.requests[0].url.params
    assert params["type"] == "audiobook"
    assert int(params["limit"]) == 10, "must clamp to the API maximum of 10"


def test_it_identifies_itself(clock):
    mock = MockSpotify(CATALOGUE)
    with make_client(mock, clock) as client:
        client.search_audiobooks("Piranesi")
    assert "spotifind" in mock.requests[0].headers["user-agent"]
    assert mock.requests[0].headers["authorization"] == "Bearer test-token"


def test_only_one_request_is_ever_in_flight(clock, tmp_path):
    mock = MockSpotify(CATALOGUE)
    with make_client(mock, clock) as client, cache_at(tmp_path) as cache:
        check_books(WANTED, client, cache, "US")
    assert mock.max_concurrent == 1


def test_a_429_is_honoured_then_the_call_is_retried(clock):
    mock = MockSpotify(CATALOGUE, script=[(429, {"Retry-After": "7"})])
    with make_client(mock, clock, config=LimiterConfig(jitter_seconds=0.0)) as client:
        results = client.search_audiobooks("Piranesi Susanna Clarke")
    assert results[0].name == "Piranesi", "the call should succeed on the retry"
    assert client.stats.rate_limited == 1
    assert clock.total_slept >= 7.0, "Retry-After must actually be waited out"


def test_a_429_slows_the_rest_of_the_run_down(clock):
    mock = MockSpotify(CATALOGUE, script=[(429, {"Retry-After": "1"})])
    with make_client(mock, clock) as client:
        before = client.limiter.effective_rate
        client.search_audiobooks("Piranesi")
        assert client.limiter.effective_rate < before


def test_a_429_with_no_header_still_waits(clock):
    mock = MockSpotify(CATALOGUE, script=[429])
    with make_client(mock, clock, config=LimiterConfig(jitter_seconds=0.0)) as client:
        client.search_audiobooks("Piranesi")
    assert clock.total_slept >= 30.0, "no header means back off a whole window"


def test_repeated_429s_stop_the_run_rather_than_hammering(clock):
    mock = MockSpotify(CATALOGUE, script=[(429, {"Retry-After": "1"})] * 10)
    with make_client(mock, clock) as client:
        with pytest.raises(RateLimitAbort):
            client.search_audiobooks("Piranesi")
    assert len(mock.requests) == 3, "must give up after three, not keep knocking"


def test_the_run_stops_on_the_first_429_storm_not_per_book(clock, tmp_path):
    mock = MockSpotify(CATALOGUE, script=[(429, {"Retry-After": "1"})] * 20)
    with make_client(mock, clock) as client, cache_at(tmp_path) as cache:
        summary = check_books(WANTED, client, cache, "US")
    assert summary.status == "aborted"
    assert len(mock.requests) == 3
    assert "cached" in summary.note


def test_a_transient_500_is_retried(clock):
    mock = MockSpotify(CATALOGUE, script=[500, 502])
    with make_client(mock, clock) as client:
        results = client.search_audiobooks("Piranesi Susanna Clarke")
    assert results[0].name == "Piranesi"
    assert client.stats.retries_5xx == 2


def test_a_persistent_500_gives_up_on_that_call(clock):
    mock = MockSpotify(CATALOGUE, script=[500] * 10)
    with make_client(mock, clock) as client:
        with pytest.raises(SpotifyError):
            client.search_audiobooks("Piranesi")


def test_a_403_is_reported_as_a_configuration_problem(clock):
    mock = MockSpotify(CATALOGUE, script=[403])
    with make_client(mock, clock) as client:
        with pytest.raises(Forbidden, match="403"):
            client.search_audiobooks("Piranesi")


def test_a_403_stops_the_whole_run(clock, tmp_path):
    """A 403 is never one book's fault; do not repeat it 1,400 times."""
    mock = MockSpotify(CATALOGUE, script=[403] * 20)
    with make_client(mock, clock) as client, cache_at(tmp_path) as cache:
        summary = check_books(WANTED, client, cache, "US")
    assert summary.status == "failed"
    assert len(mock.requests) == 1


def test_nulls_in_the_page_are_skipped(clock):
    """Spotify pads pages with nulls for market-unavailable items."""
    def handler(request):
        import httpx
        return httpx.Response(200, json={"audiobooks": {"items": [None, None]}})

    import httpx
    mock = MockSpotify(CATALOGUE)
    with make_client(mock, clock) as client:
        client._client = httpx.Client(transport=httpx.MockTransport(handler))
        assert client.search_audiobooks("anything") == []


# -- the run ---------------------------------------------------------------

def test_a_full_run_sorts_the_books_correctly(clock, tmp_path):
    mock = MockSpotify(CATALOGUE)
    with make_client(mock, clock) as client, cache_at(tmp_path) as cache:
        summary = check_books(WANTED, client, cache, "US")

    found = {r.book.title for r in summary.strong}
    assert found == {"Piranesi", "Project Hail Mary", "The Fifth Season", "Klara and the Sun"}
    missing = {r.book.title for r in summary.missing}
    assert "A Book That Does Not Exist" in missing
    assert "Dune" in missing, "the sequel must not be reported as the book"
    assert summary.status == "ok"


def test_a_market_without_the_book_reports_it_missing(clock, tmp_path):
    mock = MockSpotify(CATALOGUE, market="CA")     # Klara is US/GB only
    with make_client(mock, clock) as client, cache_at(tmp_path) as cache:
        summary = check_books(WANTED, client, cache, "CA")
    assert "Klara and the Sun" in {r.book.title for r in summary.missing}


def test_the_second_run_asks_spotify_nothing(clock, tmp_path):
    mock = MockSpotify(CATALOGUE)
    with cache_at(tmp_path) as cache:
        with make_client(mock, clock) as client:
            check_books(WANTED, client, cache, "US")
        first = len(mock.requests)
        assert first > 0

        with make_client(mock, clock) as client:
            summary = check_books(WANTED, client, cache, "US")
        assert len(mock.requests) == first, "a warm run must make no requests"
        assert summary.from_cache == len(WANTED)
        assert len(summary.strong) == 4


def test_refresh_ignores_the_cache(clock, tmp_path):
    mock = MockSpotify(CATALOGUE)
    with cache_at(tmp_path) as cache:
        with make_client(mock, clock) as client:
            check_books(WANTED[:2], client, cache, "US")
        first = len(mock.requests)
        with make_client(mock, clock) as client:
            check_books(WANTED[:2], client, cache, "US", refresh=True)
        assert len(mock.requests) > first


def test_two_markets_do_not_overwrite_each_other(clock, tmp_path):
    with cache_at(tmp_path) as cache:
        with make_client(MockSpotify(CATALOGUE, market="US"), clock) as client:
            check_books([WANTED[3]], client, cache, "US")
        with make_client(MockSpotify(CATALOGUE, market="CA"), clock) as client:
            check_books([WANTED[3]], client, cache, "CA")
        assert cache.get(WANTED[3].key, "US").found
        assert not cache.get(WANTED[3].key, "CA").found


def test_a_book_appearing_later_is_flagged_as_new(clock, tmp_path):
    thin = [MockBook("Piranesi", ["Susanna Clarke"])]
    books = [Book("Piranesi", ["Susanna Clarke"]), Book("Project Hail Mary", ["Andy Weir"])]
    with cache_at(tmp_path) as cache:
        with make_client(MockSpotify(thin), clock) as client:
            first = check_books(books, client, cache, "US")
        assert first.newly_found == [], "a first run has nothing to compare against"

        with make_client(MockSpotify(CATALOGUE), clock) as client:
            second = check_books(books, client, cache, "US", refresh=True)
        assert [r.book.title for r in second.newly_found] == ["Project Hail Mary"]


def test_the_estimate_counts_only_what_is_stale(clock, tmp_path):
    mock = MockSpotify(CATALOGUE)
    with cache_at(tmp_path) as cache:
        low, high = estimate_requests(WANTED, cache, "US", fallback="never")
        assert (low, high) == (len(WANTED), len(WANTED))
        with make_client(mock, clock) as client:
            check_books(WANTED, client, cache, "US")
        assert estimate_requests(WANTED, cache, "US", fallback="never") == (0, 0)
        assert estimate_requests(WANTED, cache, "US", refresh=True, fallback="empty") == \
            (len(WANTED), len(WANTED) * 2)


def test_fallback_never_makes_exactly_one_request_per_book(clock, tmp_path):
    mock = MockSpotify(CATALOGUE)
    with make_client(mock, clock) as client, cache_at(tmp_path) as cache:
        check_books(WANTED, client, cache, "US", fallback="never")
    assert len(mock.requests) == len(WANTED)


def test_fallback_empty_retries_only_when_nothing_came_back(clock, tmp_path):
    mock = MockSpotify(CATALOGUE)
    with make_client(mock, clock) as client, cache_at(tmp_path) as cache:
        check_books(WANTED, client, cache, "US", fallback="empty")
    # One per book, plus a title-only retry for the one book that found nothing.
    assert len(mock.requests) == len(WANTED) + 1
    assert mock.queries[-1] == "A Book That Does Not Exist"


def test_fallback_finds_a_book_whose_author_is_recorded_differently(clock, tmp_path):
    catalogue = [MockBook("Piranesi", ["Susanna Clarke"])]
    # StoryGraph has the translator's name; "Piranesi Ann Translator" finds nothing.
    books = [Book("Piranesi", ["Ann Translator", "Susanna Clarke"])]
    mock = MockSpotify(catalogue)
    with make_client(mock, clock) as client, cache_at(tmp_path) as cache:
        summary = check_books(books, client, cache, "US", fallback="empty")
    assert len(summary.strong) == 1
    assert len(mock.requests) == 2


def test_progress_is_reported_for_every_book(clock, tmp_path):
    seen = []
    mock = MockSpotify(CATALOGUE)
    with make_client(mock, clock) as client, cache_at(tmp_path) as cache:
        check_books(WANTED, client, cache, "US", on_progress=lambda i, r: seen.append(i))
    assert seen == list(range(1, len(WANTED) + 1))


def test_the_run_is_recorded(clock, tmp_path):
    mock = MockSpotify(CATALOGUE)
    with cache_at(tmp_path) as cache:
        run_id = cache.start_run(market="US", mode="user", csv_path="x.csv", books=6)
        with make_client(mock, clock) as client:
            check_books(WANTED, client, cache, "US")
            cache.finish_run(run_id, status="ok", requests=client.stats.requests)
        row = cache.previous_runs()[0]
        assert row["status"] == "ok"
        assert row["requests"] > 0


# -- several editions of one book -----------------------------------------

MULTILINGUAL = [
    MockBook("Project Hail Mary", ["Andy Weir"], ["Ray Porter"], languages=("en",)),
    MockBook("Project Hail Mary", ["Andy Weir"], ["William Angiuli"], languages=("en",)),
    MockBook("Project Hail Mary", ["Andy Weir"], ["Ein Sprecher"], languages=("de-DE",)),
]
ONE_BOOK = [Book("Project Hail Mary", ["Andy Weir"])]


def test_the_preferred_language_edition_is_the_one_reported(clock, tmp_path):
    mock = MockSpotify(MULTILINGUAL, market="CH")
    with make_client(mock, clock) as client, cache_at(tmp_path) as cache:
        summary = check_books(ONE_BOOK, client, cache, "CH", prefer_language="en")
    match = summary.strong[0].match
    assert "en" in match.candidate.language_codes()


def test_preferring_german_picks_the_german_edition(clock, tmp_path):
    mock = MockSpotify(MULTILINGUAL, market="CH")
    with make_client(mock, clock) as client, cache_at(tmp_path) as cache:
        summary = check_books(ONE_BOOK, client, cache, "CH", prefer_language="de")
    assert summary.strong[0].match.candidate.narrators == ["Ein Sprecher"]


def test_other_editions_are_counted_and_their_languages_kept(clock, tmp_path):
    mock = MockSpotify(MULTILINGUAL, market="CH")
    with make_client(mock, clock) as client, cache_at(tmp_path) as cache:
        summary = check_books(ONE_BOOK, client, cache, "CH")
    match = summary.strong[0].match
    assert match.alternates == 2
    assert "de" in match.alternate_languages


def test_an_edition_with_no_language_field_still_matches(clock, tmp_path):
    """Spotify does not always populate `languages`; unknown must not lose."""
    mock = MockSpotify([MockBook("Piranesi", ["Susanna Clarke"], languages=())], market="CH")
    with make_client(mock, clock) as client, cache_at(tmp_path) as cache:
        summary = check_books([Book("Piranesi", ["Susanna Clarke"])], client, cache, "CH")
    assert len(summary.strong) == 1


def test_a_known_wrong_language_loses_to_an_unknown_one(clock, tmp_path):
    mock = MockSpotify([
        MockBook("Piranesi", ["Susanna Clarke"], ["Deutsch"], languages=("de",)),
        MockBook("Piranesi", ["Susanna Clarke"], ["Unknown"], languages=()),
    ], market="CH")
    with make_client(mock, clock) as client, cache_at(tmp_path) as cache:
        summary = check_books([Book("Piranesi", ["Susanna Clarke"])], client, cache, "CH")
    assert summary.strong[0].match.candidate.narrators == ["Unknown"]


def test_the_language_survives_the_cache(clock, tmp_path):
    with cache_at(tmp_path) as cache:
        with make_client(MockSpotify(MULTILINGUAL, market="CH"), clock) as client:
            check_books(ONE_BOOK, client, cache, "CH")
        with make_client(MockSpotify(MULTILINGUAL, market="CH"), clock) as client:
            warm = check_books(ONE_BOOK, client, cache, "CH")
    match = warm.strong[0].match
    assert "en" in match.candidate.language_codes()
    assert match.alternates == 2
    assert "de" in match.alternate_languages


def test_a_database_from_before_languages_still_opens(tmp_path):
    """Upgrading must not need the cache thrown away."""
    import sqlite3

    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE lookups (
            book_key TEXT NOT NULL, market TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL, authors TEXT NOT NULL DEFAULT '',
            query TEXT NOT NULL DEFAULT '', checked_at TEXT NOT NULL,
            confidence TEXT NOT NULL, title_score REAL NOT NULL DEFAULT 0,
            author_matched INTEGER NOT NULL DEFAULT 0,
            spotify_id TEXT NOT NULL DEFAULT '', spotify_name TEXT NOT NULL DEFAULT '',
            spotify_authors TEXT NOT NULL DEFAULT '', narrators TEXT NOT NULL DEFAULT '',
            edition TEXT NOT NULL DEFAULT '', chapters INTEGER,
            url TEXT NOT NULL DEFAULT '', first_found_at TEXT NOT NULL DEFAULT '',
            candidates TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (book_key, market));
        INSERT INTO lookups (book_key, market, title, checked_at, confidence,
                             spotify_id, spotify_name)
        VALUES ('piranesi|clarke', 'CH', 'Piranesi', '2026-08-01T00:00:00+00:00',
                'strong', 'abc', 'Piranesi');
    """)
    conn.commit()
    conn.close()

    with Cache(path) as cache:
        entry = cache.get("piranesi|clarke", "CH")
        assert entry.found
        assert entry.match.candidate.languages == []
        assert entry.match.alternates == 0


# -- the report ------------------------------------------------------------

def test_the_csv_has_a_row_per_book(clock, tmp_path):
    mock = MockSpotify(CATALOGUE)
    with make_client(mock, clock) as client, cache_at(tmp_path) as cache:
        summary = check_books(WANTED, client, cache, "US")
    path = write_csv(summary, tmp_path / "out.csv")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(WANTED) + 1
    assert lines[0].startswith("title,authors,status")
    body = "\n".join(lines[1:])
    assert "on_spotify" in body and "not_found" in body
    assert "Chiwetel Ejiofor" in body, "the narrator should reach the CSV"


def test_the_html_is_self_contained_and_says_the_numbers(clock, tmp_path):
    mock = MockSpotify(CATALOGUE)
    with make_client(mock, clock) as client, cache_at(tmp_path) as cache:
        summary = check_books(WANTED, client, cache, "US")
    path = write_html(summary, tmp_path / "out.html", meta={"market": "US", "requests": 7})
    page = path.read_text(encoding="utf-8")

    assert "<script src" not in page and "<link" not in page, "must not fetch anything"
    assert "4 of 6 books" in page
    assert "Chiwetel Ejiofor" in page
    assert "prefers-color-scheme" in page
    assert "[hidden]" in page, "the filter depends on this rule"
    assert "A Book That Does Not Exist" in page


def test_html_escaping(clock, tmp_path):
    mock = MockSpotify([MockBook("<script>alert(1)</script>", ["A & B"])])
    books = [Book("<script>alert(1)</script>", ["A & B"])]
    with make_client(mock, clock) as client, cache_at(tmp_path) as cache:
        summary = check_books(books, client, cache, "US")
    page = write_html(summary, tmp_path / "x.html").read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_an_empty_result_set_points_at_probe_not_at_the_market(clock, tmp_path):
    """Nothing found has two causes. The report must not assert either one."""
    mock = MockSpotify(CATALOGUE, market="JP")
    with make_client(mock, clock) as client, cache_at(tmp_path) as cache:
        summary = check_books(WANTED, client, cache, "JP")
    assert summary.strong == []
    page = write_html(summary, tmp_path / "x.html").read_text(encoding="utf-8")
    assert "spotifind probe" in page
    assert "Switzerland" not in page


def test_a_swiss_market_finds_books(clock, tmp_path):
    """Spotify launched audiobooks in Switzerland in April 2025.

    Klara is the one title marked US/GB-only in the fixture, so CH finds
    three of the four — a market difference, not an empty catalogue.
    """
    mock = MockSpotify(CATALOGUE, market="CH")
    with make_client(mock, clock) as client, cache_at(tmp_path) as cache:
        summary = check_books(WANTED, client, cache, "CH")
    assert len(summary.strong) == 3
    assert "Klara and the Sun" in {r.book.title for r in summary.missing}


def test_the_report_notes_the_premium_listening_allowance(clock, tmp_path):
    """'On Spotify' is about the catalogue, not about unlimited listening."""
    mock = MockSpotify(CATALOGUE)
    with make_client(mock, clock) as client, cache_at(tmp_path) as cache:
        summary = check_books(WANTED, client, cache, "US")
    page = write_html(summary, tmp_path / "x.html").read_text(encoding="utf-8")
    assert "monthly listening allowance" in page


def test_an_aborted_run_says_so_in_the_report(clock, tmp_path):
    mock = MockSpotify(CATALOGUE, script=[(429, {"Retry-After": "1"})] * 20)
    with make_client(mock, clock) as client, cache_at(tmp_path) as cache:
        summary = check_books(WANTED, client, cache, "US")
    page = write_html(summary, tmp_path / "x.html").read_text(encoding="utf-8")
    assert "stopped early" in page
