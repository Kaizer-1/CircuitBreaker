"""Thinning snapshots never costs a seek inside the horizon more than one 10-minute slot of replay,
and pruning as time passes keeps exactly what one pass over the whole history would keep
(domain/retention.py, policy P-03).

Histories here have bursts and quiet spells of up to two days, so slots, hours and the horizon are
crossed in every combination.
"""

from hypothesis import given
from hypothesis import strategies as st

from matching_engine.domain.model import ms_of
from matching_engine.domain.retention import (
    FULL_WINDOW_MS,
    HORIZON_MS,
    MIN_SPACING_MS,
    SLOT_MS,
    snapshots_to_keep,
)

START = ms_of("2026-11-20T00:00:00.000Z")
gaps = st.lists(
    st.one_of(
        st.integers(MIN_SPACING_MS, 5 * 60_000),  # trading
        st.integers(MIN_SPACING_MS, 2 * 24 * 3_600_000),  # quiet spells
    ),
    min_size=1,
    max_size=300,
)


def history(steps: list[int]) -> list[tuple[int, int]]:
    rows, at = [], START
    for i, gap in enumerate(steps):
        at += gap
        rows.append((i + 1, at))
    return rows


def newest_at_or_before(rows: list[tuple[int, int]], at: int) -> int | None:
    return max((a for _, a in rows if a <= at), default=None)


@given(gaps, st.lists(st.integers(0, 4 * 3_600_000), min_size=1, max_size=8))
def test_every_seek_inside_the_horizon_replays_at_most_one_slot_more(
    steps: list[int], seeks: list[int]
) -> None:
    rows = history(steps)
    now = rows[-1][1]
    kept = [r for r in rows if r[0] in snapshots_to_keep(rows, now)]
    assert kept[-1] == rows[-1]  # recovery's snapshot
    for offset in seeks:
        at = now - (offset * HORIZON_MS) // (4 * 3_600_000)  # anywhere from now back to the horizon
        best = newest_at_or_before(rows, at)
        found = newest_at_or_before(kept, at)
        if best is None:
            assert found is None
            continue
        assert found is not None, "a seek inside the horizon lost its snapshot"
        if best >= now - FULL_WINDOW_MS:
            assert found == best  # the last hour keeps them all
        else:
            assert best - found < SLOT_MS  # the slot's earliest, at most one slot back


@given(gaps, st.lists(st.integers(1, 300), min_size=1, max_size=6))
def test_pruning_as_time_passes_keeps_what_one_pass_keeps(
    steps: list[int], cuts: list[int]
) -> None:
    rows = history(steps)
    stored: list[tuple[int, int]] = []
    for cut in sorted({min(c, len(rows)) for c in cuts} | {len(rows)}):
        stored += [r for r in rows[:cut] if r not in stored]  # snapshots taken since the last pass
        now = stored[-1][1]
        keep = snapshots_to_keep(stored, now)
        stored = [r for r in stored if r[0] in keep]
    assert {s for s, _ in stored} == snapshots_to_keep(rows, rows[-1][1])
