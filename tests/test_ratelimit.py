"""The rate limiter is the part of this project that can do real damage.

Every test here runs on a fake clock, so a thousand simulated requests take
no wall-clock time while still exercising the real timing arithmetic.
"""

from __future__ import annotations

import random

import pytest

from spotifind.ratelimit import (
    FakeClock,
    LimiterConfig,
    RateLimitAbort,
    RateLimiter,
)


def make(config: LimiterConfig | None = None) -> tuple[RateLimiter, FakeClock]:
    clock = FakeClock()
    limiter = RateLimiter(
        config=config or LimiterConfig(),
        clock=clock.time,
        sleep=clock.sleep,
        rng=random.Random(0),
    )
    return limiter, clock


def max_in_any_window(times: list[float], window: float) -> int:
    """The most calls that ever fall inside one rolling window."""
    worst = 0
    for i, start in enumerate(times):
        count = sum(1 for t in times[i:] if t < start + window)
        worst = max(worst, count)
    return worst


def test_never_exceeds_the_window_budget():
    limiter, clock = make(LimiterConfig(max_calls=30, window_seconds=30.0, min_gap_seconds=0.0,
                                        jitter_seconds=0.0))
    times = []
    for _ in range(500):
        limiter.acquire()
        times.append(clock.now)
    assert max_in_any_window(times, 30.0) <= 30


def test_minimum_gap_is_respected():
    limiter, clock = make(LimiterConfig(min_gap_seconds=1.0, jitter_seconds=0.0))
    times = []
    for _ in range(50):
        limiter.acquire()
        times.append(clock.now)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert min(gaps) >= 1.0


def test_default_config_is_about_one_per_second():
    limiter, clock = make()
    for _ in range(120):
        limiter.acquire()
    # 120 calls should take roughly two minutes, not two seconds.
    assert clock.now == pytest.approx(120, abs=15)


def test_from_rate_builds_a_consistent_config():
    config = LimiterConfig.from_rate(0.5)
    assert config.max_calls == 15          # 0.5/s over a 30s window
    assert config.min_gap_seconds == pytest.approx(1.8)
    limiter, clock = make(config)
    for _ in range(30):
        limiter.acquire()
    assert clock.now == pytest.approx(60, abs=8)


def test_429_sleeps_for_retry_after_plus_a_pad():
    limiter, clock = make(LimiterConfig(retry_after_pad=2.0, jitter_seconds=0.0))
    slept = limiter.penalise(11)
    assert slept == pytest.approx(13.0)
    assert clock.total_slept == pytest.approx(13.0)


def test_429_without_a_header_waits_a_whole_window():
    limiter, clock = make(LimiterConfig(window_seconds=30.0, retry_after_pad=2.0, jitter_seconds=0.0))
    assert limiter.penalise(None) == pytest.approx(32.0)


def test_absurd_retry_after_is_capped():
    limiter, _ = make(LimiterConfig(retry_after_cap=300.0, retry_after_pad=0.0, jitter_seconds=0.0))
    assert limiter.penalise(86400) == pytest.approx(300.0)


def test_consecutive_429s_wait_longer_each_time():
    """Retrying at exactly Retry-After and being refused again is one
    throttling event met with too little patience, not two refusals."""
    limiter, _ = make(LimiterConfig(retry_after_pad=0.0, jitter_seconds=0.0,
                                    penalty_escalation=3.0,
                                    abort_after_consecutive_429=99,
                                    abort_after_total_429=99))
    first = limiter.penalise(10)
    second = limiter.penalise(10)
    third = limiter.penalise(10)
    assert (first, second, third) == pytest.approx((10.0, 30.0, 90.0))


def test_escalation_resets_after_a_clean_response():
    limiter, _ = make(LimiterConfig(retry_after_pad=0.0, jitter_seconds=0.0,
                                    abort_after_consecutive_429=99,
                                    abort_after_total_429=99))
    limiter.penalise(10)
    limiter.penalise(10)
    limiter.note_success()
    assert limiter.penalise(10) == pytest.approx(10.0)


def test_escalation_is_capped():
    limiter, _ = make(LimiterConfig(retry_after_pad=0.0, jitter_seconds=0.0,
                                    max_penalty_seconds=120.0,
                                    abort_after_consecutive_429=99,
                                    abort_after_total_429=99))
    for _ in range(8):
        delay = limiter.penalise(60)
    assert delay == pytest.approx(120.0)


def test_429_permanently_slows_the_run_down():
    limiter, _ = make(LimiterConfig(max_calls=30, floor_calls=6))
    before = limiter.effective_rate
    limiter.penalise(1)
    limiter.note_success()
    assert limiter.current_max_calls == 15
    assert limiter.effective_rate < before
    limiter.penalise(1)
    limiter.note_success()
    assert limiter.current_max_calls == 7


def test_the_budget_never_falls_below_the_floor():
    limiter, _ = make(LimiterConfig(max_calls=30, floor_calls=6,
                                    abort_after_consecutive_429=99, abort_after_total_429=99))
    for _ in range(12):
        limiter.penalise(1)
        limiter.note_success()
    assert limiter.current_max_calls == 6


def test_three_consecutive_429s_end_the_run():
    limiter, _ = make(LimiterConfig(abort_after_consecutive_429=3))
    limiter.penalise(1)
    limiter.penalise(1)
    with pytest.raises(RateLimitAbort) as excinfo:
        limiter.penalise(1)
    assert "consecutive" in str(excinfo.value)


def test_a_success_resets_the_consecutive_counter():
    limiter, _ = make(LimiterConfig(abort_after_consecutive_429=3, abort_after_total_429=99))
    limiter.penalise(1)
    limiter.penalise(1)
    limiter.note_success()          # one clean response in between
    limiter.penalise(1)             # would have been the third in a row
    assert limiter.stats.consecutive_429 == 1


def test_scattered_429s_still_end_the_run_eventually():
    limiter, _ = make(LimiterConfig(abort_after_consecutive_429=3, abort_after_total_429=4))
    for _ in range(3):
        limiter.penalise(1)
        limiter.note_success()
    with pytest.raises(RateLimitAbort) as excinfo:
        limiter.penalise(1)
    assert "one run" in str(excinfo.value)


def test_the_penalty_actually_blocks_the_next_call():
    limiter, clock = make(LimiterConfig(jitter_seconds=0.0, retry_after_pad=2.0))
    limiter.acquire()
    start = clock.now
    limiter.penalise(20)            # sleeps 22
    limiter.note_success()
    limiter.acquire()
    assert clock.now - start >= 22.0


def test_a_burst_cannot_form_after_the_window_empties():
    """The classic failure: sit idle, then fire everything at once."""
    limiter, clock = make(LimiterConfig(max_calls=30, window_seconds=30.0,
                                        min_gap_seconds=0.9, jitter_seconds=0.0))
    limiter.acquire()
    clock.sleep(600)                # a long pause; the window is empty
    times = []
    for _ in range(10):
        limiter.acquire()
        times.append(clock.now)
    gaps = [b - a for a, b in zip(times, times[1:])]
    # The tolerance is MIN_MEANINGFUL_WAIT: sub-0.1ms shortfalls are float
    # noise at a clock value of 600, not a burst.
    assert min(gaps) >= 0.9 - 1e-4, "min_gap must hold even from a standing start"


def test_acquire_refuses_to_spin_forever():
    """A limiter that cannot decide must raise, not hang the run."""
    class StuckClock:
        def time(self) -> float:
            return 1000.0

        def sleep(self, seconds: float) -> None:
            pass  # a clock that never advances

    stuck = StuckClock()
    limiter = RateLimiter(
        config=LimiterConfig(max_calls=1, window_seconds=30.0, jitter_seconds=0.0),
        clock=stuck.time,
        sleep=stuck.sleep,
    )
    limiter.acquire()
    with pytest.raises(RuntimeError, match="refusing to spin"):
        limiter.acquire()


def test_config_validation():
    with pytest.raises(ValueError):
        LimiterConfig(max_calls=0)
    with pytest.raises(ValueError):
        LimiterConfig(window_seconds=0)
    with pytest.raises(ValueError):
        LimiterConfig.from_rate(0)
    # A floor above the ceiling is clamped rather than rejected.
    assert LimiterConfig(max_calls=4, floor_calls=99).floor_calls == 4
