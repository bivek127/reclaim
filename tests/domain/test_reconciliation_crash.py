"""Reconciliation crash recovery across every point in polling and re-POST.

GETs are freely repeatable. The same-key POST is repeatable because the key
never changes. Neither can create a second financial mechanism.
"""

from __future__ import annotations

import psycopg
import pytest

from reclaim.domain.reconciliation import (
    OPERATION_FETCH,
    claim_for_reconciliation,
    open_attempt_for,
    poll_count,
    reconcile_case,
    reconcile_once,
)
from reclaim.domain.states import CaseState
from reclaim.domain.sweeper import sweep_expired_leases
from reclaim.provider.contract import FetchOutcome, ProviderOutcome
from tests.domain.execution_helpers import (
    StubReconcileProvider,
    actions_for,
    attempts_for,
    case_row,
    fetch_found,
    fetch_no_evidence,
    fetch_not_found,
    requests_for,
    seed_dispatchable,
)
from tests.domain.test_reconciliation import _to_ambiguous_lost_response


def _expire_lease(conn: psycopg.Connection, case_id: int) -> None:
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = now() - interval '1 s' "
        "WHERE id = %s",
        (case_id,),
    )


def _stale_the_token(conn: psycopg.Connection, case_id: int) -> None:
    """Make any previously-held token stale by letting another worker claim.

    Deliberately explicit rather than relying on a sweeper pass: the sweeper
    only bumps when worker_id IS NOT NULL, so an already-released lease makes
    it a no-op and the "stale" token would still be current.
    """
    from reclaim.domain.leases import claim_case

    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = '-infinity' WHERE id = %s",
        (case_id,),
    )
    claim = claim_case(conn, case_id, CaseState.AMBIGUOUS, "other-worker", 60)
    assert claim is not None, "the staling claim must succeed"


def _strand_in_reconciling(conn: psycopg.Connection, ids: dict, token: int) -> None:
    """Simulate a worker that entered RECONCILING then died before its GET."""
    from reclaim.domain.transitions import transition

    applied = transition(
        conn, ids["case_id"], CaseState.AMBIGUOUS, CaseState.RECONCILING,
        token, "reconciliation_claimed",
    )
    assert applied
    conn.execute(
        "UPDATE recovery_cases SET worker_id = 'dead-worker' WHERE id = %s",
        (ids["case_id"],),
    )
    _expire_lease(conn, ids["case_id"])


# ---- R1 / R2: crash before or during the GET -----------------------------


def test_r1_orphaned_reconciling_is_reclaimable(conn: psycopg.Connection) -> None:
    """A crashed reconciler must not strand the case until TTL."""
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids)
    _strand_in_reconciling(conn, ids, token)

    claimed = claim_for_reconciliation(conn, worker_id="w2", lease_seconds=45)

    assert claimed is not None
    claim, source = claimed
    assert claim.case_id == ids["case_id"]
    assert source is CaseState.RECONCILING


def test_r1_reclaim_bumps_the_fencing_token(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids)
    _strand_in_reconciling(conn, ids, token)
    before = case_row(conn, ids["case_id"])["fencing_token"]

    claim, _ = claim_for_reconciliation(conn, worker_id="w2", lease_seconds=45)

    assert claim.fencing_token > before


def test_r2_orphan_requery_resolves_normally(conn: psycopg.Connection) -> None:
    """Repeating the GET is safe and completes the cycle."""
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids)
    _strand_in_reconciling(conn, ids, token)

    result = reconcile_once(
        conn, provider=StubReconcileProvider(fetch_found()), worker_id="w2"
    )

    assert result is not None
    assert result.case_state is CaseState.AWAITING_CUSTOMER
    assert case_row(conn, ids["case_id"])["state"] == "AWAITING_CUSTOMER"


def test_r2_orphan_recovery_makes_no_financial_post(
    conn: psycopg.Connection,
) -> None:
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids)
    _strand_in_reconciling(conn, ids, token)
    provider = StubReconcileProvider(fetch_found())

    reconcile_once(conn, provider=provider, worker_id="w2")

    assert provider.create_calls == []


def test_r1_orphan_recovery_adds_no_attempt(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids)
    _strand_in_reconciling(conn, ids, token)

    reconcile_once(conn, provider=StubReconcileProvider(fetch_found()), worker_id="w2")

    assert len(attempts_for(conn, ids["case_id"])) == 1
    assert len(actions_for(conn, ids["case_id"])) == 1


# ---- R3 / R4: GET answered, crash before the write-back -------------------


def test_r3_found_then_crash_requery_still_adopts(conn: psycopg.Connection) -> None:
    """The GET is repeated; FOUND is stable, so adoption happens once."""
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids)
    _strand_in_reconciling(conn, ids, token)  # stands in for a lost TXN B

    reconcile_once(conn, provider=StubReconcileProvider(fetch_found()), worker_id="w2")

    assert case_row(conn, ids["case_id"])["state"] == "AWAITING_CUSTOMER"
    assert attempts_for(conn, ids["case_id"])[0]["state"] == "ACCEPTED"


def test_r4_not_found_then_crash_rebranches_on_local_state(
    conn: psycopg.Connection,
) -> None:
    """attempt UNKNOWN + NOT_FOUND stays authoritative across a crash."""
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids, sent=True)
    _strand_in_reconciling(conn, ids, token)

    result = reconcile_once(
        conn, provider=StubReconcileProvider(fetch_not_found()), worker_id="w2"
    )

    assert result.case_state is CaseState.ATTEMPT_FAILED
    assert actions_for(conn, ids["case_id"])[0]["status"] == "TERMINAL_FAILED"


def test_repeated_gets_are_idempotent_locally(conn: psycopg.Connection) -> None:
    """Re-querying adds request rows but never a second mechanism."""
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids)

    for _ in range(3):
        reconcile_case(
            conn, ids["case_id"],
            provider=StubReconcileProvider(fetch_no_evidence()),
            fencing_token=token, max_polls=99,
        )
        token = case_row(conn, ids["case_id"])["fencing_token"]

    assert poll_count(conn, ids["case_id"]) == 3
    assert len(attempts_for(conn, ids["case_id"])) == 1
    assert len(actions_for(conn, ids["case_id"])) == 1


# ---- R5: the query itself failed -----------------------------------------


def test_r5_query_failure_is_not_evidence(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids)

    result = reconcile_case(
        conn, ids["case_id"], provider=StubReconcileProvider(fetch_no_evidence()),
        fencing_token=token,
    )

    assert result.fetch_outcome is FetchOutcome.NO_EVIDENCE
    assert result.case_state is CaseState.AMBIGUOUS
    assert actions_for(conn, ids["case_id"])[0]["status"] == "UNRESOLVED"


def test_r5_query_failure_records_the_attempted_query(
    conn: psycopg.Connection,
) -> None:
    """Even a failed query leaves an auditable row."""
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids)

    reconcile_case(
        conn, ids["case_id"], provider=StubReconcileProvider(fetch_no_evidence()),
        fencing_token=token,
    )

    fetches = conn.execute(
        """
        SELECT pr.outcome, pr.completed_at FROM provider_requests pr
          JOIN execution_attempts ea ON ea.id = pr.attempt_id
         WHERE ea.case_id = %s AND pr.operation = %s
        """,
        (ids["case_id"], OPERATION_FETCH),
    ).fetchall()
    assert len(fetches) == 1
    assert fetches[0][0] == "NO_EVIDENCE"
    assert fetches[0][1] is not None


# ---- R6: stale worker after someone else resolved ------------------------


def test_r6_stale_worker_result_is_discarded(conn: psycopg.Connection) -> None:
    """A lost lease means discard, never re-apply under a new token."""
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids)

    _stale_the_token(conn, ids["case_id"])
    assert case_row(conn, ids["case_id"])["fencing_token"] != token

    result = reconcile_case(
        conn, ids["case_id"], provider=StubReconcileProvider(fetch_found()),
        fencing_token=token,  # stale
    )

    assert result.applied is False
    assert case_row(conn, ids["case_id"])["state"] == "AMBIGUOUS"


def test_r6_stale_worker_makes_no_financial_post(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids, sent=False)
    _stale_the_token(conn, ids["case_id"])
    assert case_row(conn, ids["case_id"])["fencing_token"] != token
    provider = StubReconcileProvider(fetch_not_found())

    result = reconcile_case(
        conn, ids["case_id"], provider=provider, fencing_token=token
    )

    assert result.applied is False
    assert provider.create_calls == [], "a stale worker must never POST"


def test_r6_stale_worker_does_not_adopt(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids)
    _stale_the_token(conn, ids["case_id"])

    reconcile_case(
        conn, ids["case_id"], provider=StubReconcileProvider(fetch_found()),
        fencing_token=token,
    )

    assert attempts_for(conn, ids["case_id"])[0]["state"] == "UNKNOWN"


# ---- R7: crash around the re-POST ----------------------------------------


def test_r7_repost_row_recorded_before_the_call(conn: psycopg.Connection) -> None:
    """The POST is durable-logged before it goes out, as in any dispatch TXN 1."""
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids, sent=False)
    seen: dict[str, object] = {}

    class _Watching(StubReconcileProvider):
        def create_payment_link(self, **kwargs):
            with psycopg.connect(conn.info.dsn, autocommit=True) as other:
                row = other.execute(
                    """
                    SELECT count(*) FROM provider_requests pr
                      JOIN execution_attempts ea ON ea.id = pr.attempt_id
                     WHERE ea.case_id = %s AND pr.operation = 'create_payment_link'
                    """,
                    (ids["case_id"],),
                ).fetchone()
                seen["posts_committed"] = row[0]
            return super().create_payment_link(**kwargs)

    reconcile_case(
        conn, ids["case_id"], provider=_Watching(fetch_not_found()),
        fencing_token=token,
    )

    assert seen["posts_committed"] == 2, "re-POST row must be committed before the call"


def test_r7_repost_reuses_the_same_attempt_row(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids, sent=False)
    before = attempts_for(conn, ids["case_id"])[0]["id"]

    reconcile_case(
        conn, ids["case_id"], provider=StubReconcileProvider(fetch_not_found()),
        fencing_token=token,
    )

    after = attempts_for(conn, ids["case_id"])
    assert len(after) == 1
    assert after[0]["id"] == before


def test_r7_request_sequence_is_monotonic(conn: psycopg.Connection) -> None:
    """uq_request_sequence holds across POST + GET + re-POST on one attempt."""
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids, sent=False)

    reconcile_case(
        conn, ids["case_id"], provider=StubReconcileProvider(fetch_not_found()),
        fencing_token=token,
    )

    numbers = [r["request_no"] for r in requests_for(conn, ids["case_id"])]
    assert numbers == sorted(numbers)
    assert len(numbers) == len(set(numbers))


def test_no_new_key_is_ever_minted_during_reconciliation(
    conn: psycopg.Connection,
) -> None:
    """Across every crash path, the attempt keeps its original key."""
    ids = seed_dispatchable(conn)
    prepared, token = _to_ambiguous_lost_response(conn, ids, sent=False)

    reconcile_case(
        conn, ids["case_id"], provider=StubReconcileProvider(fetch_not_found()),
        fencing_token=token,
    )

    keys = {r["idempotency_key"] for r in requests_for(conn, ids["case_id"])}
    assert keys == {prepared.idempotency_key}
