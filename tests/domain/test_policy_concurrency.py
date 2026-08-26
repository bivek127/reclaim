"""Policy application races on real, independent PostgreSQL connections."""

from __future__ import annotations

import threading
from typing import Any

import psycopg

from reclaim.domain.leases import claim_case
from reclaim.domain.policy import PolicyFacts, apply_policy
from reclaim.domain.states import CaseState
from tests.domain.policy_helpers import (
    case_row,
    policy_audit_count,
    policy_decisions_for,
    seed_policy_eval,
)


def _run_parallel(dsn: str, worker, count: int = 2) -> list[Any]:
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


def test_concurrent_apply_produces_one_policy_decision(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    """Two workers, one case: exactly one policy outcome."""
    ids = seed_policy_eval(conn)
    facts = PolicyFacts(
        cause=ids["cause"],
        attempt_count=0,
        max_attempts=2,
        conflicting_history=False,
    )

    def worker(own: psycopg.Connection, index: int):
        return apply_policy(
            own,
            ids["case_id"],
            facts=facts,
            diagnosis_id=ids["diagnosis_id"],
            fencing_token=0,
            worker_id=f"policy-{index}",
        )

    results = _run_parallel(migrated_database, worker)
    assert all(kind == "ok" for kind, _ in results)

    applied = [r for kind, r in results if kind == "ok" and r.applied]
    rejected = [r for kind, r in results if kind == "ok" and not r.applied]
    assert len(applied) == 1
    assert len(rejected) == 1
    assert len(policy_decisions_for(conn, ids["case_id"])) == 1
    assert policy_audit_count(conn, ids["case_id"]) == 1
    assert case_row(conn, ids["case_id"])["state"] == "ACTION_READY"


def test_stale_fencing_token_cannot_commit_policy_decision(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    """Holder with current token wins; stale worker leaves no decision row."""
    ids = seed_policy_eval(conn)
    claim = claim_case(
        conn, ids["case_id"], CaseState.POLICY_EVAL, "holder", 90
    )
    assert claim is not None
    current = claim.fencing_token
    facts = PolicyFacts(
        cause=ids["cause"],
        attempt_count=0,
        max_attempts=2,
        conflicting_history=False,
    )

    def worker(own: psycopg.Connection, index: int):
        token = current if index == 0 else 0
        return apply_policy(
            own,
            ids["case_id"],
            facts=facts,
            diagnosis_id=ids["diagnosis_id"],
            fencing_token=token,
            worker_id=f"fenced-{index}",
        )

    results = _run_parallel(migrated_database, worker)
    assert all(kind == "ok" for kind, _ in results)

    applied = [r for kind, r in results if kind == "ok" and r.applied]
    assert len(applied) == 1
    assert len(policy_decisions_for(conn, ids["case_id"])) == 1
    assert policy_audit_count(conn, ids["case_id"]) == 1
    assert case_row(conn, ids["case_id"])["fencing_token"] == current

    stale_audit = conn.execute(
        "SELECT count(*) FROM audit_events WHERE case_id = %s "
        "AND reason_code = 'stale_write_rejected'",
        (ids["case_id"],),
    ).fetchone()
    assert stale_audit is not None and stale_audit[0] >= 1
