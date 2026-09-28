"""The daily request budget: the guard against the limit that actually bites.

Two real runs were cut off after 682 and 697 requests, at two different
rates, each told to come back in most of a day. These tests pin down that the
tool now stops itself before that point, remembers a long refusal between
runs, and never loses what it already found.
"""

from __future__ import annotations

import random
import sqlite3
import sys
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from mock_spotify import MockBook, MockSpotify  # noqa: E402

from spotifind import cli  # noqa: E402
from spotifind.auth import static_token_source  # noqa: E402
from spotifind.budget import (  # noqa: E402
    DEFAULT_DAILY_BUDGET,
    WINDOW_SECONDS,
    BudgetExhausted,
    DailyBudget,
)
from spotifind.cache import Cache  # noqa: E402
from spotifind.checker import check_books  # noqa: E402
from spotifind.csvimport import Book  # noqa: E402
from spotifind.ratelimit import FakeClock, LimiterConfig, RateLimitAbort, RateLimiter  # noqa: E402
from spotifind.report import write_html  # noqa: E402
from spotifind.spotify import SpotifyClient  # noqa: E402

CATALOGUE = [
    MockBook("Piranesi", ["Susanna Clarke"], ["Chiwetel Ejiofor"]),
    MockBook("Project Hail Mary", ["Andy Weir"], ["Ray Porter"]),
    MockBook("Dune Messiah", ["Frank Herbert"], ["Simon Vance"]),
]


@pytest.fixture
def clock():
    # Wall time, near the real now: the cache prunes ledger entries older than
    # a week on open, so a fixed epoch would make these tests rot.
    return FakeClock(start=time.time())


@pytest.fixture
def cache(tmp_path):
    with Cache(tmp_path / "cache.sqlite3") as c:
        yield c


def budget_for(cache, clock, limit=3, **kwargs) -> DailyBudget:
    return DailyBudget(cache, limit=limit, clock=clock.time, sleep=clock.sleep, **kwargs)


def client_for(mock, clock, budget) -> SpotifyClient:
    limiter = RateLimiter(config=LimiterConfig(), clock=clock.time,
                          sleep=clock.sleep, rng=random.Random(0))
    return SpotifyClient(static_token_source("t"), limiter, transport=mock.transport,
                         sleep=clock.sleep, rng=random.Random(0), budget=budget)


# -- the budget on its own ---------------------------------------------------

def test_the_default_sits_under_the_observed_cutoff():
    assert DEFAULT_DAILY_BUDGET < 682


def test_it_spends_up_to_the_limit_and_then_refuses(cache, clock):
    budget = budget_for(cache, clock, limit=3)
    for _ in range(3):
        budget.spend()
        clock.sleep(2)
    assert budget.remaining() == 0
    with pytest.raises(BudgetExhausted) as exc:
        budget.spend()
    assert "Daily request budget reached" in str(exc.value)
    assert "3 requests" in str(exc.value)
    assert budget.used() == 3, "a refused request is not recorded"


def test_a_budget_stop_is_a_rate_limit_abort():
    """Every caller that stops cleanly on a 429 abort stops cleanly on this."""
    assert issubclass(BudgetExhausted, RateLimitAbort)


def test_the_window_rolls_rather_than_resetting_at_midnight(cache, clock):
    budget = budget_for(cache, clock, limit=2)
    first = clock.time()
    budget.spend()
    clock.sleep(1000)                          # further apart than the budget will wait
    budget.spend()
    assert budget.available_at() == pytest.approx(first + WINDOW_SECONDS)

    clock.sleep(WINDOW_SECONDS - 1000 + 1)     # the first request has aged out
    assert budget.remaining() == 1
    budget.spend()
    with pytest.raises(BudgetExhausted):
        budget.spend()


def test_the_budget_carries_over_between_runs(tmp_path, clock):
    path = tmp_path / "cache.sqlite3"
    with Cache(path) as first_run:
        budget = budget_for(first_run, clock, limit=3)
        budget.spend()
        budget.spend()
    with Cache(path) as second_run:
        budget = budget_for(second_run, clock, limit=3)
        assert budget.used() == 2
        budget.spend()
        with pytest.raises(BudgetExhausted):
            budget.spend()


def test_a_retry_after_of_hours_is_remembered_between_runs(tmp_path, clock):
    path = tmp_path / "cache.sqlite3"
    with Cache(path) as c:
        budget = budget_for(c, clock, limit=600)
        budget.note_retry_after(83818)
        assert budget.remaining() == 0

    with Cache(path) as c:
        budget = budget_for(c, clock, limit=600)
        with pytest.raises(BudgetExhausted) as exc:
            budget.spend()
        assert "refused this app until" in str(exc.value)
        assert "Spotify has refused" in budget.status_line()

        clock.sleep(83818 + 1)
        budget.spend()                       # the door is open again


def test_a_short_retry_after_is_left_to_the_rate_limiter(cache, clock):
    budget = budget_for(cache, clock, limit=5)
    budget.note_retry_after(5)
    budget.note_retry_after(None)
    assert budget.remaining() == 5


def test_a_run_during_the_trickle_paces_itself_instead_of_stopping(cache, clock):
    """The day after a big run the budget comes back one request at a time.
    A run started then should follow that pace, not stop after one request."""
    budget = budget_for(cache, clock, limit=2)
    start = clock.time()
    budget.spend()
    clock.sleep(60)
    budget.spend()

    clock.sleep(WINDOW_SECONDS - 60 + 1)       # only the first has aged out
    budget.spend()
    before = clock.time()
    budget.spend()                             # the second frees up in ~59s: wait for it
    assert clock.time() - before == pytest.approx(59, abs=1)
    assert clock.time() > start + 60 + WINDOW_SECONDS


def test_a_slot_moments_away_is_not_a_refusal(cache, clock):
    budget = budget_for(cache, clock, limit=1)
    budget.spend()
    clock.sleep(WINDOW_SECONDS - 30)
    assert budget.remaining() == 0
    assert not budget.refuses_now(), "30 seconds is worth waiting for"
    impatient = budget_for(cache, clock, limit=1, max_wait_seconds=10)
    assert impatient.refuses_now()


def test_it_never_waits_out_a_refusal_from_spotify(cache, clock):
    budget = budget_for(cache, clock, limit=10, max_wait_seconds=1000)
    budget.note_retry_after(400)
    before = clock.time()
    with pytest.raises(BudgetExhausted):
        budget.spend()
    assert clock.time() == before, "a closed door is not waited at"


def test_the_message_says_when_the_whole_budget_is_back(cache, clock):
    budget = budget_for(cache, clock, limit=2)
    budget.spend()
    clock.sleep(1800)
    budget.spend()
    with pytest.raises(BudgetExhausted) as exc:
        budget.spend()
    assert "frees up gradually from" in str(exc.value)
    assert "fully back by" in str(exc.value)


def test_a_budget_of_zero_is_not_a_budget(cache):
    with pytest.raises(ValueError):
        DailyBudget(cache, limit=0)


# -- upgrading a database from before the budget ------------------------------

def test_a_new_ledger_is_seeded_from_past_runs(tmp_path):
    """Today's 697-request run must count, or the first run after the
    upgrade believes the budget is untouched on a day Spotify has shut."""
    path = tmp_path / "cache.sqlite3"
    with Cache(path) as c:
        run_id = c.start_run(market="account", mode="user", csv_path="x.csv", books=1400)
        c.finish_run(run_id, status="aborted", requests=697)
        # A run from last month is no use to a 24-hour budget.
        c.conn.execute(
            "INSERT INTO runs (started_at, finished_at, requests, status) "
            "VALUES ('2026-08-30T15:34:18+00:00', '2026-08-30T16:05:07+00:00', 682, 'aborted')"
        )
        c.conn.execute("DROP TABLE api_requests")   # as a pre-budget database
        c.conn.execute("DROP TABLE settings")
        c.conn.commit()

    with Cache(path) as c:
        budget = DailyBudget(c)                     # real wall clock
        assert budget.used() == 697
        assert budget.remaining() == 0
        with pytest.raises(BudgetExhausted):
            budget.spend()


def test_the_seed_runs_only_once(tmp_path):
    path = tmp_path / "cache.sqlite3"
    with Cache(path) as c:
        run_id = c.start_run(market="account", mode="user", csv_path="x.csv", books=10)
        c.finish_run(run_id, status="ok", requests=10)
        c.conn.execute("DROP TABLE api_requests")
        c.conn.commit()
    for _ in range(3):
        with Cache(path) as c:
            assert DailyBudget(c).used() == 10


def test_an_old_database_without_a_runs_history_still_opens(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, "
                 "finished_at TEXT NOT NULL DEFAULT '', requests INTEGER NOT NULL DEFAULT 0)")
    conn.commit()
    conn.close()
    with Cache(path) as c:
        assert DailyBudget(c).used() == 0


# -- through the client -------------------------------------------------------

def test_the_client_stops_before_sending_what_the_budget_cannot_cover(cache, clock):
    mock = MockSpotify(CATALOGUE)
    budget = budget_for(cache, clock, limit=2)
    with client_for(mock, clock, budget) as client:
        client.search_audiobooks("Piranesi Susanna Clarke")
        client.search_audiobooks("Project Hail Mary Andy Weir")
        with pytest.raises(BudgetExhausted):
            client.search_audiobooks("Dune Messiah Frank Herbert")
    assert len(mock.requests) == 2, "the third request never left the machine"


def test_retries_count_against_the_budget(cache, clock):
    """Spotify counts the requests it refused; so must the budget."""
    mock = MockSpotify(CATALOGUE, script=[(429, {"Retry-After": "1"})])
    budget = budget_for(cache, clock, limit=10)
    with client_for(mock, clock, budget) as client:
        client.search_audiobooks("Piranesi Susanna Clarke")
    assert len(mock.requests) == 2
    assert budget.used() == 2


def test_a_daily_quota_refusal_keeps_the_next_run_away(tmp_path, clock):
    """The failure that started this: 429 with Retry-After of 23 hours."""
    mock = MockSpotify(CATALOGUE, script=[(429, {"Retry-After": "83818"})])
    path = tmp_path / "cache.sqlite3"

    with Cache(path) as c, client_for(mock, clock, budget_for(c, clock, limit=600)) as client:
        with pytest.raises(RateLimitAbort):
            client.search_audiobooks("Piranesi Susanna Clarke")
    assert len(mock.requests) == 1

    clock.sleep(3600)                               # an hour later, try again
    with Cache(path) as c, client_for(mock, clock, budget_for(c, clock, limit=600)) as client:
        with pytest.raises(BudgetExhausted):
            client.search_audiobooks("Piranesi Susanna Clarke")
    assert len(mock.requests) == 1, "nothing was sent while the door was shut"


# -- a run that hits the budget ---------------------------------------------

BOOKS = [
    Book("Piranesi", ["Susanna Clarke"]),
    Book("Project Hail Mary", ["Andy Weir"]),
    Book("Dune", ["Frank Herbert"]),
]


def test_a_budget_stop_still_reports_every_cached_book(cache, clock):
    mock = MockSpotify(CATALOGUE)
    # An earlier day checked the last book.
    with client_for(mock, clock, None) as client:
        check_books(BOOKS[2:], client, cache, "US", fallback="never")

    budget = budget_for(cache, clock, limit=1)
    with client_for(mock, clock, budget) as client:
        summary = check_books(BOOKS, client, cache, "US", fallback="never")

    assert summary.status == "aborted"
    assert summary.stopped_by_budget
    assert [r.book.title for r in summary.results] == ["Piranesi", "Dune"]
    assert summary.results[1].from_cache
    assert summary.unchecked == 1

    page = write_html(summary, Path(cache.path).with_name("r.html")).read_text(encoding="utf-8")
    assert "daily request budget" in page
    assert "1 books are not checked yet" in page


def test_the_next_day_finishes_the_list_without_asking_twice(cache, clock):
    mock = MockSpotify(CATALOGUE)
    budget = budget_for(cache, clock, limit=2)
    with client_for(mock, clock, budget) as client:
        first = check_books(BOOKS, client, cache, "US", fallback="never")
    assert first.unchecked == 1

    clock.sleep(WINDOW_SECONDS + 1)
    with client_for(mock, clock, budget) as client:
        second = check_books(BOOKS, client, cache, "US", fallback="never")
    assert second.status == "ok"
    assert second.from_cache == 2
    assert len(mock.requests) == 3, "one request per book across both days"


# -- the command line ----------------------------------------------------------

HEADER = (
    "Title,Authors,Contributors,ISBN/UID,Format,Read Status,Date Added,"
    "Last Date Read,Dates Read,Read Count,Moods,Pace,Star Rating,Review,Tags,Owned?\n"
)
ROWS = (
    "Piranesi,Susanna Clarke,,,,to-read,2025/01/01,,,0,,,,,,No\n"
    "Project Hail Mary,Andy Weir,,,,to-read,2025/01/02,,,0,,,,,,No\n"
    "A Book That Does Not Exist,Nobody At All,,,,to-read,2025/01/03,,,0,,,,,,No\n"
)


@pytest.fixture
def export(tmp_path):
    path = tmp_path / "export.csv"
    path.write_text(HEADER + ROWS, encoding="utf-8")
    return path


@pytest.fixture
def wired(monkeypatch):
    mock = MockSpotify(CATALOGUE)
    clock = FakeClock()

    def fake_client(source, limiter, *, market=None, **kwargs):
        limiter.clock, limiter.sleep = clock.time, clock.sleep
        return SpotifyClient(static_token_source("t"), limiter, market=market,
                             transport=mock.transport, sleep=clock.sleep,
                             rng=random.Random(0), budget=kwargs.get("budget"))

    monkeypatch.setattr(cli, "SpotifyClient", fake_client)
    monkeypatch.setattr(cli, "_token_source", lambda args: static_token_source("t"))
    return mock


def run(argv, tmp_path):
    return cli.main(argv + ["--client-id", "x", "--cache", str(tmp_path / "c.sqlite3"),
                            "--out-csv", str(tmp_path / "r.csv"),
                            "--out-html", str(tmp_path / "r.html")])


def test_check_stops_at_the_budget_and_says_when_to_come_back(wired, export, tmp_path, capsys):
    code = run(["check", str(export), "--daily-budget", "1", "--fallback", "never"], tmp_path)
    out = capsys.readouterr().out
    assert code == 3
    assert len(wired.requests) == 1
    assert "Daily request budget reached" in out
    assert "It frees up" in out
    assert "not checked yet 2" in out
    assert (tmp_path / "r.html").exists()


def test_the_pre_flight_shows_the_budget(wired, export, tmp_path, capsys):
    run(["check", str(export), "--dry-run"], tmp_path)
    out = capsys.readouterr().out
    assert f"Daily budget: {DEFAULT_DAILY_BUDGET} of {DEFAULT_DAILY_BUDGET} requests left" in out


def test_the_pre_flight_warns_when_the_budget_will_not_cover_the_run(wired, export, tmp_path, capsys):
    run(["check", str(export), "--dry-run", "--daily-budget", "2"], tmp_path)
    out = capsys.readouterr().out
    assert "This run can send 2 of them, then it stops by itself" in out
    assert "the whole list needs about 2 days" in out
    assert wired.requests == []


def test_a_spent_budget_sends_nothing_at_all(wired, export, tmp_path, capsys):
    with Cache(tmp_path / "c.sqlite3") as c:
        for _ in range(5):
            c.record_request(time.time())
    code = run(["check", str(export), "--daily-budget", "5"], tmp_path)
    out = capsys.readouterr().out
    assert code == 3
    assert wired.requests == []
    assert "Daily budget spent" in out
    assert "Nothing sent to Spotify" in out


def test_the_pre_flight_does_not_give_up_while_the_budget_trickles_back(wired, export, tmp_path, capsys):
    with Cache(tmp_path / "c.sqlite3") as c:
        c.record_request(time.time() - WINDOW_SECONDS + 60)   # frees up in a minute
    code = run(["check", str(export), "--dry-run", "--daily-budget", "1"], tmp_path)
    out = capsys.readouterr().out
    assert code == 0
    assert "coming back one request at a time" in out
    assert "Nothing sent" not in out


def test_probe_counts_against_the_budget_and_reports_it(wired, tmp_path, capsys):
    code = cli.main(["probe", "--client-id", "x", "--cache", str(tmp_path / "c.sqlite3"),
                     "--query", "Piranesi Susanna Clarke", "--daily-budget", "10"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Daily budget: 9 of 10 requests left" in out


def test_probe_refuses_while_spotify_has_shut_the_door(wired, tmp_path, capsys):
    with Cache(tmp_path / "c.sqlite3") as c:
        c.set_blocked_until(time.time() + 3600)
    code = cli.main(["probe", "--client-id", "x", "--cache", str(tmp_path / "c.sqlite3")])
    err = capsys.readouterr().err
    assert code == 3
    assert wired.requests == []
    assert "refused this app until" in err


def test_a_budget_of_zero_turns_it_off(wired, export, tmp_path, capsys):
    code = run(["check", str(export), "--daily-budget", "0"], tmp_path)
    assert code == 0
    with Cache(tmp_path / "c.sqlite3") as c:
        assert c.request_times_since(0) == []


def test_a_negative_budget_is_refused(wired, export, tmp_path):
    with pytest.raises(SystemExit) as exc:
        run(["check", str(export), "--daily-budget", "-1"], tmp_path)
    assert "cannot be negative" in str(exc.value)
