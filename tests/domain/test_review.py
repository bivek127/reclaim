"""Human review: lifecycle, I10, Executor handoff, provenance."""

from __future__ import annotations

from datetime import datetime, timezone

import psycopg
import pytest

from reclaim.domain.breaker import BreakerOpen
from reclaim.domain.execution import BudgetExhausted, DispatchAborted, dispatch, prepare_dispatch
from reclaim.domain.leases import claim_case
from reclaim.domain.review import (
    ReviewBlocked,
    approve_review,
    expire_reviews,
    load_review_evidence,
    reject_review,
)
from reclaim.domain.states import CaseState
from reclaim.domain.sweeper import expire_ttl
from tests.db.helpers import insert_case, insert_obligation
from tests.domain.execution_helpers import LINK_TTL_SECONDS, StubProvider
from tests.domain.review_helpers import (
    actions_for,
    attempts_for,
    audit_types,
    case_row,
    policy_decisions_for,
    provider_requests_for,
    reviews_for,
    seed_escalated,
)


def test_policy_escalation_creates_exactly_one_pending_review(
    conn: psycopg.Connection,
) -> None:
    ids = seed_escalated(conn)
    revs = reviews_for(conn, ids["case_id"])
    assert len(revs) == 1
    assert revs[0]["status"] == "PENDING"
    assert revs[0]["review_expires_at"] > datetime.now(timezone.utc)


def test_ttl_escalation_creates_pending_review_and_provenance(
    conn: psycopg.Connection,
) -> None:
    obligation_id = insert_obligation(
        conn,
        anchor_key="ord_ttl_rev",
        anchor_canonical="order:ord_ttl_rev",
        source_event_id="evt_ttl_rev",
    )
    case_id = insert_case(conn, obligation_id, state="DIAGNOSING")
    conn.execute(
        """
        UPDATE recovery_cases
           SET ttl_budget_ms = 1,
               active_elapsed_ms = 5000,
               active_since = now() - interval '2 seconds'
         WHERE id = %s
        """,
        (case_id,),
    )
    result = expire_ttl(conn)
    assert result.expired >= 1
    assert case_row(conn, case_id)["state"] == "ESCALATED"
    revs = reviews_for(conn, case_id)
    assert len(revs) == 1
    assert revs[0]["status"] == "PENDING"
    decisions = policy_decisions_for(conn, case_id)
    assert len(decisions) == 1
    assert decisions[0]["verdict"] == "ESCALATE"
    assert decisions[0]["reason_code"] == "ttl_exhausted"
    assert decisions[0]["selected_action"] is None
    assert decisions[0]["diagnosis_id"] is None


def test_policy_escalation_reuses_policy_decision_row(conn: psycopg.Connection) -> None:
    ids = seed_escalated(conn)
    decisions = policy_decisions_for(conn, ids["case_id"])
    assert len(decisions) == 1
    assert decisions[0]["id"] == ids["policy_decision_id"]
    assert decisions[0]["reason_code"].startswith("policy_escalate")


def test_evidence_loader_shape(conn: psycopg.Connection) -> None:
    ids = seed_escalated(conn)
    evidence = load_review_evidence(conn, ids["case_id"])
    assert evidence.case_id == ids["case_id"]
    assert evidence.amount_minor > 0
    assert evidence.currency == "INR"
    assert evidence.diagnosis is not None
    assert evidence.diagnosis["cause"] == "CARD_DECLINED_ISSUER"
    assert evidence.policy is not None
    assert evidence.policy["reason_code"].startswith("policy_escalate")
    assert isinstance(evidence.audit_timeline, tuple)


def test_approve_creates_proposed_only(conn: psycopg.Connection) -> None:
    ids = seed_escalated(conn)
    result = approve_review(
        conn,
        ids["case_id"],
        selected_action="CREATE_PAYMENT_LINK",
        reviewer_ref="alice",
        fencing_token=0,
    )
    assert result.applied is True
    assert case_row(conn, ids["case_id"])["state"] == "ESCALATED"
    assert case_row(conn, ids["case_id"])["attempt_count"] == 0
    assert attempts_for(conn, ids["case_id"]) == []
    assert provider_requests_for(conn, ids["case_id"]) == []
    actions = actions_for(conn, ids["case_id"])
    assert len(actions) == 1
    assert actions[0]["status"] == "PROPOSED"
    assert actions[0]["action_type"] == "CREATE_PAYMENT_LINK"
    assert actions[0]["policy_decision_id"] == ids["policy_decision_id"]
    rev = reviews_for(conn, ids["case_id"])[0]
    assert rev["status"] == "APPROVED"
    assert rev["reviewer_ref"] == "alice"
    assert "review_decision" in audit_types(conn, ids["case_id"])


def test_approve_rejects_retry_charge_before_any_side_effect(
    conn: psycopg.Connection,
) -> None:
    """Human approval must not reopen unsupported RETRY_CHARGE."""
    ids = seed_escalated(conn)
    with pytest.raises(ReviewBlocked, match="RETRY_CHARGE"):
        approve_review(
            conn,
            ids["case_id"],
            selected_action="RETRY_CHARGE",
            reviewer_ref="alice",
            fencing_token=0,
        )
    assert case_row(conn, ids["case_id"])["state"] == "ESCALATED"
    assert case_row(conn, ids["case_id"])["attempt_count"] == 0
    assert reviews_for(conn, ids["case_id"])[0]["status"] == "PENDING"
    assert actions_for(conn, ids["case_id"]) == []
    assert attempts_for(conn, ids["case_id"]) == []
    assert provider_requests_for(conn, ids["case_id"]) == []


def test_approve_does_not_call_evaluate_or_llm(conn: psycopg.Connection) -> None:
    """Approval must not invent a second policy evaluation."""
    ids = seed_escalated(conn)
    before = policy_decisions_for(conn, ids["case_id"])
    approve_review(
        conn,
        ids["case_id"],
        selected_action="CREATE_PAYMENT_LINK",
        reviewer_ref="alice",
        fencing_token=0,
    )
    after = policy_decisions_for(conn, ids["case_id"])
    assert after == before


def test_reject(conn: psycopg.Connection) -> None:
    ids = seed_escalated(conn)
    result = reject_review(
        conn, ids["case_id"], reviewer_ref="bob", fencing_token=0
    )
    assert result.applied is True
    assert case_row(conn, ids["case_id"])["state"] == "VERIFIED_FAILED"
    assert reviews_for(conn, ids["case_id"])[0]["status"] == "REJECTED"
    assert actions_for(conn, ids["case_id"]) == []
    assert attempts_for(conn, ids["case_id"]) == []


def test_review_expiry(conn: psycopg.Connection) -> None:
    ids = seed_escalated(conn)
    conn.execute(
        "UPDATE human_reviews SET review_expires_at = now() - interval '1 second' "
        "WHERE case_id = %s",
        (ids["case_id"],),
    )
    n = expire_reviews(conn)
    assert n == 1
    assert case_row(conn, ids["case_id"])["state"] == "EXPIRED_UNRESOLVED"
    assert reviews_for(conn, ids["case_id"])[0]["status"] == "EXPIRED"
    assert actions_for(conn, ids["case_id"]) == []


def test_duplicate_approval_blocked(conn: psycopg.Connection) -> None:
    ids = seed_escalated(conn)
    assert approve_review(
        conn,
        ids["case_id"],
        selected_action="CREATE_PAYMENT_LINK",
        reviewer_ref="alice",
        fencing_token=0,
    ).applied
    with pytest.raises(ReviewBlocked):
        approve_review(
            conn,
            ids["case_id"],
            selected_action="CREATE_PAYMENT_LINK",
            reviewer_ref="alice",
            fencing_token=0,
        )


def test_duplicate_expiry_is_noop(conn: psycopg.Connection) -> None:
    ids = seed_escalated(conn)
    conn.execute(
        "UPDATE human_reviews SET review_expires_at = now() - interval '1 second' "
        "WHERE case_id = %s",
        (ids["case_id"],),
    )
    assert expire_reviews(conn) == 1
    assert expire_reviews(conn) == 0


def test_stale_fencing_reject_approve(conn: psycopg.Connection) -> None:
    ids = seed_escalated(conn)
    conn.execute(
        "UPDATE recovery_cases SET fencing_token = 9 WHERE id = %s",
        (ids["case_id"],),
    )
    result = approve_review(
        conn,
        ids["case_id"],
        selected_action="CREATE_PAYMENT_LINK",
        reviewer_ref="alice",
        fencing_token=0,
    )
    assert result.applied is False
    assert reviews_for(conn, ids["case_id"])[0]["status"] == "PENDING"
    assert actions_for(conn, ids["case_id"]) == []
    assert "stale_write_rejected" in audit_types(conn, ids["case_id"])


def test_approve_after_expiry_loses_on_state(conn: psycopg.Connection) -> None:
    """After the expiry job runs, a late approve loses on expected_state."""
    ids = seed_escalated(conn)
    conn.execute(
        "UPDATE human_reviews SET review_expires_at = now() - interval '1 second' "
        "WHERE case_id = %s",
        (ids["case_id"],),
    )
    assert expire_reviews(conn) == 1
    result = approve_review(
        conn,
        ids["case_id"],
        selected_action="CREATE_PAYMENT_LINK",
        reviewer_ref="alice",
        fencing_token=case_row(conn, ids["case_id"])["fencing_token"],
    )
    assert result.applied is False
    assert case_row(conn, ids["case_id"])["state"] == "EXPIRED_UNRESOLVED"
    assert actions_for(conn, ids["case_id"]) == []


def test_executor_rejects_escalated_without_proposed(conn: psycopg.Connection) -> None:
    ids = seed_escalated(conn)
    with pytest.raises(DispatchAborted):
        prepare_dispatch(
            conn,
            ids["case_id"],
            fencing_token=0,
            policy_decision_id=ids["policy_decision_id"],
            link_ttl_seconds=LINK_TTL_SECONDS,
        )
    assert case_row(conn, ids["case_id"])["state"] == "ESCALATED"
    assert attempts_for(conn, ids["case_id"]) == []


def test_executor_promotes_proposed(conn: psycopg.Connection) -> None:
    ids = seed_escalated(conn)
    approve_review(
        conn,
        ids["case_id"],
        selected_action="CREATE_PAYMENT_LINK",
        reviewer_ref="alice",
        fencing_token=0,
    )
    action_id = actions_for(conn, ids["case_id"])[0]["id"]
    claim = claim_case(conn, ids["case_id"], CaseState.ESCALATED, "exec", 60)
    assert claim is not None
    result = dispatch(
        conn,
        ids["case_id"],
        provider=StubProvider(),
        fencing_token=claim.fencing_token,
        policy_decision_id=ids["policy_decision_id"],
        link_ttl_seconds=LINK_TTL_SECONDS,
        worker_id="exec",
    )
    assert result.applied is True
    actions = actions_for(conn, ids["case_id"])
    assert len(actions) == 1
    assert actions[0]["id"] == action_id
    assert actions[0]["status"] == "LIVE"
    assert len(attempts_for(conn, ids["case_id"])) == 1
    assert case_row(conn, ids["case_id"])["attempt_count"] == 1


def test_review_approval_uses_same_executor(conn: psycopg.Connection) -> None:
    """I10: review → PROPOSED → same prepare_dispatch/dispatch path."""
    ids = seed_escalated(conn)
    approve_review(
        conn,
        ids["case_id"],
        selected_action="CREATE_PAYMENT_LINK",
        reviewer_ref="alice",
        fencing_token=0,
    )
    claim = claim_case(conn, ids["case_id"], CaseState.ESCALATED, "exec", 60)
    assert claim is not None
    dispatch(
        conn,
        ids["case_id"],
        provider=StubProvider(),
        fencing_token=claim.fencing_token,
        policy_decision_id=ids["policy_decision_id"],
        link_ttl_seconds=LINK_TTL_SECONDS,
        worker_id="exec",
    )
    assert case_row(conn, ids["case_id"])["state"] in {
        "AWAITING_CUSTOMER",
        "ATTEMPT_FAILED",
        "AMBIGUOUS",
    }
    assert provider_requests_for(conn, ids["case_id"])


def test_review_approval_respects_budget(conn: psycopg.Connection) -> None:
    ids = seed_escalated(conn, attempt_count=2)
    approve_review(
        conn,
        ids["case_id"],
        selected_action="CREATE_PAYMENT_LINK",
        reviewer_ref="alice",
        fencing_token=0,
    )
    assert case_row(conn, ids["case_id"])["attempt_count"] == 2
    claim = claim_case(conn, ids["case_id"], CaseState.ESCALATED, "exec", 60)
    assert claim is not None
    with pytest.raises(BudgetExhausted):
        prepare_dispatch(
            conn,
            ids["case_id"],
            fencing_token=claim.fencing_token,
            policy_decision_id=ids["policy_decision_id"],
            link_ttl_seconds=LINK_TTL_SECONDS,
        )
    assert case_row(conn, ids["case_id"])["state"] == "ESCALATED"
    assert case_row(conn, ids["case_id"])["attempt_count"] == 2
    assert attempts_for(conn, ids["case_id"]) == []
    assert actions_for(conn, ids["case_id"])[0]["status"] == "PROPOSED"


def test_review_approval_blocked_when_breaker_open(conn: psycopg.Connection) -> None:
    ids = seed_escalated(conn)
    approve_review(
        conn,
        ids["case_id"],
        selected_action="CREATE_PAYMENT_LINK",
        reviewer_ref="alice",
        fencing_token=0,
    )
    conn.execute(
        "UPDATE circuit_breaker SET state='OPEN', opened_at=now(), "
        "reset_after=now() + interval '120 seconds' WHERE id=1"
    )
    claim = claim_case(conn, ids["case_id"], CaseState.ESCALATED, "exec", 60)
    assert claim is not None
    with pytest.raises(BreakerOpen):
        prepare_dispatch(
            conn,
            ids["case_id"],
            fencing_token=claim.fencing_token,
            policy_decision_id=ids["policy_decision_id"],
            link_ttl_seconds=LINK_TTL_SECONDS,
        )
    assert case_row(conn, ids["case_id"])["state"] == "ESCALATED"
    assert case_row(conn, ids["case_id"])["attempt_count"] == 0
    assert attempts_for(conn, ids["case_id"]) == []
    assert actions_for(conn, ids["case_id"])[0]["status"] == "PROPOSED"


def test_approve_allowed_while_breaker_open(conn: psycopg.Connection) -> None:
    ids = seed_escalated(conn)
    conn.execute(
        "UPDATE circuit_breaker SET state='OPEN', opened_at=now(), "
        "reset_after=now() + interval '120 seconds' WHERE id=1"
    )
    result = approve_review(
        conn,
        ids["case_id"],
        selected_action="CREATE_PAYMENT_LINK",
        reviewer_ref="alice",
        fencing_token=0,
    )
    assert result.applied is True
    assert actions_for(conn, ids["case_id"])[0]["status"] == "PROPOSED"


def test_crash_rollback_mid_approve(conn: psycopg.Connection) -> None:
    ids = seed_escalated(conn)
    try:
        with conn.transaction():
            approve_review(
                conn,
                ids["case_id"],
                selected_action="CREATE_PAYMENT_LINK",
                reviewer_ref="alice",
                fencing_token=0,
            )
            raise RuntimeError("simulated crash")
    except RuntimeError:
        pass
    assert reviews_for(conn, ids["case_id"])[0]["status"] == "PENDING"
    assert actions_for(conn, ids["case_id"]) == []


def test_committed_proposed_survives(conn: psycopg.Connection) -> None:
    ids = seed_escalated(conn)
    approve_review(
        conn,
        ids["case_id"],
        selected_action="CREATE_PAYMENT_LINK",
        reviewer_ref="alice",
        fencing_token=0,
    )
    assert actions_for(conn, ids["case_id"])[0]["status"] == "PROPOSED"
    assert reviews_for(conn, ids["case_id"])[0]["status"] == "APPROVED"


def test_forensic_audit_chain(conn: psycopg.Connection) -> None:
    ids = seed_escalated(conn)
    approve_review(
        conn,
        ids["case_id"],
        selected_action="CREATE_PAYMENT_LINK",
        reviewer_ref="alice",
        fencing_token=0,
    )
    claim = claim_case(conn, ids["case_id"], CaseState.ESCALATED, "exec", 60)
    assert claim is not None
    dispatch(
        conn,
        ids["case_id"],
        provider=StubProvider(),
        fencing_token=claim.fencing_token,
        policy_decision_id=ids["policy_decision_id"],
        link_ttl_seconds=LINK_TTL_SECONDS,
        worker_id="exec",
    )
    types = audit_types(conn, ids["case_id"])
    assert "policy_decision" in types
    assert "review_decision" in types
    assert "provider_request_sent" in types
    assert "state_transition" in types
    review_audit = conn.execute(
        """
        SELECT reviewer_ref, detail FROM audit_events
         WHERE case_id = %s AND event_type = 'review_decision'
        """,
        (ids["case_id"],),
    ).fetchone()
    assert review_audit is not None
    assert review_audit[0] == "alice"
    assert review_audit[1]["status"] == "APPROVED"
    assert review_audit[1]["action_id"] is not None
