"""Rate limits (system architecture, cross-cutting concerns): a token bucket per caller.

> **Token bucket:** each caller has a bucket that refills at `rate` tokens per second, up to `rate`
> tokens. Each request takes one; an empty bucket means 429. It allows short bursts but caps the
> average rate.

In memory, per gateway replica: with 2 replicas a trader can reach twice the limit. That is enough
to stop a runaway client in a demo; a shared limit would need a shared store (out of scope).
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

TRADER_PER_SECOND = 20
BOT_PER_SECOND = 50
ANONYMOUS_PER_SECOND = 20  # per client IP, for the session endpoints


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic, max_keys: int = 50_000) -> None:
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._max_keys = max_keys

    def check(self, key: str, rate: float) -> int | None:
        """Take one token. None if allowed; otherwise the seconds to wait (Retry-After)."""
        now = self._clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self._max_keys:
                self._buckets.clear()  # crude bound on memory; only resets limits early
            bucket = self._buckets[key] = _Bucket(rate, now)
        bucket.tokens = min(rate, bucket.tokens + (now - bucket.updated) * rate)
        bucket.updated = now
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return None
        return max(1, math.ceil((1 - bucket.tokens) / rate))
