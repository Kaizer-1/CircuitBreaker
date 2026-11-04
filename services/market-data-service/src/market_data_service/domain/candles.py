"""Candles (OHLCV) built from trades by event time (docs/03-architecture/08-read-side.md).

- A trade belongs to the bucket that contains its `executed_at`: [start, start + interval), with
  bucket starts aligned to the Unix epoch (so 1m buckets start on the minute).
- A candle's open is the first trade's price in sequence order, its close the last; high and low
  are the extremes; volume and trade count add up.
- A bucket with no trade is **flat** at the previous close (open = high = low = close, volume 0).
  Before the first trade ever, the previous close is the reference price.
- A series starts at `since` and has every bucket from then on, so charts and bots see no gaps.

`CandleSeries` builds candles live: trades move event time forward, and the clock closes buckets
when no trade does. A trade may arrive for a bucket the clock already closed (the engine
republishing after a crash): the series then corrects that candle and the flat ones after it.
`rebuild` derives the same candles from the trades alone; a property test checks they agree.
All times are integer milliseconds since the Unix epoch.
"""

from bisect import bisect_left, insort
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

INTERVALS: dict[str, int] = {"5s": 5_000, "1m": 60_000, "5m": 300_000}
KEEP_BUCKETS = 1_000  # per series in memory: about 83 minutes of 5s candles


def bucket_of(at_ms: int, interval_ms: int) -> int:
    return at_ms - at_ms % interval_ms


@dataclass(frozen=True)
class Candle:
    bucket_start_ms: int
    open_paise: int
    high_paise: int
    low_paise: int
    close_paise: int
    volume: int
    trade_count: int


@dataclass
class _Trades:
    """What the trades of one bucket add up to."""

    first: int
    high: int
    low: int
    last: int
    volume: int
    count: int


class CandleSeries:
    """One ticker's candles for one interval."""

    def __init__(
        self, interval_ms: int, since_ms: int, prior_close: int, *, keep: int = KEEP_BUCKETS
    ) -> None:
        self.interval = interval_ms
        self._since = bucket_of(since_ms, interval_ms)
        self._prior = prior_close  # the close just before `since`
        self._open = self._since  # the forming bucket; every earlier one is closed
        self._trades: dict[int, _Trades] = {}
        self._keys: list[int] = []  # buckets with trades, sorted
        self._keep = keep

    @classmethod
    def restore(
        cls, interval_ms: int, candles: Sequence[Candle], *, keep: int = KEEP_BUCKETS
    ) -> "CandleSeries":
        """From stored candles, oldest first. The newest is taken as the forming one."""
        first = candles[0]
        series = cls(interval_ms, first.bucket_start_ms, first.open_paise, keep=keep)
        for c in candles:
            if c.trade_count:
                series._put(
                    c.bucket_start_ms,
                    _Trades(
                        c.open_paise,
                        c.high_paise,
                        c.low_paise,
                        c.close_paise,
                        c.volume,
                        c.trade_count,
                    ),
                )
        series._open = candles[-1].bucket_start_ms
        return series

    @property
    def since_ms(self) -> int:
        return self._since

    @property
    def forming_start_ms(self) -> int:
        return self._open

    def forming(self) -> Candle:
        return self.candle(self._open)

    def add_trade(self, price: int, quantity: int, at_ms: int) -> list[tuple[Candle, bool]]:
        """Apply one trade. Returns every candle that is new or changed, with `closed`."""
        bucket = bucket_of(at_ms, self.interval)
        if bucket < self._since:
            return []  # older than the window kept in memory: nothing to correct
        changed = [(c, True) for c in self.close_until(bucket)] if bucket > self._open else []
        old_close = self.candle(bucket).close_paise
        t = self._trades.get(bucket)
        if t is None:
            self._put(bucket, _Trades(price, price, price, price, quantity, 1))
        else:
            t.high, t.low = max(t.high, price), min(t.low, price)
            t.last = price
            t.volume += quantity
            t.count += 1
        changed.append((self.candle(bucket), bucket < self._open))
        if price != old_close:
            # A late trade moved the close of a closed bucket: the flat candles after it carry
            # that close, up to the next bucket with trades of its own.
            b = bucket + self.interval
            while b <= self._open and b not in self._trades:
                changed.append((self.candle(b), b < self._open))
                b += self.interval
        return changed

    def close_until(self, now_ms: int) -> list[Candle]:
        """Close every bucket that ends at or before `now_ms`. Returns them, oldest first."""
        closed = []
        while self._open + self.interval <= now_ms:
            closed.append(self.candle(self._open))
            self._open += self.interval
        self._prune()
        return closed

    def candle(self, bucket: int) -> Candle:
        t = self._trades.get(bucket)
        if t is not None:
            return Candle(bucket, t.first, t.high, t.low, t.last, t.volume, t.count)
        close = self._close_before(bucket)
        return Candle(bucket, close, close, close, close, 0, 0)

    def candles(self, from_ms: int, to_ms: int) -> list[Candle]:
        """Candles whose bucket starts in [from_ms, to_ms], oldest first, forming one included."""
        first = max(self._since, bucket_of(from_ms, self.interval))
        last = min(self._open, bucket_of(to_ms, self.interval))
        return [self.candle(b) for b in range(first, last + 1, self.interval)]

    def _close_before(self, bucket: int) -> int:
        i = bisect_left(self._keys, bucket)
        return self._trades[self._keys[i - 1]].last if i else self._prior

    def _put(self, bucket: int, trades: _Trades) -> None:
        self._trades[bucket] = trades
        insort(self._keys, bucket)

    def _prune(self) -> None:
        oldest = self._open - (self._keep - 1) * self.interval
        if oldest <= self._since:
            return
        self._prior = self._close_before(oldest)
        while self._keys and self._keys[0] < oldest:
            del self._trades[self._keys.pop(0)]
        self._since = oldest


def rebuild(
    trades: Iterable[tuple[int, int, int]],
    interval_ms: int,
    since_ms: int,
    until_ms: int,
    prior_close: int,
) -> list[Candle]:
    """Every candle from the bucket of `since_ms` to the bucket of `until_ms`, from trades alone.

    `trades` are (price, quantity, executed_at_ms) in sequence order. Deliberately written apart
    from CandleSeries (no shared code), so the property test compares two derivations.
    """
    first, last = bucket_of(since_ms, interval_ms), bucket_of(until_ms, interval_ms)
    groups: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for price, quantity, at in trades:
        b = bucket_of(at, interval_ms)
        if first <= b <= last:
            groups[b].append((price, quantity))
    out: list[Candle] = []
    close = prior_close
    for b in range(first, last + 1, interval_ms):
        group = groups.get(b)
        if group:
            prices = [p for p, _ in group]
            c = Candle(
                b,
                prices[0],
                max(prices),
                min(prices),
                prices[-1],
                sum(q for _, q in group),
                len(prices),
            )
        else:
            c = Candle(b, close, close, close, close, 0, 0)
        close = c.close_paise
        out.append(c)
    return out
