"""Command line: `spotifind login`, `spotifind check`, `spotifind probe`."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import auth
from .cache import Cache
from .checker import FALLBACK_MODES, check_books, estimate_requests
from .csvimport import CsvFormatError, read_books
from .ratelimit import LimiterConfig, RateLimitAbort, RateLimiter
from .report import write_csv, write_html
from . import saver
from .spotify import Forbidden, SpotifyClient, SpotifyError

# Markets where Spotify audiobooks are known to exist. Spotify's own API
# reference still lists only the original six (US, GB, CA, IE, NZ, AU) and has
# not been updated for the European expansion — Germany, Austria, Switzerland
# and Liechtenstein arrived in April 2025, France/Belgium/Netherlands/
# Luxembourg before them. Verified against a live Swiss account on 2026-08-29.
#
# This list is only used to soften a warning, never to decide anything. If a
# market is missing from it, `probe` still tells you the truth in one request.
AUDIOBOOK_MARKETS = (
    "US", "GB", "CA", "IE", "NZ", "AU",
    "DE", "AT", "CH", "LI", "FR", "BE", "NL", "LU",
)

DEFAULT_CACHE = Path(
    os.environ.get("SPOTIFIND_CACHE")
    or (Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        / "spotifind" / "spotifind.sqlite3")
)


def _client_id(args) -> str:
    value = args.client_id or os.environ.get("SPOTIFY_CLIENT_ID", "")
    if not value:
        sys.exit(
            "No Spotify client id. Create an app at "
            "https://developer.spotify.com/dashboard, then either pass "
            "--client-id or set SPOTIFY_CLIENT_ID."
        )
    return value


def _token_source(args):
    if args.auth == "app":
        secret = args.client_secret or os.environ.get("SPOTIFY_CLIENT_SECRET", "")
        if not secret:
            sys.exit("--auth app needs --client-secret or SPOTIFY_CLIENT_SECRET.")
        return auth.app_token_source(_client_id(args), secret)
    return auth.user_token_source(_client_id(args), Path(args.token_path))


def _limiter(args) -> RateLimiter:
    return RateLimiter(config=LimiterConfig.from_rate(args.rate))


def _fmt_seconds(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


# --------------------------------------------------------------------------


def cmd_login(args) -> int:
    token = auth.authorise_user(
        _client_id(args),
        redirect_uri=args.redirect_uri,
        token_path=Path(args.token_path),
        open_browser=not args.no_browser,
        manual=args.manual,
        scopes=auth.SAVE_SCOPES if args.for_saving else (),
    )
    print(f"Signed in. Refresh token saved to {args.token_path}")
    if args.for_saving:
        granted = ", ".join(token.scopes) or "nothing"
        print(f"Permissions granted: {granted}")
        if "user-library-modify" not in token.scopes:
            print("Warning: library writes were NOT granted — `spotifind save` will "
                  "fail. Try signing in again and approving the request.")
    if not token.refresh_token:
        print("Warning: Spotify did not return a refresh token; you may need to sign in again next run.")
    return 0


def cmd_probe(args) -> int:
    """One request. Answers 'does my account see audiobooks at all?'"""
    source = _token_source(args)
    limiter = _limiter(args)
    market = args.market if args.auth == "app" else None

    def throttled(retry_after) -> None:
        header = f"Retry-After: {int(retry_after)}s" if retry_after else "no Retry-After header"
        print(f"Spotify asked us to slow down (429, {header}).", flush=True)

    with SpotifyClient(source, limiter, market=market,
                       on_throttle=throttled) as client:
        query = args.query
        try:
            candidates = client.search_audiobooks(query)
        except Forbidden as exc:
            print(f"403 from Spotify.\n{exc}")
            return 2
        except SpotifyError as exc:
            print(f"Failed: {exc}")
            return 2

    print(f"Query: {query!r}   auth: {args.auth}   market: {market or 'from your account'}")
    if not candidates:
        print(
            "\nNo audiobooks came back for this query.\n"
            "Either this title is not in the catalogue this token can see, or this\n"
            "token cannot see audiobooks at all. To tell the two apart, try another\n"
            "well-known title, and then a different market:\n"
            "  spotifind probe --query 'Becoming Michelle Obama'\n"
            "  spotifind probe --auth app --client-secret … --market US"
        )
        return 1

    print(f"\n{len(candidates)} result(s) — audiobooks are visible to this token:\n")
    languages: set[str] = set()
    for c in candidates[:5]:
        who = ", ".join(c.authors) or "?"
        narr = ", ".join(c.narrators)
        langs = ", ".join(sorted(c.language_codes()))
        languages |= c.language_codes()
        print(f"  · {c.name}\n      by {who}"
              + (f"\n      read by {narr}" if narr else "")
              + (f"\n      language: {langs}" if langs else ""))
    if len(languages) > 1:
        print(
            f"\nThis market returns editions in {', '.join(sorted(languages))}. "
            "`check` prefers\nthe --prefer-language edition (default en) and notes the others."
        )
    return 0


def cmd_check(args) -> int:
    csv_path = Path(args.csv).expanduser()
    if not csv_path.exists():
        sys.exit(f"No such file: {csv_path}")

    try:
        books, import_report = read_books(csv_path, only_to_read=not args.all_shelves)
    except CsvFormatError as exc:
        sys.exit(str(exc))

    if args.limit:
        books = books[: args.limit]
    if not books:
        sys.exit("No books to check. (Is this a to-read export? Try --all-shelves.)")

    print(
        f"Read {import_report['kept']} books from a {import_report['format']} export"
        + (f", skipped {import_report['skipped_not_to_read']} not on the to-read shelf"
           if import_report["skipped_not_to_read"] else "")
        + (f", {import_report['duplicates']} duplicates" if import_report["duplicates"] else "")
        + (f", {import_report['without_author']} without an author"
           if import_report["without_author"] else "")
        + "."
    )

    market_label = (args.market or "").upper() if args.auth == "app" else "account"
    if args.auth == "app" and args.market and args.market.upper() not in AUDIOBOOK_MARKETS:
        print(
            f"\nNote: {args.market.upper()} is not on the list of markets where Spotify is "
            "known to\nsell audiobooks. That list goes out of date — Spotify keeps adding "
            "markets and\nits own docs lag — so this is a heads-up, not a verdict. "
            "`spotifind probe --auth app\n--market " + args.market.upper() +
            "` settles it in one request."
        )
    if args.auth == "user" and args.market:
        print(
            "\nNote: --market is ignored with a user token — Spotify always uses your\n"
            "account's own country. Use --auth app to query a different market."
        )

    cache = Cache(args.cache, hit_ttl_days=args.hit_ttl, miss_ttl_days=args.miss_ttl)
    low, high = estimate_requests(books, cache, market_label,
                                  refresh=args.refresh, fallback=args.fallback)
    if low == 0:
        print("\nEverything is already cached and fresh — writing the report without asking Spotify.")
    else:
        est_low, est_high = low / args.rate, high / args.rate
        span = _fmt_seconds(est_low) if low == high else f"{_fmt_seconds(est_low)}–{_fmt_seconds(est_high)}"
        print(
            f"\n{low}" + (f"–{high}" if high != low else "") + f" requests at {args.rate:g}/s "
            f"≈ {span}. {len(books) - low} answers come from the cache."
        )
    if args.dry_run:
        cache.close()
        return 0

    source = _token_source(args)
    limiter = _limiter(args)
    client_market = args.market if args.auth == "app" else None
    started = time.monotonic()
    run_id = cache.start_run(market=market_label, mode=args.auth,
                             csv_path=str(csv_path), books=len(books))

    total = len(books)
    state = {"hits": 0, "last_line": 0.0}

    def throttled(retry_after) -> None:
        header = (f"Retry-After: {int(retry_after)}s" if retry_after
                  else "no Retry-After header")
        print(f"\n  Spotify asked us to slow down (429, {header}) — backing off.",
              flush=True)

    with SpotifyClient(source, limiter, market=client_market,
                       on_throttle=throttled) as client:

        def progress(index: int, result) -> None:
            if result.match.confidence == "strong":
                state["hits"] += 1
            now = time.monotonic()
            if now - state["last_line"] < 0.5 and index != total:
                return
            state["last_line"] = now
            sys.stdout.write(
                f"\r  {index}/{total} checked · {state['hits']} on Spotify · "
                f"{client.stats.requests} requests · {limiter.effective_rate:.2f}/s   "
            )
            sys.stdout.flush()

        summary = check_books(
            books, client, cache, market_label,
            refresh=args.refresh, fallback=args.fallback,
            prefer_language=args.prefer_language,
            on_progress=progress,
        )
        requests_made = client.stats.requests

    print()
    elapsed = time.monotonic() - started
    cache.finish_run(run_id, status=summary.status, requests=requests_made, note=summary.note)

    out_csv = Path(args.out_csv or (csv_path.parent / "spotify-availability.csv"))
    out_html = Path(args.out_html or (csv_path.parent / "spotify-availability.html"))
    write_csv(summary, out_csv)
    write_html(summary, out_html, meta={
        "market": market_label if args.auth == "app" else "your account’s country",
        "requests": requests_made,
        "from_cache": summary.from_cache,
        "rate": f"{limiter.effective_rate:.2f} requests/second",
        "elapsed": _fmt_seconds(elapsed),
    })
    cache.close()

    print(
        f"\n  on Spotify      {len(summary.strong)}"
        f"\n  worth a look    {len(summary.likely) + len(summary.unconfirmed)}"
        f"\n  not found       {len(summary.missing)}"
        + (f"\n  errors          {len(summary.failed)}" if summary.failed else "")
        + (f"\n  new since last  {len(summary.newly_found)}" if summary.newly_found else "")
    )
    print(f"\n  {requests_made} requests, {summary.from_cache} from cache, {_fmt_seconds(elapsed)}.")
    if limiter.stats.hits_429:
        print(f"  Spotify asked us to slow down {limiter.stats.hits_429}× "
              f"(rate is now {limiter.effective_rate:.2f}/s).")
    print(f"\n  {out_csv}\n  {out_html}")

    if summary.status == "aborted":
        print("\n" + summary.note + "\nRe-run the same command later; cached books are skipped.")
        return 3
    if summary.status == "failed":
        print("\n" + summary.note)
        return 2
    return 0


# --------------------------------------------------------------------------


def cmd_save(args) -> int:
    if args.auth != "user":
        sys.exit("Saving to a library needs your own account: drop --auth app.")

    cache = Cache(args.cache)

    if args.undo:
        pending = cache.saved_uris()
        if not pending:
            print("Nothing to undo — this tool has not saved anything from this cache.")
            cache.close()
            return 0
        print(f"About to REMOVE {len(pending)} audiobooks that spotifind added "
              "to your library.")
        for row in pending[:10]:
            print(f"  · {row['title']}")
        if len(pending) > 10:
            print(f"  … and {len(pending) - 10} more")
        if not _confirm(args, f"Remove {len(pending)} audiobooks?"):
            cache.close()
            return 1

        source = auth.user_token_source(_client_id(args), Path(args.token_path),
                                        required_scopes=auth.SAVE_SCOPES)
        with SpotifyClient(source, _limiter(args)) as client:
            summary = saver.undo(client, cache, chunk_size=args.chunk)
        cache.close()
        print(f"\nRemoved {summary.saved_count} of {len(pending)}.")
        if summary.note:
            print(summary.note)
        return 0 if summary.status == "ok" else 2

    statuses = ["on_spotify"]
    if args.include_probably:
        statuses.append("probably")
    if args.include_check:
        statuses.append("check")

    report_path = Path(args.report).expanduser()
    try:
        items = saver.read_report(report_path, include=statuses)
    except saver.ReportError as exc:
        cache.close()
        sys.exit(str(exc))

    if args.limit:
        items = items[: args.limit]
    if not items:
        cache.close()
        sys.exit(
            f"No rows in {report_path} with status {' or '.join(statuses)}. "
            "(Add --include-probably to widen it.)"
        )

    print(f"{len(items)} audiobooks from {report_path.name} "
          f"(status: {', '.join(statuses)}):\n")
    for item in items[:10]:
        print(f"  · {item.label}")
    if len(items) > 10:
        print(f"  … and {len(items) - 10} more")

    if args.dry_run:
        print("\n--dry-run: nothing was sent to Spotify.")
        cache.close()
        return 0

    print(f"\nThis ADDS them to your Spotify library. "
          f"`spotifind save --undo` removes exactly these again.")
    if not _confirm(args, f"Add {len(items)} audiobooks to your library?"):
        cache.close()
        return 1

    source = auth.user_token_source(_client_id(args), Path(args.token_path),
                                    required_scopes=auth.SAVE_SCOPES)
    total = len(items)

    with SpotifyClient(source, _limiter(args)) as client:
        def progress(done: int, _total: int) -> None:
            sys.stdout.write(f"\r  {done}/{total} processed…   ")
            sys.stdout.flush()

        summary = saver.save_to_library(
            items, client, cache, chunk_size=args.chunk,
            skip_existing=not args.no_skip, on_progress=progress,
        )
    print()
    cache.close()

    print(f"\n  added           {summary.saved_count}")
    if summary.already_there:
        print(f"  already saved   {len(summary.already_there)}")
    if summary.status != "ok":
        print(f"\n{summary.note}")
        return 3 if summary.status == "aborted" else 2
    print("\n  Undo with: spotifind save --undo")
    return 0


def _confirm(args, question: str) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print("Not a terminal and --yes not given — refusing to write to your library.")
        return False
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spotifind",
        description="Check which books on your to-read list are on Spotify as audiobooks.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--client-id", default="", help="Spotify app client id (or SPOTIFY_CLIENT_ID)")
        p.add_argument("--client-secret", default="", help="only for --auth app (or SPOTIFY_CLIENT_SECRET)")
        p.add_argument("--auth", choices=("user", "app"), default="user",
                       help="user: your account and its market (default). app: client credentials, "
                            "lets you name a market you are not in.")
        p.add_argument("--token-path", default=str(auth.DEFAULT_TOKEN_PATH))
        p.add_argument("--rate", type=float, default=1.0,
                       help="requests per second (default 1.0; be kind)")
        p.add_argument("--market", default="", help="ISO country code; only honoured with --auth app")

    p_login = sub.add_parser("login", help="sign in once with your Spotify account")
    common(p_login)
    p_login.add_argument("--redirect-uri", default=auth.DEFAULT_REDIRECT)
    p_login.add_argument("--no-browser", action="store_true",
                         help="don't try to open a browser; just print the URL")
    p_login.add_argument("--manual", action="store_true",
                         help="skip the local callback server entirely and paste the "
                              "redirect URL by hand")
    p_login.add_argument("--for-saving", action="store_true",
                         help="also ask for permission to modify your library, so "
                              "`spotifind save` works. Not needed for check/probe.")
    p_login.set_defaults(func=cmd_login)

    p_probe = sub.add_parser("probe", help="one request, to see whether this token sees audiobooks")
    common(p_probe)
    p_probe.add_argument("--query", default="Project Hail Mary Andy Weir")
    p_probe.set_defaults(func=cmd_probe)

    p_check = sub.add_parser("check", help="check a StoryGraph or Goodreads CSV export")
    common(p_check)
    p_check.add_argument("csv", help="path to the export")
    p_check.add_argument("--out-csv", default="")
    p_check.add_argument("--out-html", default="")
    p_check.add_argument("--cache", default=str(DEFAULT_CACHE))
    p_check.add_argument("--refresh", action="store_true", help="ignore cached answers")
    p_check.add_argument("--limit", type=int, default=0, help="only check the first N books")
    p_check.add_argument("--all-shelves", action="store_true",
                         help="do not filter to the to-read shelf")
    p_check.add_argument("--fallback", choices=FALLBACK_MODES, default="empty",
                         help="spend a second search on title-only: never / when the first came "
                              "back empty (default) / whenever nothing matched")
    p_check.add_argument("--prefer-language", default="en", metavar="CODE",
                         help="which edition to report when a book exists in several "
                              "languages (default en). Others are still counted.")
    p_check.add_argument("--hit-ttl", type=int, default=90, help="days to trust a found book")
    p_check.add_argument("--miss-ttl", type=int, default=14, help="days to trust a not-found book")
    p_check.add_argument("--dry-run", action="store_true", help="print the plan and stop")
    p_check.set_defaults(func=cmd_check)

    p_save = sub.add_parser(
        "save", help="add the books found by `check` to your Spotify library")
    common(p_save)
    p_save.add_argument("report", nargs="?", default="spotify-availability.csv",
                        help="the CSV written by `check` (default: ./spotify-availability.csv)")
    p_save.add_argument("--cache", default=str(DEFAULT_CACHE))
    p_save.add_argument("--include-probably", action="store_true",
                        help="also save 'probably' rows, not just confirmed ones")
    p_save.add_argument("--include-check", action="store_true",
                        help="also save 'check' rows — the ones whose author didn't match")
    p_save.add_argument("--limit", type=int, default=0, help="only save the first N")
    p_save.add_argument("--chunk", type=int, default=20,
                        help="how many to send per request (default 20)")
    p_save.add_argument("--no-skip", action="store_true",
                        help="don't check what's already in your library first")
    p_save.add_argument("--dry-run", action="store_true",
                        help="list what would be saved and stop")
    p_save.add_argument("--yes", action="store_true", help="skip the confirmation")
    p_save.add_argument("--undo", action="store_true",
                        help="remove everything spotifind added to your library")
    p_save.set_defaults(func=cmd_save)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except auth.AuthError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2
    except RateLimitAbort as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\nStopped. Everything checked so far is cached.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
