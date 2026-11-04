"""A circuit breaker for the synchronous call to portfolio-service (ADR-0011, supporting rule 2).

> **Circuit breaker:** like the switch in a fuse box. After repeated failures it "opens" and calls
> fail immediately, instead of every request waiting for a dependency that is down. After a pause it
> lets one trial call through ("half-open"); success closes it again, failure opens it again.

Ours: 5 consecutive failures open it for 10 s; then one trial call decides. While it is open,
order placement answers 503 DEPENDENCY_UNAVAILABLE at once. Only failures of the dependency itself
count (a timeout, a connection error, a 5xx); a business answer such as 422 INSUFFICIENT_FUNDS is a
success, because portfolio-service did its job.
"""

import time
from collections.abc import Callable
from enum import StrEnum

FAILURE_THRESHOLD = 5
OPEN_SECONDS = 10.0


class BreakerState(StrEnum):
    CLOSED = "closed"  # calls go through
    OPEN = "open"  # calls fail fast
    HALF_OPEN = "half_open"  # one trial call is allowed


class CircuitOpenError(Exception):
    """The breaker is open: the call was not even attempted."""


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = FAILURE_THRESHOLD,
        open_seconds: float = OPEN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        on_state_change: Callable[[BreakerState], None] = lambda state: None,
        on_trip: Callable[[], None] = lambda: None,
    ) -> None:
        self._threshold = failure_threshold
        self._open_seconds = open_seconds
        self._clock = clock
        self._on_state_change = on_state_change
        self._on_trip = on_trip
        self._failures = 0
        self._opened_at = 0.0
        self._trial_running = False
        self.state = BreakerState.CLOSED
        self.trips = 0
        on_state_change(self.state)

    def _move(self, state: BreakerState) -> None:
        if state is not self.state:
            self.state = state
            self._on_state_change(state)

    def before_call(self) -> None:
        """Raise CircuitOpenError instead of calling, while open (or while a trial is running)."""
        if self.state is BreakerState.OPEN:
            if self._clock() - self._opened_at < self._open_seconds:
                raise CircuitOpenError("portfolio-service circuit is open")
            self._move(BreakerState.HALF_OPEN)
        if self.state is BreakerState.HALF_OPEN:
            if self._trial_running:
                raise CircuitOpenError("portfolio-service circuit is half-open: a trial is running")
            self._trial_running = True

    def succeeded(self) -> None:
        self._failures = 0
        self._trial_running = False
        self._move(BreakerState.CLOSED)

    def failed(self) -> None:
        self._trial_running = False
        self._failures += 1
        if self.state is BreakerState.HALF_OPEN or self._failures >= self._threshold:
            self._opened_at = self._clock()
            self._failures = 0
            self.trips += 1
            self._on_trip()
            self._move(BreakerState.OPEN)
