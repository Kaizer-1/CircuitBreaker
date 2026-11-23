"""Which snapshots the engine keeps (domain/retention.py, policy P-03)."""

from matching_engine.domain.model import ms_of
from matching_engine.domain.retention import (
    FULL_WINDOW_MS,
    HORIZON_MS,
    HOUR_MS,
    SLOT_MS,
    hour_of,
    snapshots_to_keep,
)

NOW = ms_of("2026-11-20T12:00:00.000Z")
MIN = 60_000


def every(start: int, end: int, step: int) -> list[tuple[int, int]]:
    """Snapshots (sequence, as_of) from start to end, `step` ms apart, sequences rising."""
    return [(i + 1, at) for i, at in enumerate(range(start, end + 1, step))]


def test_an_empty_store_keeps_nothing() -> None:
    assert snapshots_to_keep([], NOW) == set()


def test_every_snapshot_of_the_last_hour_is_kept() -> None:
    rows = every(NOW - FULL_WINDOW_MS, NOW, 30_000)
    assert snapshots_to_keep(rows, NOW) == {s for s, _ in rows}


def test_older_snapshots_keep_the_earliest_of_each_10_minute_slot() -> None:
    rows = every(NOW - 3 * HOUR_MS, NOW, MIN)
    kept = snapshots_to_keep(rows, NOW)
    by_seq = dict(rows)
    old = sorted(by_seq[s] for s in kept if by_seq[s] < NOW - FULL_WINDOW_MS)
    assert old == list(range(NOW - 3 * HOUR_MS, NOW - FULL_WINDOW_MS, SLOT_MS))


def test_snapshots_past_the_horizon_go_except_the_newest_hour_that_ended_before_it() -> None:
    rows = every(NOW - 30 * HOUR_MS, NOW, 10 * MIN)  # one per slot: thinning removes none
    kept = snapshots_to_keep(rows, NOW)
    by_seq = dict(rows)
    oldest = min(by_seq[s] for s in kept)
    horizon = NOW - HORIZON_MS
    assert hour_of(oldest) + HOUR_MS <= horizon  # a snapshot at or before the horizon remains
    assert oldest == hour_of(horizon) - HOUR_MS  # and nothing from an older hour
    assert {s for s, a in rows if a >= hour_of(horizon) - HOUR_MS} == kept


def test_the_newest_snapshot_stays_after_a_long_quiet_spell() -> None:
    # The market went quiet two days ago: its last snapshots are all past the horizon.
    last = NOW - 48 * HOUR_MS + 30 * MIN
    rows = every(last - 25 * MIN, last, MIN)  # 12:05 ... 12:30, two days ago
    by_seq = dict(rows)
    kept = sorted(by_seq[s] for s in snapshots_to_keep(rows, NOW))
    # The newest stays (recovery starts there) and its hour keeps its slots (the horizon's anchor).
    assert kept == [last - 25 * MIN, last - 20 * MIN, last - 10 * MIN, last]


def test_a_seek_in_the_thinned_hours_starts_at_most_one_slot_back() -> None:
    rows = every(NOW - 20 * HOUR_MS, NOW, 30_000)
    by_seq = dict(rows)
    kept_times = sorted(by_seq[s] for s in snapshots_to_keep(rows, NOW))
    for at in range(NOW - 20 * HOUR_MS, NOW - FULL_WINDOW_MS, 7 * MIN):
        start = max(t for t in kept_times if t <= at)
        assert at - start < SLOT_MS


def test_the_rule_is_stable_when_applied_again_later() -> None:
    rows = every(NOW - 30 * HOUR_MS, NOW, 2 * MIN)
    first = snapshots_to_keep(rows, NOW - 2 * HOUR_MS)
    kept_rows = [r for r in rows if r[0] in first or r[1] > NOW - 2 * HOUR_MS]
    assert snapshots_to_keep(kept_rows, NOW) == snapshots_to_keep(rows, NOW)
