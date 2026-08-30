"""Simulate a real-sized run: `python tests/bench.py [books] [rate]`.

Counts requests and reports the wall-clock time the same run would take
against the real API, on a fake clock so the simulation itself is instant.
"""

from __future__ import annotations

import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_spotify import MockBook, MockSpotify  # noqa: E402

from spotifind.auth import static_token_source  # noqa: E402
from spotifind.cache import Cache  # noqa: E402
from spotifind.checker import check_books  # noqa: E402
from spotifind.csvimport import Book  # noqa: E402
from spotifind.ratelimit import FakeClock, LimiterConfig, RateLimiter  # noqa: E402
from spotifind.spotify import SpotifyClient  # noqa: E402

WORDS = ("silver hollow ash tide lantern ember quiet garden winter salt "
         "north river glass thorn harbour amber stone field").split()


def make_books(n: int) -> list[Book]:
    rng = random.Random(7)
    books, seen = [], set()
    while len(books) < n:
        title = " ".join(rng.sample(WORDS, 3)).title()
        author = f"{rng.choice(WORDS).title()} {rng.choice(WORDS).title()}"
        key = (title, author)
        if key in seen:
            continue
        seen.add(key)
        books.append(Book(title, [author]))
    return books


def run(n: int, rate: float, hit_fraction: float = 0.15) -> None:
    books = make_books(n)
    rng = random.Random(11)
    on_spotify = rng.sample(books, int(n * hit_fraction))
    catalogue = [MockBook(b.title, b.authors) for b in on_spotify]

    for label, refresh in (("cold", False), ("warm", False)):
        mock = MockSpotify(catalogue)
        clock = FakeClock()
        limiter = RateLimiter(config=LimiterConfig.from_rate(rate),
                              clock=clock.time, sleep=clock.sleep, rng=random.Random(0))
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "c.sqlite3"
            if label == "warm":
                # Prime the cache with a cold run first.
                warm_clock = FakeClock()
                warm_limiter = RateLimiter(config=LimiterConfig.from_rate(rate),
                                           clock=warm_clock.time, sleep=warm_clock.sleep)
                with Cache(cache_path) as cache, SpotifyClient(
                    static_token_source("t"), warm_limiter,
                    transport=MockSpotify(catalogue).transport, sleep=warm_clock.sleep
                ) as client:
                    check_books(books, client, cache, "US")

            with Cache(cache_path) as cache, SpotifyClient(
                static_token_source("t"), limiter, transport=mock.transport, sleep=clock.sleep
            ) as client:
                summary = check_books(books, client, cache, "US", refresh=refresh)
                requests = client.stats.requests

        minutes = clock.now / 60
        print(f"  {label:5} {requests:6} requests  {minutes:7.1f} min  "
              f"{len(summary.strong):5} found  {summary.from_cache:5} cached")


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 1400
    rate = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
    print(f"\n{count} books at {rate:g} requests/second\n")
    run(count, rate)
    print()
