"""Which book snapshots to keep (policy P-03; docs/03-architecture/04-event-sourcing-and-cqrs.md).

Snapshots only make replay fast; the events are the history and are never deleted. A snapshot
older than the time-travel horizon serves no seek, so keeping every one for ever grew one partition
per ticker to 2 GB after 19 hours (profiling, 2026-11-15) and slowed every read of it.

The rule, in event time (`now` is the ticker's latest event), so every replica and every rerun
keeps the same snapshots:

- the newest snapshot, always (recovery starts from it, even after a day with no trading);
- every snapshot of the last hour (seeks there replay at most P-03's spacing of events);
- older than that, the earliest snapshot in each 10-minute slot (a seek replays at most about
  10 minutes of events);
- older than the 24-hour horizon, nothing, except the snapshots of the newest hour that ended
  before it: a seek at the very start of the horizon still has a snapshot at or before it.

Snapshots are stored in one partition per ticker per hour of `as_of` (`hour_of`), so no partition
holds more than one hour of snapshots and a 10-minute slot never spans two partitions.
"""

from collections.abc import Iterable

HOUR_MS = 3_600_000
FULL_WINDOW_MS = HOUR_MS  # keep every snapshot this close to the latest event
SLOT_MS = 600_000  # older ones: the earliest per 10-minute slot
HORIZON_MS = 24 * HOUR_MS  # time travel seeks back this far (Q-12 refuses anything older)
MIN_SPACING_MS = 30_000  # P-03: at most one snapshot per 30 s of event time per ticker


def hour_of(as_of_ms: int) -> int:
    """The start of the hour (ms) whose partition holds a snapshot taken at `as_of_ms`."""
    return as_of_ms - as_of_ms % HOUR_MS


def hours_to_drop(hours: Iterable[int], now_ms: int) -> set[int]:
    """Hours (partition starts) wholly past the horizon, except the newest of them: its snapshots
    are the horizon's anchor."""
    hours = set(hours)
    horizon = now_ms - HORIZON_MS
    anchor = max((h for h in hours if h + HOUR_MS <= horizon), default=None)
    return set() if anchor is None else {h for h in hours if h < anchor}


def snapshots_to_keep(snapshots: Iterable[tuple[int, int]], now_ms: int) -> set[int]:
    """The sequences to keep, of (sequence, as_of_ms) pairs, when the latest event is at now_ms."""
    rows = sorted(snapshots, key=lambda s: s[0])
    if not rows:
        return set()
    full_from = now_ms - FULL_WINDOW_MS
    dropped = hours_to_drop((hour_of(a) for _, a in rows), now_ms)
    keep = {rows[-1][0]}
    earliest_in_slot: dict[int, int] = {}
    for sequence, as_of in rows:
        if as_of >= full_from:
            keep.add(sequence)
        elif hour_of(as_of) not in dropped:
            earliest_in_slot.setdefault(as_of - as_of % SLOT_MS, sequence)
    return keep | set(earliest_in_slot.values())
