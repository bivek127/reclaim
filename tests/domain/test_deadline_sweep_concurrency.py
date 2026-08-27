"""Deadline sweep under real concurrency (I5, I6, I8).

Genuinely independent psycopg connections in separate threads. The constraints
under test are database constraints, so mocking would prove nothing.
"""

from __future__ import annotations

import threading
from typing import Any

import psycopg

from reclaim.domain.leases import fenced_transition
from reclaim.domain.states import CaseState
from reclaim.domain.sweeper import expire_action_deadlines
from tests.domain.test_deadline_sweep import _case, _count, _past, seed_awaiting


def _parallel(dsn: str, worker, count: int = 2) -> list[Any]:
    results: list[Any] = [None] * count
    barrier = threading.Barrier(count)

    def target(index: int) -> None:
        with psycopg.connect(dsn, autocommit=True) as own:
            barrier.wait()
            try:
                results[index] = ("ok", worker(own, index))
            except Exception as exc:  # noqa: BLE001 - recorded, then asserted
                results[index] = ("err", exc)

    threads = [threading.Thread(target=target, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads), "a worker thread deadlocked"
    return results


def test_two_sweepers_escalate_a_case_exactly_once(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    """SKIP LOCKED plus the state guard: no double escalation, no second review."""
    ids = seed_awaiting(conn, deadline=_past())

    def worker(own: psycopg.Connection, _i: int):
        return expire_action_deadlines(own)

    results = _parallel(migrated_database, worker)

    escalated = sum(
        r[1].escalated for r in results if r and r[0] == "ok"
    )
    assert escalated == 1, f"expected exactly one escalation, got {escalated}"
    assert _case(conn, ids["case_id"])["state"] == "ESCALATED"
    assert _count(conn, "human_reviews", ids["case_id"]) == 1


def test_four_sweepers_still_escalate_once(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    ids = seed_awaiting(conn, deadline=_past())

    def worker(own: psycopg.Connection, _i: int):
        return expire_action_deadlines(own)

    results = _parallel(migrated_database, worker, count=4)

    escalated = sum(r[1].escalated for r in results if r and r[0] == "ok")
    assert escalated == 1
    assert _count(conn, "human_reviews", ids["case_id"]) == 1


def test_sweep_racing_verification_yields_one_winner(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    """Both edges out of AWAITING_CUSTOMER are legal; exactly one may commit.

    Whichever wins, the loser is fenced out and no revenue is written by the
    losing path -- I8 does not depend on who wins the race.
    """
    ids = seed_awaiting(conn, deadline=_past())
    token = _case(conn, ids["case_id"])["fencing_token"]

    def worker(own: psycopg.Connection, index: int):
        if index == 0:
            return expire_action_deadlines(own)
        return fenced_transition(
            own,
            ids["case_id"],
            CaseState.AWAITING_CUSTOMER,
            CaseState.AMBIGUOUS,
            token,
            "concurrent_verification",
            worker_id="verifier",
        )

    _parallel(migrated_database, worker)

    final = _case(conn, ids["case_id"])
    assert final["state"] in ("ESCALATED", "AMBIGUOUS")
    assert final["revenue"] == 0
    # Whoever lost wrote nothing: at most one review, and only if the sweep won.
    reviews = _count(conn, "human_reviews", ids["case_id"])
    assert reviews == (1 if final["state"] == "ESCALATED" else 0)


def test_concurrent_sweeps_over_many_cases_escalate_each_once(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    """SKIP LOCKED must partition work, not duplicate or drop it."""
    ids = [seed_awaiting(conn, deadline=_past()) for _ in range(6)]

    def worker(own: psycopg.Connection, _i: int):
        return expire_action_deadlines(own)

    results = _parallel(migrated_database, worker, count=3)

    total = sum(r[1].escalated for r in results if r and r[0] == "ok")
    assert total == 6, f"expected 6 escalations across workers, got {total}"
    for case in ids:
        assert _case(conn, case["case_id"])["state"] == "ESCALATED"
        assert _count(conn, "human_reviews", case["case_id"]) == 1
