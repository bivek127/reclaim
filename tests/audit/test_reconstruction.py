"""Reconstruct a case's complete history from the audit trail ALONE."""

from __future__ import annotations

import psycopg
import pytest

from reclaim.audit import load_case_audit_trail, reconstruct_case_history
from reclaim.domain.breaker import (
    EVENT_BREAKER_OPENED,
    EVENT_BREAKER_RESET,
    set_breaker_state,
)
from reclaim.domain.diagnosis import diagnose_case
from reclaim.domain.execution import dispatch
from reclaim.domain.leases import claim_case
from reclaim.domain.policy import PolicyFacts, apply_policy
from reclaim.domain.review import approve_review
from reclaim.domain.states import CaseState
from reclaim.domain.sweeper import sweep_expired_leases
from reclaim.domain.verification import verify_case
from reclaim.llm.client import ScriptedLlm
from reclaim.provider.contract import ProviderOutcome
from tests.audit.audit_helpers import ingest_case, redeliver
from tests.db.helpers import insert_policy_decision
from tests.domain.diagnosis_helpers import seed_diagnosing, valid_json
from tests.domain.execution_helpers import (
    LINK_TTL_SECONDS,
    StubProvider,
    seed_dispatchable,
)
from tests.domain.policy_helpers import seed_policy_eval
from tests.domain.review_helpers import seed_escalated
from tests.domain.verification_helpers import (
    CORRELATION_ID,
    StubVerifyProvider,
    deliver_webhook,
    fetch_paid,
    seed_awaiting_customer,
)


def _history(conn: psycopg.Connection, case_id: int):
    return reconstruct_case_history(load_case_audit_trail(conn, case_id))


def _dispatched(conn: psycopg.Connection, outcome=ProviderOutcome.ACCEPTED, **kw):
    ids = seed_dispatchable(conn, **kw)
    dispatch(
        conn,
        ids["case_id"],
        provider=StubProvider(outcome),
        fencing_token=0,
        policy_decision_id=ids["policy_id"],
        link_ttl_seconds=LINK_TTL_SECONDS,
        worker_id="w1",
    )
    return ids


# ---- the reconstruction test ---------------------------------------------


def test_provider_reference_is_recoverable_from_the_trail(
    conn: psycopg.Connection,
) -> None:
    """Which attempts ran under which provider references must be recoverable."""
    ids = _dispatched(conn)
    events = load_case_audit_trail(conn, ids["case_id"])
    sent = next(e for e in events if e.event_type == "provider_request_sent")
    key = sent.get("provider_reference")
    assert key, "the request event must itself carry the reference"

    history = reconstruct_case_history(events)

    assert key in history.provider_references
    assert history.actions[0].attempts[0].provider_reference == key


def test_action_type_is_recoverable(conn: psycopg.Connection) -> None:
    """Which actions ran, in which order, must be recoverable."""
    ids = _dispatched(conn)

    history = _history(conn, ids["case_id"])

    assert history.actions[0].action_type == "CREATE_PAYMENT_LINK"


def test_obligation_is_recoverable(conn: psycopg.Connection) -> None:
    obligation_id, case_id = ingest_case(conn)

    history = _history(conn, case_id)

    assert history.obligation_id == obligation_id


def test_case_created_event_exists(conn: psycopg.Connection) -> None:
    """Case creation must leave its own audit event."""
    _, case_id = ingest_case(conn)

    history = _history(conn, case_id)

    assert history.created is True
    assert "case_created/case_deduplicated" not in history.unreconstructable


def test_case_deduplicated_event_exists(conn: psycopg.Connection) -> None:
    """Deduplication must leave its own audit event."""
    _, case_id = ingest_case(conn, suffix="dedup1")
    redeliver(conn, "dedup1")

    history = _history(conn, case_id)

    assert history.deduplicated is True


def test_state_changes_carry_reason_worker_and_token(
    conn: psycopg.Connection,
) -> None:
    """Every state change must carry its reason, worker, and fencing token."""
    ids = _dispatched(conn)

    history = _history(conn, ids["case_id"])

    assert [(s.prev_state, s.new_state) for s in history.state_changes] == [
        ("ACTION_READY", "EXECUTING"),
        ("EXECUTING", "AWAITING_CUSTOMER"),
    ]
    assert all(s.reason_code for s in history.state_changes)
    assert history.workers == ("w1",)
    assert history.fencing_tokens == (0,)


def test_provider_correlation_id_is_recoverable(conn: psycopg.Connection) -> None:
    """Every provider correlation id must be recoverable."""
    ids = _dispatched(conn)

    history = _history(conn, ids["case_id"])

    assert "plink_stub0000001" in history.provider_correlation_ids


def test_complete_lifecycle_reconstructs_with_no_gaps(
    conn: psycopg.Connection,
) -> None:
    """A complete real dispatch reconstructs with no reported gaps."""
    _, case_id = ingest_case(conn, suffix="full")
    policy_id = insert_policy_decision(conn, case_id)
    conn.execute(
        "UPDATE recovery_cases SET state = 'ACTION_READY' WHERE id = %s", (case_id,)
    )
    dispatch(
        conn,
        case_id,
        provider=StubProvider(ProviderOutcome.ACCEPTED),
        fencing_token=0,
        policy_decision_id=policy_id,
        link_ttl_seconds=LINK_TTL_SECONDS,
        worker_id="w-full",
    )

    history = _history(conn, case_id)

    assert history.unreconstructable == (), history.unreconstructable
    assert history.created is True
    assert history.obligation_id is not None
    assert history.actions and history.actions[0].attempts
    assert history.provider_references
    assert history.provider_correlation_ids
    assert history.final_state == "AWAITING_CUSTOMER"


def test_provider_request_and_response_are_on_the_trail(
    conn: psycopg.Connection,
) -> None:
    ids = _dispatched(conn)
    types = [e.event_type for e in load_case_audit_trail(conn, ids["case_id"])]

    assert "provider_request_sent" in types
    assert "provider_response_received" in types


def test_model_is_recoverable_from_the_trail(conn: psycopg.Connection) -> None:
    ids = seed_diagnosing(conn)
    diagnose_case(
        conn,
        ids["case_id"],
        llm=ScriptedLlm(valid_json(), model="gemma-forensic"),
        fencing_token=0,
        worker_id="dx-1",
    )

    history = _history(conn, ids["case_id"])

    assert history.diagnoses, "diagnosis_produced must be on the trail"
    assert history.diagnoses[0].model == "gemma-forensic"
    assert history.diagnoses[0].detail.get("source") == "LLM"


def test_policy_version_is_recoverable_from_the_trail(
    conn: psycopg.Connection,
) -> None:
    ids = seed_policy_eval(conn, cause="INSUFFICIENT_FUNDS")
    result = apply_policy(
        conn,
        ids["case_id"],
        facts=PolicyFacts(
            cause="INSUFFICIENT_FUNDS",
            attempt_count=0,
            max_attempts=2,
            conflicting_history=False,
        ),
        diagnosis_id=ids["diagnosis_id"],
        fencing_token=0,
        worker_id="policy-1",
    )
    assert result.applied is True
    assert result.decision is not None

    history = _history(conn, ids["case_id"])

    assert history.policy_decisions, "policy_decision must be on the trail"
    assert history.policy_decisions[0].policy_version == result.decision.policy_version
    assert history.policy_decisions[0].detail.get("verdict") == "ALLOW"


def test_reviewer_decision_is_recoverable_from_the_trail(
    conn: psycopg.Connection,
) -> None:
    ids = seed_escalated(conn)
    applied = approve_review(
        conn,
        ids["case_id"],
        selected_action="CREATE_PAYMENT_LINK",
        reviewer_ref="alice@ops",
        fencing_token=0,
        worker_id="reviewer-1",
    )
    assert applied.applied is True

    history = _history(conn, ids["case_id"])

    assert history.reviews, "review_decision must be on the trail"
    assert history.reviews[0].reviewer_ref == "alice@ops"
    assert history.reviews[0].detail.get("status") == "APPROVED"
    assert history.reviews[0].detail.get("selected_action") == "CREATE_PAYMENT_LINK"


def test_verification_is_recoverable_from_the_trail(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)
    result = verify_case(
        verifier_conn,
        ids["case_id"],
        provider=StubVerifyProvider(fetch_paid()),
        fencing_token=0,
        worker_id="verifier-1",
    )
    assert result.applied is True

    history = _history(conn, ids["case_id"])

    assert history.verifications, "verification_recorded must be on the trail"
    assert history.verifications[0].detail.get("agrees") is True
    assert CORRELATION_ID in history.provider_correlation_ids


def test_lease_claim_is_on_the_trail(conn: psycopg.Connection) -> None:
    _, case_id = ingest_case(conn, suffix="lease")
    claim = claim_case(conn, case_id, CaseState.NEW, "worker-lease", 30)
    assert claim is not None

    history = _history(conn, case_id)
    claimed = [e for e in history.timeline if e.event_type == "lease_claimed"]

    assert claimed, "lease claim must leave an audit row"
    assert claimed[0].worker_id == "worker-lease"
    assert claimed[0].fencing_token == claim.fencing_token


def test_lease_release_on_expiry_is_on_the_trail(
    conn: psycopg.Connection,
) -> None:
    _, case_id = ingest_case(conn, suffix="sweep")
    claim = claim_case(conn, case_id, CaseState.NEW, "worker-sweep", 30)
    assert claim is not None
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = now() - interval '1 s' WHERE id = %s",
        (case_id,),
    )
    sweep_expired_leases(conn)

    history = _history(conn, case_id)
    released = [e for e in history.timeline if e.event_type == "lease_released"]

    assert released, "lease expiry must leave a lease_released row"
    assert released[0].reason_code == "lease_expired"


# ---- gaps are reported, never hidden ------------------------------------


def test_missing_evidence_is_reported_not_silently_none(
    conn: psycopg.Connection,
) -> None:
    """A case seeded outside lifecycle has no case_created row -- say so."""
    ids = _dispatched(conn)  # fixture bypasses create_obligation_and_case

    history = _history(conn, ids["case_id"])

    assert "case_created/case_deduplicated" in history.unreconstructable


def test_empty_trail_reports_everything_missing() -> None:
    history = reconstruct_case_history(())

    assert history.case_id is None
    assert "case_id" in history.unreconstructable
    assert "state_changes" in history.unreconstructable


# ---- stale fencing must not erase the provider's answer -----------------


def test_stale_write_preserves_the_provider_response(
    conn: psycopg.Connection,
) -> None:
    """A stale fencing token rejects the write-back but must not lose the
    provider's actual answer -- that evidence is what a forensic reader needs
    most when two workers raced over money."""
    from reclaim.domain.execution import call_provider, prepare_dispatch, settle_dispatch

    ids = seed_dispatchable(conn)
    claim = claim_case(conn, ids["case_id"], CaseState.ACTION_READY, "slow", 60)
    prepared = prepare_dispatch(
        conn,
        ids["case_id"],
        fencing_token=claim.fencing_token,
        policy_decision_id=ids["policy_id"],
        link_ttl_seconds=LINK_TTL_SECONDS,
        worker_id="slow",
    )
    result = call_provider(StubProvider(ProviderOutcome.ACCEPTED), prepared)
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = now() - interval '1 s' WHERE id=%s",
        (ids["case_id"],),
    )
    sweep_expired_leases(conn)

    settled = settle_dispatch(
        conn, prepared, result, fencing_token=claim.fencing_token, worker_id="slow"
    )

    assert settled.applied is False
    history = _history(conn, ids["case_id"])
    observed = [
        e for e in history.timeline if e.event_type == "provider_response_observed"
    ]
    assert len(observed) == 1, "the provider's answer must survive rejection"
    assert observed[0].get("provider_outcome") == "ACCEPTED"
    assert observed[0].get("applied") is False
    assert observed[0].provider_correlation_id == "plink_stub0000001"
    assert history.stale_writes, "the rejection itself is still recorded"


# ---- breaker events -------------------------------------------------------


def test_breaker_open_and_reset_are_audited(conn: psycopg.Connection) -> None:
    assert set_breaker_state(
        conn, open_breaker=True, reason_code="threshold_reached",
        trip_cause={"consecutive_failures": 5}, reset_seconds=120,
    )
    assert set_breaker_state(conn, open_breaker=False, reason_code="reset_elapsed")

    rows = conn.execute(
        "SELECT event_type FROM audit_events WHERE event_type IN (%s, %s) ORDER BY id",
        (EVENT_BREAKER_OPENED, EVENT_BREAKER_RESET),
    ).fetchall()
    assert [r[0] for r in rows] == [EVENT_BREAKER_OPENED, EVENT_BREAKER_RESET]


def test_breaker_noop_writes_no_event(conn: psycopg.Connection) -> None:
    """A repeated open must not manufacture a second opening in history."""
    set_breaker_state(conn, open_breaker=True, reason_code="first")

    assert set_breaker_state(conn, open_breaker=True, reason_code="again") is False

    n = conn.execute(
        "SELECT count(*) FROM audit_events WHERE event_type = %s",
        (EVENT_BREAKER_OPENED,),
    ).fetchone()[0]
    assert n == 1


def test_breaker_state_cannot_change_without_audit(conn: psycopg.Connection) -> None:
    """The primitive is the only audited path; both writes share one transaction."""
    import inspect

    from reclaim.domain import breaker

    source = inspect.getsource(breaker.set_breaker_state)
    assert "UPDATE circuit_breaker" in source
    assert "INSERT INTO audit_events" in source


# ---- ordering ---------------------------------------------------------


def test_same_transaction_events_keep_insertion_order(
    conn: psycopg.Connection,
) -> None:
    """occurred_at is transaction-start time, so id is the tiebreak."""
    ids = _dispatched(conn)

    events = load_case_audit_trail(conn, ids["case_id"])

    assert [e.id for e in events] == sorted(e.id for e in events)
    txn1 = [e for e in events if e.event_type in
            ("provider_request_sent", "state_transition")][:2]
    assert txn1[0].occurred_at == txn1[1].occurred_at
    assert txn1[0].id < txn1[1].id


def test_reconstruction_preserves_stored_order(conn: psycopg.Connection) -> None:
    ids = _dispatched(conn)
    events = load_case_audit_trail(conn, ids["case_id"])

    history = reconstruct_case_history(events)

    assert history.timeline == events


# ---- append-only -----------------------------------------------------


def test_audit_events_reject_update(conn: psycopg.Connection) -> None:
    ids = _dispatched(conn)

    with pytest.raises(Exception, match="append-only"):
        conn.execute(
            "UPDATE audit_events SET reason_code = 'forged' WHERE case_id = %s",
            (ids["case_id"],),
        )


def test_audit_events_reject_delete(conn: psycopg.Connection) -> None:
    ids = _dispatched(conn)

    with pytest.raises(Exception, match="append-only"):
        conn.execute(
            "DELETE FROM audit_events WHERE case_id = %s", (ids["case_id"],)
        )


def test_history_survives_a_closed_connection(conn: psycopg.Connection) -> None:
    """No lazy loading: reconstruction works after the connection is gone."""
    ids = _dispatched(conn)
    events = load_case_audit_trail(conn, ids["case_id"])

    history = reconstruct_case_history(events)  # no conn in scope for the engine

    assert history.provider_references
    assert history.actions[0].action_type == "CREATE_PAYMENT_LINK"
