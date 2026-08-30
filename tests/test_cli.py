"""The command line, driven end to end against the mock."""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from mock_spotify import MockBook, MockSpotify  # noqa: E402

from spotifind import cli  # noqa: E402
from spotifind.auth import static_token_source  # noqa: E402
from spotifind.ratelimit import FakeClock, RateLimiter  # noqa: E402
from spotifind.spotify import SpotifyClient  # noqa: E402

HEADER = (
    "Title,Authors,Contributors,ISBN/UID,Format,Read Status,Date Added,"
    "Last Date Read,Dates Read,Read Count,Moods,Pace,Star Rating,Review,Tags,Owned?\n"
)

ROWS = (
    "Piranesi,Susanna Clarke,,,,to-read,2025/01/01,,,0,,,,,,No\n"
    "Project Hail Mary,Andy Weir,,,,to-read,2025/01/02,,,0,,,,,,No\n"
    "A Book That Does Not Exist,Nobody At All,,,,to-read,2025/01/03,,,0,,,,,,No\n"
    "Already Read,Someone,,,,read,2025/01/04,,,0,,,,,,No\n"
)

CATALOGUE = [
    MockBook("Piranesi", ["Susanna Clarke"], ["Chiwetel Ejiofor"]),
    MockBook("Project Hail Mary", ["Andy Weir"], ["Ray Porter"]),
]


@pytest.fixture
def export(tmp_path):
    path = tmp_path / "storygraph_export.csv"
    path.write_text(HEADER + ROWS, encoding="utf-8")
    return path


@pytest.fixture
def wired(monkeypatch):
    """Point the CLI at the mock, with a clock that costs no wall time."""
    mock = MockSpotify(CATALOGUE)
    clock = FakeClock()

    def fake_client(source, limiter, *, market=None, **kwargs):
        limiter.clock = clock.time
        limiter.sleep = clock.sleep
        limiter.rng = random.Random(0)
        return SpotifyClient(
            static_token_source("t"), limiter, market=market,
            transport=mock.transport, sleep=clock.sleep, rng=random.Random(0),
        )

    monkeypatch.setattr(cli, "SpotifyClient", fake_client)
    monkeypatch.setattr(cli, "_token_source", lambda args: static_token_source("t"))
    return mock


def run(argv, tmp_path):
    return cli.main(argv + ["--cache", str(tmp_path / "c.sqlite3")])


def test_check_writes_both_files_and_reports(wired, export, tmp_path, capsys):
    code = run(["check", str(export), "--client-id", "x",
                "--out-csv", str(tmp_path / "r.csv"),
                "--out-html", str(tmp_path / "r.html")], tmp_path)
    out = capsys.readouterr().out

    assert code == 0
    assert "skipped 1 not on the to-read shelf" in out
    assert (tmp_path / "r.csv").exists()
    assert (tmp_path / "r.html").exists()
    assert "on Spotify      2" in out
    assert "not found       1" in out


def test_dry_run_asks_spotify_nothing(wired, export, tmp_path, capsys):
    code = run(["check", str(export), "--client-id", "x", "--dry-run"], tmp_path)
    out = capsys.readouterr().out
    assert code == 0
    assert "requests at 1/s" in out
    assert wired.requests == []


def test_the_estimate_shrinks_after_a_run(wired, export, tmp_path, capsys):
    run(["check", str(export), "--client-id", "x",
         "--out-csv", str(tmp_path / "a.csv"), "--out-html", str(tmp_path / "a.html")], tmp_path)
    capsys.readouterr()
    run(["check", str(export), "--client-id", "x", "--dry-run"], tmp_path)
    assert "already cached and fresh" in capsys.readouterr().out


def test_a_slower_rate_is_accepted_and_used(wired, export, tmp_path, capsys):
    code = run(["check", str(export), "--client-id", "x", "--rate", "0.25", "--dry-run"], tmp_path)
    assert code == 0
    assert "at 0.25/s" in capsys.readouterr().out


def test_market_is_ignored_with_a_user_token_and_says_so(wired, export, tmp_path, capsys):
    run(["check", str(export), "--client-id", "x", "--market", "US", "--dry-run"], tmp_path)
    assert "--market is ignored with a user token" in capsys.readouterr().out


def test_an_unlisted_market_gets_a_hedge_not_a_verdict(wired, export, tmp_path, capsys, monkeypatch):
    """The market list goes stale; the warning must not sound certain."""
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "s")
    run(["check", str(export), "--client-id", "x", "--auth", "app",
         "--market", "JP", "--dry-run"], tmp_path)
    out = capsys.readouterr().out
    assert "not on the list of markets" in out
    assert "heads-up, not a verdict" in out
    assert "probe" in out


def test_switzerland_is_a_known_audiobook_market(wired, export, tmp_path, capsys, monkeypatch):
    """Spotify launched audiobooks in CH in April 2025; do not warn about it."""
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "s")
    run(["check", str(export), "--client-id", "x", "--auth", "app",
         "--market", "CH", "--dry-run"], tmp_path)
    assert "not on the list of markets" not in capsys.readouterr().out
    assert "CH" in cli.AUDIOBOOK_MARKETS


def test_limit_only_checks_the_first_n(wired, export, tmp_path, capsys):
    run(["check", str(export), "--client-id", "x", "--limit", "1",
         "--out-csv", str(tmp_path / "b.csv"), "--out-html", str(tmp_path / "b.html")], tmp_path)
    rows = (tmp_path / "b.csv").read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 2


def test_a_missing_file_is_a_clean_error(wired, tmp_path):
    with pytest.raises(SystemExit) as exc:
        run(["check", str(tmp_path / "nope.csv"), "--client-id", "x"], tmp_path)
    assert "No such file" in str(exc.value)


def test_no_client_id_anywhere_is_a_clean_error(export, tmp_path, monkeypatch):
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    with pytest.raises(SystemExit) as exc:
        run(["check", str(export)], tmp_path)
    assert "developer.spotify.com" in str(exc.value)


def test_probe_reports_a_visible_catalogue(wired, tmp_path, capsys):
    code = cli.main(["probe", "--client-id", "x", "--query", "Piranesi Susanna Clarke"])
    out = capsys.readouterr().out
    assert code == 0
    assert "audiobooks are visible to this token" in out
    assert "Chiwetel Ejiofor" in out


def _probe_against(monkeypatch, mock):
    clock = FakeClock()

    def fake_client(source, limiter, *, market=None, **kwargs):
        limiter.clock, limiter.sleep = clock.time, clock.sleep
        return SpotifyClient(static_token_source("t"), limiter, market=market,
                             transport=mock.transport, sleep=clock.sleep)

    monkeypatch.setattr(cli, "SpotifyClient", fake_client)
    monkeypatch.setattr(cli, "_token_source", lambda args: static_token_source("t"))


def test_probe_does_not_blame_the_market_for_an_empty_answer(monkeypatch, capsys):
    """An empty result has two causes; the message must not pick one."""
    _probe_against(monkeypatch, MockSpotify(CATALOGUE, market="JP"))
    code = cli.main(["probe", "--client-id", "x"])
    out = capsys.readouterr().out
    assert code == 1
    assert "No audiobooks came back for this query" in out
    assert "tell the two apart" in out


def test_probe_reports_the_languages_a_market_returns(monkeypatch, capsys):
    """A Swiss account sees German and French editions next to the English."""
    catalogue = [
        MockBook("Project Hail Mary", ["Andy Weir"], ["Ray Porter"], languages=("en",)),
        MockBook("Project Hail Mary", ["Andy Weir"], ["Ein Sprecher"], languages=("de-DE",)),
    ]
    _probe_against(monkeypatch, MockSpotify(catalogue, market="CH"))
    code = cli.main(["probe", "--client-id", "x", "--query", "Project Hail Mary Andy Weir"])
    out = capsys.readouterr().out
    assert code == 0
    assert "language: en" in out and "language: de" in out
    assert "returns editions in de, en" in out


# -- save ------------------------------------------------------------------

SAVE_HEADER = ("title,authors,status,confidence,spotify_title,spotify_authors,"
               "narrators,language,edition,chapters,spotify_id,spotify_url,"
               "other_editions,other_languages,title_score,author_matched,"
               "newly_found,from_cache,date_added,error\n")
SAVE_ROWS = (
    "Piranesi,Susanna Clarke,on_spotify,strong,Piranesi,Susanna Clarke,N,en,"
    "Unabridged,30,aaa111,https://open.spotify.com/show/aaa111,,,1.0,yes,,,,\n"
    "Circe,Madeline Miller,check,unconfirmed,Circe,Other,N,en,Unabridged,30,"
    "ccc333,https://open.spotify.com/show/ccc333,,,0.95,no,,,,\n"
)


@pytest.fixture
def saved_report(tmp_path):
    path = tmp_path / "spotify-availability.csv"
    path.write_text(SAVE_HEADER + SAVE_ROWS, encoding="utf-8")
    return path


def test_save_dry_run_lists_without_touching_spotify(saved_report, tmp_path, capsys, monkeypatch):
    called = []
    monkeypatch.setattr(cli, "SpotifyClient",
                        lambda *a, **k: called.append(1) or pytest.fail("no API call expected"))
    code = run(["save", str(saved_report), "--client-id", "x", "--dry-run"], tmp_path)
    out = capsys.readouterr().out
    assert code == 0
    assert "Piranesi" in out
    assert "nothing was sent to Spotify" in out
    assert called == []


def test_save_only_takes_confirmed_rows_by_default(saved_report, tmp_path, capsys):
    run(["save", str(saved_report), "--client-id", "x", "--dry-run"], tmp_path)
    out = capsys.readouterr().out
    assert "Piranesi" in out
    assert "Circe" not in out


def test_include_check_widens_it(saved_report, tmp_path, capsys):
    run(["save", str(saved_report), "--client-id", "x", "--dry-run", "--include-check"],
        tmp_path)
    assert "Circe" in capsys.readouterr().out


def test_save_refuses_without_confirmation(saved_report, tmp_path, capsys, monkeypatch):
    """A write to someone's account needs an explicit yes."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _p: "n")
    monkeypatch.setattr(cli, "SpotifyClient",
                        lambda *a, **k: pytest.fail("must not call Spotify after 'no'"))
    code = run(["save", str(saved_report), "--client-id", "x"], tmp_path)
    assert code == 1


def test_save_refuses_when_not_a_terminal_and_no_yes(saved_report, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(cli, "SpotifyClient",
                        lambda *a, **k: pytest.fail("must not call Spotify unattended"))
    code = run(["save", str(saved_report), "--client-id", "x"], tmp_path)
    assert code == 1
    assert "refusing to write" in capsys.readouterr().out


def test_save_needs_a_user_token(saved_report, tmp_path, monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "s")
    with pytest.raises(SystemExit) as exc:
        run(["save", str(saved_report), "--client-id", "x", "--auth", "app"], tmp_path)
    assert "your own account" in str(exc.value)


def test_undo_with_nothing_saved_says_so(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "SpotifyClient",
                        lambda *a, **k: pytest.fail("nothing to undo, so no API call"))
    code = run(["save", "--client-id", "x", "--undo"], tmp_path)
    assert code == 0
    assert "Nothing to undo" in capsys.readouterr().out


def test_login_for_saving_requests_the_library_scope(monkeypatch, capsys):
    seen = {}

    def fake_authorise(client_id, **kwargs):
        seen.update(kwargs)
        from spotifind.auth import Token
        return Token("at", 9e9, "rt", scopes=list(kwargs.get("scopes") or []))

    monkeypatch.setattr(cli.auth, "authorise_user", fake_authorise)
    code = cli.main(["login", "--client-id", "x", "--for-saving"])
    assert code == 0
    assert "user-library-modify" in seen["scopes"]
    assert "user-library-modify" in capsys.readouterr().out


def test_plain_login_asks_for_no_scopes_at_all(monkeypatch):
    """Don't request permissions the tool isn't about to use."""
    seen = {}

    def fake_authorise(client_id, **kwargs):
        seen.update(kwargs)
        from spotifind.auth import Token
        return Token("at", 9e9, "rt")

    monkeypatch.setattr(cli.auth, "authorise_user", fake_authorise)
    cli.main(["login", "--client-id", "x"])
    assert tuple(seen["scopes"]) == ()


@pytest.mark.parametrize("seconds, expected", [
    (12, "12s"), (95, "1m 35s"), (1500, "25m 00s"), (7200, "2h 00m"),
])
def test_duration_formatting(seconds, expected):
    assert cli._fmt_seconds(seconds) == expected
