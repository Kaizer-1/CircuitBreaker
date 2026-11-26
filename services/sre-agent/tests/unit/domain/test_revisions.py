"""New release, rollback, restart: told apart by the pod template, not the revision number."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sre_agent.domain.revisions import (
    RESTARTED_AT,
    ReplicaSetInfo,
    facts,
    history,
    spans_from_series,
)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def template(image: str, restarted: str | None = None, hash_: str = "abc") -> dict[str, Any]:
    annotations = {RESTARTED_AT: restarted} if restarted else {}
    return {
        "metadata": {"labels": {"app": "order-service", "pod-template-hash": hash_},
                     "annotations": annotations},
        "spec": {"containers": [{"name": "order-service", "image": image}]},
    }  # fmt: skip


def rs(revision: int, minutes_ago: float, tpl: dict[str, Any], cause: str) -> ReplicaSetInfo:
    return ReplicaSetInfo(
        revision, NOW - timedelta(minutes=minutes_ago), tpl, cause, f"rs-{revision}"
    )


def ran(*minutes_ago: tuple[float, float]) -> list[tuple[datetime, datetime]]:
    """Runs as (from, to) minutes ago."""
    return [(NOW - timedelta(minutes=a), NOW - timedelta(minutes=b)) for a, b in minutes_ago]


GOOD = template("ghcr.io/x/order-service:a1", hash_="h1")
BAD = template("ghcr.io/x/order-service:a1-bad", hash_="h2")


def test_a_new_release_is_new_and_rolls_back_to_the_release_before() -> None:
    f = facts([rs(8, 120, GOOD, "release 0.5.0"), rs(9, 2, BAD, "release 1.4.1")], NOW)
    assert f is not None
    assert (f.revision, f.release, f.new, f.known_good) == (9, "release 1.4.1", True, False)
    assert (f.rollback_to, f.rollback_release) == (8, "release 0.5.0")


def test_after_a_rollback_the_revision_number_is_new_but_the_template_is_known_good() -> None:
    # Kubernetes reuses revision 8's ReplicaSet and renumbers it 10: created two hours ago.
    f = facts([rs(9, 5, BAD, "release 1.4.1"), rs(10, 120, GOOD, "release 0.5.0")], NOW)
    assert f is not None
    assert (f.revision, f.new, f.known_good) == (10, False, True)


def test_a_restart_is_the_same_release_not_a_new_one() -> None:
    restarted = template("ghcr.io/x/order-service:a1", restarted=NOW.isoformat(), hash_="h3")
    f = facts([rs(8, 120, GOOD, "release 0.5.0"), rs(9, 1, restarted, "release 0.5.0")], NOW)
    assert f is not None
    assert (f.revision, f.new, f.known_good, f.rollback_to) == (9, False, True, None)


def test_a_restart_soon_after_a_new_release_keeps_the_release_new() -> None:
    again = template("ghcr.io/x/order-service:a1-bad", restarted=NOW.isoformat(), hash_="h4")
    f = facts([rs(8, 120, GOOD, "r0"), rs(9, 6, BAD, "r1"), rs(10, 1, again, "r1")], NOW)
    assert f is not None
    assert (f.new, round(f.running_minutes), f.rollback_to) == (True, 6, 8)


def test_no_replicasets_no_facts_and_history_marks_restarts() -> None:
    assert facts([], NOW) is None
    restarted = template("ghcr.io/x/order-service:a1", restarted="t", hash_="h5")
    rows = history([rs(8, 120, GOOD, "r0"), rs(9, 1, restarted, "r0")], NOW)
    assert [r["revision"] for r in rows] == [9, 8]
    assert rows[0]["same_release_as_revision"] == 8 and rows[1]["same_release_as_revision"] is None


def test_a_bad_release_deployed_again_is_new_although_its_replicaset_is_old() -> None:
    """Staging, 2026-11-26: the day's second chaos BAD_DEPLOY reused the first one's ReplicaSet
    (made hours before), which had only ever run for a few minutes at a time."""
    sets = [rs(26, 1100, GOOD, "release 0.5.0"), rs(29, 300, BAD, "release 1.4.1")]
    runs = {"rs-26": ran((1100, 2), (30, 25)), "rs-29": ran((300, 297), (35, 30), (1, 0))}
    f = facts(sets, NOW, runs)
    assert f is not None
    assert (f.new, f.known_good, round(f.running_minutes), round(f.longest_run_minutes)) == (
        True, False, 1, 5)  # fmt: skip


def test_a_rollback_to_a_release_that_ran_for_hours_is_known_good() -> None:
    sets = [rs(29, 7, BAD, "release 1.4.1"), rs(30, 1100, GOOD, "release 0.5.0")]
    runs = {"rs-30": ran((1100, 6), (2, 0)), "rs-29": ran((6, 3))}
    f = facts(sets, NOW, runs)
    assert f is not None and f.known_good and not f.new
    assert round(f.running_minutes) == 2 and f.longest_run_minutes > 1000


def test_a_restart_during_a_long_run_continues_that_run() -> None:
    restarted = template("ghcr.io/x/order-service:a1", restarted="t", hash_="h9")
    sets = [rs(8, 600, GOOD, "r0"), rs(9, 1, restarted, "r0")]
    runs = {"rs-8": ran((600, 1)), "rs-9": ran((1, 0))}
    f = facts(sets, NOW, runs)
    assert f is not None and f.known_good and round(f.running_minutes) == 600


def test_a_fresh_replicaset_gives_the_exact_moment_its_run_began() -> None:
    sets = [rs(8, 600, GOOD, "r0"), rs(9, 2.5, BAD, "r1")]
    runs = {"rs-8": ran((600, 2)), "rs-9": ran((2, 0))}  # the series only knows the minute
    f = facts(sets, NOW, runs)
    assert f is not None and f.new and f.first_ran_at == NOW - timedelta(minutes=2.5)


def test_runs_are_read_from_a_series_of_ready_pods() -> None:
    spans = spans_from_series([0, 2, 2, None, 0, 1], NOW, 60)
    assert spans == [
        (NOW - timedelta(minutes=4), NOW - timedelta(minutes=3)),
        (NOW, NOW),
    ]
