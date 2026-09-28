"""A daily request budget, because Spotify's real limit is a daily count.

Spotify documents its rate limit as a rolling 30-second window. That is not
the limit that bites. Two real runs of this tool against a Development Mode
app were cut off after 682 and 697 search requests — the first at one request
a second, the second at half that — each time with a Retry-After of most of a
day. Going slower bought nothing: the limit is a count of requests over about
24 hours.

So every request this tool sends is written down in the cache database, and
before sending another the budget counts how many went out in the last 24
hours. At the budget (600 by default, comfortably under the ~700 observed) the
run stops by itself, says when the budget frees up, and leaves everything
checked so far in the cache for the next run to pick up. Spotify never has to
say no.

The window is rolling rather than a calendar day. The 697-request run was
told to come back almost exactly 24 hours after its first request, which a
rolling window explains; and staying under the budget in *every* 24-hour
window also keeps under it for any fixed window Spotify might use instead.

A Retry-After measured in hours is written down too. Until it has passed, the
tool refuses to contact Spotify at all: knocking on a door that has been
closed for the day is the behaviour most likely to get an app's access
pulled.

The clock here is wall time, not monotonic time, because the ledger has to
mean the same thing to tomorrow's run as it does to today's.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Protocol

from .ratelimit import RateLimitAbort

#: Default requests allowed in any 24 hours. Spotify cut a Development Mode
#: app off at 682 and 697; 600 leaves room for error in that estimate.
DEFAULT_DAILY_BUDGET = 600

#: The observed quota window.
WINDOW_SECONDS = 24 * 3600.0

#: A run that finds the budget full waits for the next free slot if it is
#: at most this far away, instead of stopping. The day after a big run the
#: budget comes back one request at a time, as the previous day's requests
#: age out, and a run started in the middle of that should pace itself to
#: the trickle rather than stop after a single request.
MAX_WAIT_SECONDS = 120.0

#: A Retry-After longer than this is a closed door, not a throttle, and is
#: remembered across runs. Matches the rate limiter's retry_after_cap, the
#: point past which it stops waiting and aborts.
LONG_BLOCK_SECONDS = 300.0


class BudgetExhausted(RateLimitAbort):
    """The daily budget is spent, or Spotify has closed the door for now.

    A subclass of :class:`RateLimitAbort` so every caller that already stops
    cleanly on a rate-limit abort stops cleanly on this too.
    """


class RequestLedger(Protocol):
    """Where the budget keeps its records. :class:`~spotifind.cache.Cache` is one."""

    def record_request(self, at: float) -> None: ...

    def request_times_since(self, since: float) -> list[float]: ...

    def blocked_until(self) -> float: ...

    def set_blocked_until(self, at: float) -> None: ...


@dataclass
class DailyBudget:
    ledger: RequestLedger
    limit: int = DEFAULT_DAILY_BUDGET
    window_seconds: float = WINDOW_SECONDS
    block_threshold: float = LONG_BLOCK_SECONDS
    max_wait_seconds: float = MAX_WAIT_SECONDS
    clock: Callable[[], float] = field(default=time.time)
    sleep: Callable[[float], None] = field(default=time.sleep)

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("the daily budget must be at least 1 request")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")

    # -- asking ------------------------------------------------------------

    def _times(self, now: float) -> list[float]:
        return sorted(self.ledger.request_times_since(now - self.window_seconds))

    def used(self) -> int:
        """Requests sent in the last 24 hours."""
        return len(self._times(self.clock()))

    def blocked_until(self) -> float | None:
        """When Spotify's last long Retry-After runs out, if it has not yet."""
        until = float(self.ledger.blocked_until() or 0.0)
        return until if until > self.clock() else None

    def remaining(self) -> int:
        """Requests that may go out right now without breaking the budget."""
        if self.blocked_until() is not None:
            return 0
        return max(0, self.limit - self.used())

    def available_at(self) -> float | None:
        """When the next request may go out, or None if it may go out now."""
        now = self.clock()
        waits: list[float] = []
        blocked = self.blocked_until()
        if blocked is not None:
            waits.append(blocked)
        times = self._times(now)
        excess = len(times) - self.limit
        if excess >= 0:
            # Enough of the oldest requests have to age out to bring the
            # count below the limit; the last of those decides.
            waits.append(times[excess] + self.window_seconds)
        return max(waits) if waits else None

    def refuses_now(self) -> bool:
        """Would :meth:`spend` refuse right now, rather than send or briefly wait?"""
        at = self.available_at()
        if at is None:
            return False
        return self.blocked_until() is not None or at - self.clock() > self.max_wait_seconds

    def fully_free_at(self) -> float | None:
        """When every request now in the window has aged out (and any block ended)."""
        now = self.clock()
        waits: list[float] = []
        blocked = self.blocked_until()
        if blocked is not None:
            waits.append(blocked)
        times = self._times(now)
        if times:
            waits.append(times[-1] + self.window_seconds)
        return max(waits) if waits else None

    # -- spending ----------------------------------------------------------

    def spend(self) -> None:
        """Record one request about to be sent, or refuse if there is no room.

        Waits instead of refusing when the next slot is less than
        ``max_wait_seconds`` away — but never waits out a refusal from
        Spotify itself.
        """
        for _ in range(3):
            at = self.available_at()
            if at is None:
                self.ledger.record_request(self.clock())
                return
            wait = at - self.clock()
            if self.blocked_until() is not None or wait > self.max_wait_seconds:
                break
            # A hair past the slot, so the oldest request is out of the window.
            self.sleep(max(0.0, wait) + 0.05)
        raise BudgetExhausted(self._refusal(at))

    def note_retry_after(self, retry_after: float | None) -> None:
        """Remember a Retry-After long enough to outlive this run."""
        if retry_after is not None and retry_after > self.block_threshold:
            self.ledger.set_blocked_until(self.clock() + float(retry_after))

    # -- explaining --------------------------------------------------------

    def status_line(self) -> str:
        """One line for a pre-flight: how much is left and when more frees up."""
        at = self.available_at()
        if at is not None:
            if self.blocked_until() is not None and self.used() < self.limit:
                return (f"Spotify has refused this app until {when(at, self.clock())}; "
                        "nothing will be sent before then.")
            return (f"Daily budget spent ({self.used()} requests in the last 24 hours, "
                    f"budget {self.limit}). {self._freeing(at)}")
        left = self.remaining()
        line = f"Daily budget: {left} of {self.limit} requests left for the next 24 hours"
        times = self._times(self.clock())
        if times and left < self.limit:
            line += f"; the oldest frees up at {when(times[0] + self.window_seconds, self.clock())}"
        return line + "."

    def _refusal(self, at: float) -> str:
        now = self.clock()
        used = self.used()
        blocked = self.blocked_until()
        if blocked is not None and blocked >= at:
            return (
                f"Spotify refused this app until {when(at, now)}, and this tool "
                "remembers that between runs — it will not contact Spotify before "
                "then. Everything checked so far is cached; re-run after that time."
            )
        return (
            f"Daily request budget reached: {used} requests to Spotify in the last "
            f"24 hours, against a budget of {self.limit}. Spotify cuts this kind of "
            "app off at about 700 a day, so the run stops here rather than finding "
            f"out the hard way. {self._freeing(at)} "
            "Everything checked so far is cached; re-run then and it picks up "
            "where it stopped."
        )

    def _freeing(self, at: float) -> str:
        now = self.clock()
        full = self.fully_free_at()
        if full is None or full - at < 60:
            return f"It frees up at {when(at, now)}."
        return (f"It frees up gradually from {when(at, now)}, one request at a time "
                f"as older ones age out, and is fully back by {when(full, now)}.")


def when(at: float, now: float) -> str:
    """'Sun 27 Sep 17:22 (in 23h 18m)', in local time."""
    stamp = datetime.fromtimestamp(at).strftime("%a %d %b %H:%M")
    delta = max(0, int(at - now))
    hours, rest = divmod(delta, 3600)
    minutes = rest // 60
    if hours:
        rel = f"in {hours}h {minutes:02d}m"
    elif minutes:
        rel = f"in {minutes}m"
    else:
        rel = "in under a minute"
    return f"{stamp} ({rel})"
