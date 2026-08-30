"""Rate limiting that is deliberately paranoid.

Spotify does not publish a number. What it documents is the *shape* of the
limit: "the number of calls that your app makes to Spotify in a rolling 30
second window", and a 429 carrying a ``Retry-After`` header when you cross it.

So this module models exactly that shape and then sits well under it:

* a rolling 30-second window with a hard cap (default 30 calls, i.e. 1/s),
* a minimum gap between consecutive calls, so a burst can never form,
* strictly one request in flight at a time (there is no concurrency anywhere
  in this project, on purpose),
* on a 429: honour ``Retry-After`` exactly, add a pad, and then *permanently
  halve the budget for the rest of the run* — a 429 is treated as evidence
  that the chosen rate was wrong, not as a speed bump,
* on repeated 429s: stop the run entirely. Backing off and continuing to
  knock is what gets an app's access pulled; the safe move is to give up and
  let the operator come back later. The cache makes that cheap.

The clock and the sleep function are injectable so the tests can run the whole
thing in zero wall-clock time and still assert on the timing decisions.
"""

from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque


# Waits shorter than this are treated as zero. Not cosmetic: at a clock value
# of ~600s, adding 2e-14 changes nothing a float can represent, so a loop that
# insists on sleeping it never terminates. Found by a test that idled for ten
# minutes and then tried to resume.
MIN_MEANINGFUL_WAIT = 1e-4

# If we ever go round the acquire loop this many times without being cleared
# to fire, something is wrong with the arithmetic and hanging is the worst
# possible response.
MAX_ACQUIRE_ITERATIONS = 1000


class RateLimitAbort(RuntimeError):
    """Raised when we have been told to slow down too many times.

    The run stops. It does not retry, and it does not fall back to a
    different endpoint or a different token. Progress is already in the
    cache, so resuming later costs nothing.
    """


@dataclass
class LimiterConfig:
    #: Hard cap on calls inside the rolling window.
    max_calls: int = 30
    #: Length of the rolling window, in seconds. Matches Spotify's own.
    window_seconds: float = 30.0
    #: Never fire two calls closer together than this.
    min_gap_seconds: float = 0.9
    #: Extra seconds added to any Retry-After we are handed.
    retry_after_pad: float = 2.0
    #: Ignore an absurd Retry-After rather than sleeping for an hour.
    retry_after_cap: float = 300.0
    #: Each consecutive 429 waits this many times longer than the last.
    #: Observed behaviour: Spotify's Retry-After can be shorter than the
    #: window that actually needs to drain, so retrying at exactly
    #: Retry-After produces another 429 immediately, and three of those in a
    #: row end the run over what was really one throttling event.
    penalty_escalation: float = 3.0
    #: However far the escalation goes, never sleep longer than this.
    max_penalty_seconds: float = 900.0
    #: Consecutive 429s that end the run. Four rather than three because the
    #: waits between them escalate: four attempts spread over ~90 seconds is
    #: markedly *more* patient than the three-over-14-seconds this used to be,
    #: and it survives a throttling window that simply needs a minute to
    #: drain — which is what a real 1,400-book run ran into.
    abort_after_consecutive_429: int = 4
    #: Total 429s in one run that end the run.
    abort_after_total_429: int = 8
    #: The budget is halved on each 429 but never falls below this.
    floor_calls: int = 6
    #: Random jitter added to every wait, so repeated runs do not align.
    jitter_seconds: float = 0.15

    def __post_init__(self) -> None:
        if self.max_calls < 1:
            raise ValueError("max_calls must be at least 1")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if self.floor_calls < 1:
            raise ValueError("floor_calls must be at least 1")
        if self.floor_calls > self.max_calls:
            self.floor_calls = self.max_calls

    @classmethod
    def from_rate(cls, calls_per_second: float, **kwargs) -> "LimiterConfig":
        """Build a config from a plain requests-per-second figure."""
        if calls_per_second <= 0:
            raise ValueError("calls_per_second must be positive")
        window = float(kwargs.pop("window_seconds", 30.0))
        max_calls = max(1, int(calls_per_second * window))
        gap = kwargs.pop("min_gap_seconds", max(0.0, (1.0 / calls_per_second) * 0.9))
        floor = kwargs.pop("floor_calls", max(1, min(6, max_calls)))
        return cls(
            max_calls=max_calls,
            window_seconds=window,
            min_gap_seconds=gap,
            floor_calls=floor,
            **kwargs,
        )


@dataclass
class LimiterStats:
    calls: int = 0
    waits: int = 0
    total_wait: float = 0.0
    hits_429: int = 0
    consecutive_429: int = 0
    budget_reductions: int = 0


@dataclass
class RateLimiter:
    config: LimiterConfig = field(default_factory=LimiterConfig)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=random.Random)

    _times: Deque[float] = field(default_factory=deque, init=False)
    _current_max: int = field(default=0, init=False)
    _blocked_until: float = field(default=0.0, init=False)
    stats: LimiterStats = field(default_factory=LimiterStats, init=False)

    def __post_init__(self) -> None:
        self._current_max = self.config.max_calls

    # -- introspection ----------------------------------------------------

    @property
    def current_max_calls(self) -> int:
        return self._current_max

    @property
    def effective_rate(self) -> float:
        """Calls per second the limiter is currently willing to allow."""
        return self._current_max / self.config.window_seconds

    # -- the two things a caller does -------------------------------------

    def acquire(self) -> None:
        """Block until it is safe to make one call. Records the call."""
        for _ in range(MAX_ACQUIRE_ITERATIONS):
            now = self.clock()
            self._evict(now)
            waits = []

            if self._blocked_until > now:
                waits.append(self._blocked_until - now)

            if self._times:
                gap_wait = self.config.min_gap_seconds - (now - self._times[-1])
                if gap_wait > 0:
                    waits.append(gap_wait)

            if len(self._times) >= self._current_max:
                # Wait until the oldest call falls out of the window.
                waits.append(self._times[0] + self.config.window_seconds - now)

            wait = max(waits) if waits else 0.0
            if wait < MIN_MEANINGFUL_WAIT:
                self._times.append(now)
                self.stats.calls += 1
                return

            wait += self.rng.uniform(0, self.config.jitter_seconds)
            self.stats.waits += 1
            self.stats.total_wait += wait
            self.sleep(wait)

        raise RuntimeError(
            "Rate limiter failed to reach a firing decision — refusing to spin. "
            "This is a bug; please report it rather than removing the limiter."
        )

    def penalise(self, retry_after: float | None) -> float:
        """Record a 429 and block the limiter for the right amount of time.

        Returns the number of seconds slept. Raises :class:`RateLimitAbort`
        once the run has been told to slow down too often.
        """
        self.stats.hits_429 += 1
        self.stats.consecutive_429 += 1

        # A 429 means the rate was wrong, not that this one call was unlucky.
        new_max = max(self.config.floor_calls, self._current_max // 2)
        if new_max < self._current_max:
            self._current_max = new_max
            self.stats.budget_reductions += 1

        if retry_after is None or retry_after < 0:
            # No header: fall back to a full window, which is the longest
            # period Spotify's own accounting can be looking at.
            delay = self.config.window_seconds
        else:
            delay = min(float(retry_after), self.config.retry_after_cap)
        delay += self.config.retry_after_pad

        # Escalate on repeats. Retrying at exactly Retry-After and being
        # refused again is not three separate refusals — it is one, met with
        # too little patience. Waiting longer each time gives the real window
        # a chance to drain before the run gives up on itself.
        if self.stats.consecutive_429 > 1:
            delay *= self.config.penalty_escalation ** (self.stats.consecutive_429 - 1)
        delay = min(delay, self.config.max_penalty_seconds)
        delay += self.rng.uniform(0, self.config.jitter_seconds)

        now = self.clock()
        self._blocked_until = max(self._blocked_until, now + delay)
        # Everything inside the window is water under the bridge once we have
        # been throttled; start the window fresh after the penalty.
        self._times.clear()

        self.stats.total_wait += delay
        self.sleep(delay)

        if self.stats.consecutive_429 >= self.config.abort_after_consecutive_429:
            raise RateLimitAbort(
                f"{self.stats.consecutive_429} consecutive 429 responses from "
                "Spotify, over roughly a minute and a half of waiting — stopping "
                "rather than continuing to knock.\n"
                "Everything checked so far is cached, so a re-run only covers "
                "what is left. Wait an hour or so, then re-run with a lower "
                "rate, e.g. --rate 0.5."
            )
        if self.stats.hits_429 >= self.config.abort_after_total_429:
            raise RateLimitAbort(
                f"{self.stats.hits_429} rate-limit responses in one run — "
                "stopping. Progress is cached; try again later at a lower rate."
            )
        return delay

    def note_success(self) -> None:
        """Reset the consecutive-429 counter after a clean response."""
        self.stats.consecutive_429 = 0

    # -- internals --------------------------------------------------------

    def _evict(self, now: float) -> None:
        cutoff = now - self.config.window_seconds
        while self._times and self._times[0] <= cutoff:
            self._times.popleft()


class FakeClock:
    """A monotonic clock whose only way of advancing is being slept on.

    Used by the tests: a run of thousands of calls takes microseconds while
    still exercising every real timing decision.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise AssertionError("negative sleep")
        self.slept.append(seconds)
        self.now += seconds

    @property
    def total_slept(self) -> float:
        return sum(self.slept)
