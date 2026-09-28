"""The run loop: books in, matches out, cache in the middle."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from .budget import BudgetExhausted
from .cache import Cache
from .csvimport import Book
from .matching import Candidate, Match, best_match
from .ratelimit import RateLimitAbort
from .spotify import Forbidden, SpotifyClient, SpotifyError

# When to spend a second request on a title-only search.
#   never  — one request per book, always
#   empty  — only when "title author" came back with nothing at all
#   always — whenever the first search produced no acceptable match
FALLBACK_MODES = ("never", "empty", "always")


@dataclass
class BookResult:
    book: Book
    match: Match
    from_cache: bool = False
    newly_found: bool = False
    error: str = ""
    searches: int = 0

    @property
    def found(self) -> bool:
        return self.match.found and not self.error


@dataclass
class RunSummary:
    results: list[BookResult] = field(default_factory=list)
    status: str = "ok"          # ok | aborted | failed
    note: str = ""
    searched: int = 0
    from_cache: int = 0
    errors: int = 0
    #: Books left unchecked because the run stopped before reaching them and
    #: the cache had no fresh answer. They are not in ``results``.
    unchecked: int = 0
    #: True when the stop was the daily budget, not a refusal from Spotify.
    stopped_by_budget: bool = False

    @property
    def strong(self) -> list[BookResult]:
        return [r for r in self.results if r.match.confidence == "strong" and not r.error]

    @property
    def likely(self) -> list[BookResult]:
        return [r for r in self.results if r.match.confidence == "likely" and not r.error]

    @property
    def unconfirmed(self) -> list[BookResult]:
        return [r for r in self.results if r.match.confidence == "unconfirmed" and not r.error]

    @property
    def missing(self) -> list[BookResult]:
        return [r for r in self.results if r.match.confidence == "none" and not r.error]

    @property
    def failed(self) -> list[BookResult]:
        return [r for r in self.results if r.error]

    @property
    def newly_found(self) -> list[BookResult]:
        return [r for r in self.results if r.newly_found]


def estimate_requests(books: Sequence[Book], cache: Cache, market: str, *,
                      refresh: bool = False, fallback: str = "empty") -> tuple[int, int]:
    """(minimum, maximum) requests this run will make. For the pre-flight."""
    if refresh:
        to_check = len(books)
    else:
        to_check = 0
        for book in books:
            entry = cache.get(book.key, market)
            if not (entry and cache.is_fresh(entry)):
                to_check += 1
    if fallback == "never":
        return to_check, to_check
    return to_check, to_check * 2


def check_books(
    books: Iterable[Book],
    client: SpotifyClient,
    cache: Cache,
    market: str,
    *,
    refresh: bool = False,
    fallback: str = "empty",
    prefer_language: str = "en",
    on_progress: Callable[[int, BookResult], None] | None = None,
) -> RunSummary:
    if fallback not in FALLBACK_MODES:
        raise ValueError(f"fallback must be one of {FALLBACK_MODES}")

    summary = RunSummary()
    books = list(books)

    # On the very first run for a market everything found is "new", which is
    # true and useless — it would flag the whole report. Only a run that has
    # something to compare against can report changes.
    first_run = cache.count_for_market(market) == 0
    # Once the run has stopped asking Spotify, it still walks the rest of the
    # list so the report includes every book the cache already knows about.
    # A budget stop is routine, and a report that silently dropped hundreds of
    # answered books would make every partial day look worse than it is.
    stopped = False

    for index, book in enumerate(books, start=1):
        if not refresh:
            entry = cache.get(book.key, market)
            if entry and cache.is_fresh(entry):
                result = BookResult(book=book, match=entry.match, from_cache=True)
                summary.results.append(result)
                summary.from_cache += 1
                if on_progress:
                    on_progress(index, result)
                continue

        if stopped:
            summary.unchecked += 1
            continue

        try:
            result = _lookup(book, client, cache, market, fallback=fallback,
                             first_run=first_run, prefer_language=prefer_language)
        except RateLimitAbort as exc:
            summary.status = "aborted"
            summary.note = str(exc)
            summary.stopped_by_budget = isinstance(exc, BudgetExhausted)
            summary.unchecked += 1
            stopped = True
            continue
        except Forbidden as exc:
            # This is never a one-book problem; stop rather than repeat it
            # 1,400 times.
            summary.status = "failed"
            summary.note = str(exc)
            summary.unchecked += 1
            stopped = True
            continue
        except SpotifyError as exc:
            result = BookResult(book=book, match=Match(None, "none", 0.0, False), error=str(exc))
            summary.errors += 1

        summary.results.append(result)
        summary.searched += result.searches
        if on_progress:
            on_progress(index, result)

    return summary


def _lookup(book: Book, client: SpotifyClient, cache: Cache, market: str, *,
            fallback: str, first_run: bool = False,
            prefer_language: str = "en") -> BookResult:
    candidates: list[Candidate] = client.search_audiobooks(book.query)
    searches = 1
    match = best_match(book.title, book.authors, candidates,
                       prefer_language=prefer_language)

    should_retry = (
        fallback != "never"
        and book.authors
        and book.query.strip().lower() != book.title.strip().lower()
        and (
            (fallback == "empty" and not candidates)
            or (fallback == "always" and match.confidence == "none")
        )
    )
    if should_retry:
        extra = client.search_audiobooks(book.title)
        searches += 1
        if extra:
            candidates = candidates + extra
            match = best_match(book.title, book.authors, candidates,
                               prefer_language=prefer_language)

    newly = cache.put(
        book_key=book.key,
        market=market,
        title=book.title,
        authors=book.author_display,
        query=book.query,
        match=match,
        candidates=candidates,
    )
    return BookResult(book=book, match=match, newly_found=newly and not first_run,
                      searches=searches)
