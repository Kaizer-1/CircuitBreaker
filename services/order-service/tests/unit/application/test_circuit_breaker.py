"""The circuit breaker around A-06 (ADR-0011): 5 failures open it for 10 s; one trial decides."""

import pytest

from order_service.application.circuit_breaker import BreakerState, CircuitBreaker, CircuitOpenError


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def breaker(clock: Clock, states: list[BreakerState] | None = None) -> CircuitBreaker:
    seen = states if states is not None else []
    return CircuitBreaker(clock=clock, on_state_change=seen.append)


def fail(b: CircuitBreaker, times: int) -> None:
    for _ in range(times):
        b.before_call()
        b.failed()


def test_it_starts_closed_and_four_failures_do_not_open_it(clock: Clock) -> None:
    b = breaker(clock)
    fail(b, 4)
    assert b.state is BreakerState.CLOSED
    b.before_call()  # still lets calls through


def test_a_success_resets_the_failure_count(clock: Clock) -> None:
    b = breaker(clock)
    fail(b, 4)
    b.before_call()
    b.succeeded()
    fail(b, 4)
    assert b.state is BreakerState.CLOSED


def test_five_consecutive_failures_open_it_and_calls_fail_fast(clock: Clock) -> None:
    states: list[BreakerState] = []
    b = breaker(clock, states)
    fail(b, 5)
    assert (b.state, b.trips) == (BreakerState.OPEN, 1)
    with pytest.raises(CircuitOpenError):
        b.before_call()
    assert states == [BreakerState.CLOSED, BreakerState.OPEN]


def test_after_10_seconds_one_trial_call_is_allowed(clock: Clock) -> None:
    b = breaker(clock)
    fail(b, 5)
    clock.now += 9.9
    with pytest.raises(CircuitOpenError):
        b.before_call()
    clock.now += 0.1
    b.before_call()  # the trial
    assert b.state is BreakerState.HALF_OPEN
    with pytest.raises(CircuitOpenError, match="trial"):
        b.before_call()  # only one at a time


def test_a_successful_trial_closes_it(clock: Clock) -> None:
    states: list[BreakerState] = []
    b = breaker(clock, states)
    fail(b, 5)
    clock.now += 10
    b.before_call()
    b.succeeded()
    assert b.state is BreakerState.CLOSED
    assert states == [
        BreakerState.CLOSED,
        BreakerState.OPEN,
        BreakerState.HALF_OPEN,
        BreakerState.CLOSED,
    ]


def test_a_failed_trial_opens_it_again_for_another_10_seconds(clock: Clock) -> None:
    trips: list[int] = []
    b = CircuitBreaker(clock=clock, on_trip=lambda: trips.append(1))
    fail(b, 5)
    clock.now += 10
    b.before_call()
    b.failed()
    assert (b.state, b.trips, len(trips)) == (BreakerState.OPEN, 2, 2)
    clock.now += 5
    with pytest.raises(CircuitOpenError):
        b.before_call()
