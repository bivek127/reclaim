"""Reconciliation races on real, independent PostgreSQL connections.

Every test opens genuinely separate psycopg connections in separate threads.
The constraints under test *are* the thing being tested.
"""

from __future__ import annotations

import threading
from typing import Any

import psycopg
import pytest
from psycopg.errors import UniqueViolation

from reclaim.domain.execution import BudgetExhausted, DispatchAborted, dispatch
from reclaim.domain.leases import claim_case
from reclaim.domain.reconciliation import (
    claim_for_reconciliation,
    reconcile_case,
    reconcile_once,
)
from reclaim.domain.states import CaseState
from reclaim.provider.contract import ProviderOutcome
from tests.domain.execution_helpers import (
    LINK_TTL_SECONDS,
    StubProvider,
    StubReconcileProvider,
    actions_for,
    attempts_for,
    case_row,
    fetch_found,
    fetch_no_evidence,
    fetch_not_found,
    seed_dispatchable,
)
from tests.domain.test_reconciliation import _to_ambiguous_lost_response


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


def _split(results: list[Any]) -> tuple[list[Any], list[Exception]]:
    ok = [v for kind, v in results if kind == "ok"]
    err = [v for kind, v in results if kind == "err"]
    return ok, err


# ---- two reconcilers, one case -------------------------------------------


def test_only_one_reconciler_claims_a_case(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    ids = seed_dispatchable(conn)
    _to_ambiguous_lost_response(conn, ids)
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = '-infinity' WHERE id = %s",
        (ids["case_id"],),
    )

    def worker(own: psycopg.Connection, _i: int):
        return claim_for_reconciliation(own, worker_id="w", lease_seconds=45)

    ok, _ = _split(_run_parallel(migrated_database, worker))

    claimed = [c for c in ok if c is not None]
    assert len(claimed) == 1, "exactly one reconciler may hold the lease"


def test_concurrent_reconcilers_query_once_and_adopt_once(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    ids = seed_dispatchable(conn)
    _to_ambiguous_lost_response(conn, ids)
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = '-infinity' WHERE id = %s",
        (ids["case_id"],),
    )
    providers = [StubReconcileProvider(fetch_found()), StubReconcileProvider(fetch_found())]

    def worker(own: psycopg.Connection, index: int):
        return reconcile_once(own, provider=providers[index], worker_id=f"w{index}")

    ok, _ = _split(_run_parallel(migrated_database, worker))

    resolved = [r for r in ok if r is not None and r.applied]
    assert len(resolved) == 1
    assert sum(len(p.fetch_calls) for p in providers) == 1
    assert case_row(conn, ids["case_id"])["state"] == "AWAITING_CUSTOMER"
    assert len(attempts_for(conn, ids["case_id"])) == 1


def test_concurrent_reconcilers_make_at_most_one_post(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    """The decisive duplicate-charge test for the re-POST path."""
    ids = seed_dispatchable(conn)
    _to_ambiguous_lost_response(conn, ids, sent=False)
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = '-infinity' WHERE id = %s",
        (ids["case_id"],),
    )
    providers = [
        StubReconcileProvider(fetch_not_found()),
        StubReconcileProvider(fetch_not_found()),
    ]

    def worker(own: psycopg.Connection, index: int):
        return reconcile_once(own, provider=providers[index], worker_id=f"w{index}")

    _run_parallel(migrated_database, worker)

    assert sum(len(p.create_calls) for p in providers) <= 1
    assert len(attempts_for(conn, ids["case_id"])) == 1
    assert len(actions_for(conn, ids["case_id"])) == 1


def test_concurrent_reconcilers_never_double_adopt(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    ids = seed_dispatchable(conn)
    _to_ambiguous_lost_response(conn, ids)
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = '-infinity' WHERE id = %s",
        (ids["case_id"],),
    )

    def worker(own: psycopg.Connection, index: int):
        return reconcile_once(
            own, provider=StubReconcileProvider(fetch_found()), worker_id=f"w{index}"
        )

    _run_parallel(migrated_database, worker, count=3)

    accepted = [a for a in attempts_for(conn, ids["case_id"]) if a["state"] == "ACCEPTED"]
    assert len(accepted) == 1


# ---- fencing --------------------------------------------------------------


def test_stale_reconciler_blocked_once_the_case_is_resolved(
    conn: psycopg.Connection,
) -> None:
    """First guard: a resolved case has no open attempt left to reconcile."""
    from reclaim.domain.reconciliation import ReconciliationBlocked

    ids = seed_dispatchable(conn)
    _, stale_token = _to_ambiguous_lost_response(conn, ids)
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = '-infinity' WHERE id = %s",
        (ids["case_id"],),
    )
    fresh = claim_case(conn, ids["case_id"], CaseState.AMBIGUOUS, "fresh", 60)
    assert fresh is not None
    reconcile_case(
        conn, ids["case_id"], provider=StubReconcileProvider(fetch_found()),
        fencing_token=fresh.fencing_token,
    )
    assert case_row(conn, ids["case_id"])["state"] == "AWAITING_CUSTOMER"

    stale_provider = StubReconcileProvider(fetch_not_found())
    with pytest.raises(ReconciliationBlocked):
        reconcile_case(
            conn, ids["case_id"], provider=stale_provider, fencing_token=stale_token
        )

    assert case_row(conn, ids["case_id"])["state"] == "AWAITING_CUSTOMER"
    assert stale_provider.create_calls == []
    assert stale_provider.fetch_calls == [], "blocked before any provider call"


def test_stale_reconciler_cannot_overwrite_a_newer_result(
    conn: psycopg.Connection,
) -> None:
    """Second guard: the case is still open, so fencing is what rejects it.

    The newer worker gets NO_EVIDENCE, leaving the case unresolved -- so the
    stale worker really does reach the fenced write-back and is turned away
    there rather than by the open-attempt check.
    """
    ids = seed_dispatchable(conn)
    _, stale_token = _to_ambiguous_lost_response(conn, ids)
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = '-infinity' WHERE id = %s",
        (ids["case_id"],),
    )
    fresh = claim_case(conn, ids["case_id"], CaseState.AMBIGUOUS, "fresh", 60)
    assert fresh is not None
    reconcile_case(
        conn, ids["case_id"], provider=StubReconcileProvider(fetch_no_evidence()),
        fencing_token=fresh.fencing_token, max_polls=99,
    )
    assert case_row(conn, ids["case_id"])["state"] == "AMBIGUOUS"
    assert case_row(conn, ids["case_id"])["fencing_token"] != stale_token

    stale_provider = StubReconcileProvider(fetch_found())
    result = reconcile_case(
        conn, ids["case_id"], provider=stale_provider, fencing_token=stale_token,
        max_polls=99,
    )

    assert result.applied is False, "a stale token must not apply its finding"
    assert case_row(conn, ids["case_id"])["state"] == "AMBIGUOUS"
    assert attempts_for(conn, ids["case_id"])[0]["state"] == "UNKNOWN"
    assert stale_provider.create_calls == []


def test_stale_reconciler_write_is_audited(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)
    _, stale_token = _to_ambiguous_lost_response(conn, ids)
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = '-infinity' WHERE id = %s",
        (ids["case_id"],),
    )
    fresh = claim_case(conn, ids["case_id"], CaseState.AMBIGUOUS, "fresh", 60)
    assert fresh is not None
    reconcile_case(
        conn, ids["case_id"], provider=StubReconcileProvider(fetch_no_evidence()),
        fencing_token=fresh.fencing_token, max_polls=99,
    )

    reconcile_case(
        conn, ids["case_id"], provider=StubReconcileProvider(fetch_found()),
        fencing_token=stale_token, max_polls=99,
    )

    row = conn.execute(
        "SELECT count(*) FROM audit_events WHERE case_id = %s "
        "AND reason_code = 'stale_write_rejected'",
        (ids["case_id"],),
    ).fetchone()
    assert row is not None and row[0] >= 1


# ---- I4: reconciliation vs dispatch are mutually exclusive ----------------


def test_dispatcher_cannot_act_on_a_reconciling_case(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    """Mutual exclusion is the state guard, not a separate mechanism."""
    ids = seed_dispatchable(conn)
    _to_ambiguous_lost_response(conn, ids)
    token = case_row(conn, ids["case_id"])["fencing_token"]
    provider = StubProvider()

    with pytest.raises((DispatchAborted, BudgetExhausted, UniqueViolation)):
        dispatch(
            conn,
            ids["case_id"],
            provider=provider,
            fencing_token=token,
            policy_decision_id=ids["policy_id"],
            link_ttl_seconds=LINK_TTL_SECONDS,
        )

    assert provider.calls == [], "no financial POST while unresolved"


def test_reconciler_and_dispatcher_race(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    """Only the reconciler may act; the dispatcher must lose on state."""
    ids = seed_dispatchable(conn)
    _to_ambiguous_lost_response(conn, ids)
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = '-infinity' WHERE id = %s",
        (ids["case_id"],),
    )
    exec_provider = StubProvider()

    def worker(own: psycopg.Connection, index: int):
        if index == 0:
            return reconcile_once(
                own, provider=StubReconcileProvider(fetch_found()), worker_id="rec"
            )
        return dispatch(
            own,
            ids["case_id"],
            provider=exec_provider,
            fencing_token=case_row(own, ids["case_id"])["fencing_token"],
            policy_decision_id=ids["policy_id"],
            link_ttl_seconds=LINK_TTL_SECONDS,
        )

    _run_parallel(migrated_database, worker)

    assert exec_provider.calls == [], "a dispatcher must never POST here"
    assert len(attempts_for(conn, ids["case_id"])) == 1
    assert len(actions_for(conn, ids["case_id"])) == 1


def test_no_duplicate_unresolved_mechanism_under_load(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    """Four workers, one ambiguous case: still exactly one action and attempt."""
    ids = seed_dispatchable(conn)
    _to_ambiguous_lost_response(conn, ids, sent=False)
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = '-infinity' WHERE id = %s",
        (ids["case_id"],),
    )

    def worker(own: psycopg.Connection, index: int):
        return reconcile_once(
            own,
            provider=StubReconcileProvider(fetch_not_found()),
            worker_id=f"w{index}",
        )

    _run_parallel(migrated_database, worker, count=4)

    assert len(actions_for(conn, ids["case_id"])) == 1
    assert len(attempts_for(conn, ids["case_id"])) == 1
    assert case_row(conn, ids["case_id"])["attempt_count"] == 1
