"""Policy application: persistence, transitions, audit, and boundaries."""

from __future__ import annotations

import inspect

import psycopg
import pytest

from reclaim.config import load_policy_config
from reclaim.domain.policy import (
    PolicyBlocked,
    PolicyFacts,
    apply_policy,
    evaluate_once,
)
from reclaim.domain.states import CaseState
from tests.domain.policy_helpers import (
    case_row,
    policy_audit_count,
    policy_decisions_for,
    seed_policy_eval,
)


def _facts(
    ids: dict,
    *,
    conflicting_history: bool = False,
    attempt_count: int | None = None,
    cause: str | None = None,
) -> PolicyFacts:
    return PolicyFacts(
        cause=cause or ids["cause"],
        attempt_count=attempt_count if attempt_count is not None else 0,
        max_attempts=2,
        conflicting_history=conflicting_history,
    )


def test_apply_allow_transitions_to_action_ready(conn: psycopg.Connection) -> None:
    ids = seed_policy_eval(conn, cause="INSUFFICIENT_FUNDS")
    result = apply_policy(
        conn,
        ids["case_id"],
        facts=_facts(ids),
        diagnosis_id=ids["diagnosis_id"],
        fencing_token=0,
    )
    assert result.applied is True
    assert result.case_state is CaseState.ACTION_READY
    assert case_row(conn, ids["case_id"])["state"] == "ACTION_READY"
    row = policy_decisions_for(conn, ids["case_id"])[0]
    assert row["verdict"] == "ALLOW"
    assert row["selected_action"] == "CREATE_PAYMENT_LINK"
    assert policy_audit_count(conn, ids["case_id"]) == 1


def test_apply_escalate_transitions_to_escalated(conn: psycopg.Connection) -> None:
    ids = seed_policy_eval(conn, cause="CARD_DECLINED_ISSUER")
    result = apply_policy(
        conn,
        ids["case_id"],
        facts=_facts(ids),
        diagnosis_id=ids["diagnosis_id"],
        fencing_token=0,
    )
    assert result.applied is True
    assert result.case_state is CaseState.ESCALATED
    assert policy_decisions_for(conn, ids["case_id"])[0]["selected_action"] is None


def test_apply_no_action_transitions_to_verified_failed(conn: psycopg.Connection) -> None:
    ids = seed_policy_eval(conn, cause="RISK_BLOCKED")
    result = apply_policy(
        conn,
        ids["case_id"],
        facts=_facts(ids),
        diagnosis_id=ids["diagnosis_id"],
        fencing_token=0,
    )
    assert result.applied is True
    assert result.case_state is CaseState.VERIFIED_FAILED
    assert policy_decisions_for(conn, ids["case_id"])[0]["verdict"] == "NO_ACTION"


def test_apply_persists_ambiguity_booleans(conn: psycopg.Connection) -> None:
    ids = seed_policy_eval(conn, cause="WEIRD_CAUSE")
    apply_policy(
        conn,
        ids["case_id"],
        facts=_facts(ids, conflicting_history=True, cause="WEIRD_CAUSE"),
        diagnosis_id=ids["diagnosis_id"],
        fencing_token=0,
    )
    row = policy_decisions_for(conn, ids["case_id"])[0]
    assert row["lookup_miss"] is True
    assert row["conflicting_history"] is True
    assert row["ambiguity_signal"] is True


def test_apply_without_diagnosis_is_blocked(conn: psycopg.Connection) -> None:
    ids = seed_policy_eval(conn)
    conn.execute("DELETE FROM diagnoses WHERE case_id = %s", (ids["case_id"],))
    with pytest.raises(PolicyBlocked, match="not present"):
        apply_policy(
            conn,
            ids["case_id"],
            facts=_facts(ids),
            diagnosis_id=ids["diagnosis_id"],
            fencing_token=0,
        )


def test_stale_fence_rejects_without_policy_row(conn: psycopg.Connection) -> None:
    ids = seed_policy_eval(conn)
    apply_policy(
        conn,
        ids["case_id"],
        facts=_facts(ids),
        diagnosis_id=ids["diagnosis_id"],
        fencing_token=0,
    )
    second = apply_policy(
        conn,
        ids["case_id"],
        facts=_facts(ids),
        diagnosis_id=ids["diagnosis_id"],
        fencing_token=0,
    )
    assert second.applied is False
    assert len(policy_decisions_for(conn, ids["case_id"])) == 1


def test_apply_rolls_back_when_audit_side_effect_crashes(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INSERT + fenced_transition must be atomic; a crash rolls back the decision row."""
    ids = seed_policy_eval(conn)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("crash after policy insert")

    monkeypatch.setattr("reclaim.domain.policy._audit_policy_decision", _boom)

    with pytest.raises(RuntimeError, match="crash after policy insert"):
        apply_policy(
            conn,
            ids["case_id"],
            facts=_facts(ids),
            diagnosis_id=ids["diagnosis_id"],
            fencing_token=0,
        )

    assert policy_decisions_for(conn, ids["case_id"]) == []
    assert case_row(conn, ids["case_id"])["state"] == "POLICY_EVAL"
    assert policy_audit_count(conn, ids["case_id"]) == 0


def test_evaluate_once_requires_explicit_conflicting_history() -> None:
    """No silent default to False — the parameter is mandatory."""
    params = inspect.signature(evaluate_once).parameters
    assert "conflicting_history" in params
    assert params["conflicting_history"].default is inspect.Parameter.empty


def test_evaluate_once_claims_and_applies(conn: psycopg.Connection) -> None:
    ids = seed_policy_eval(conn)
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = '-infinity' WHERE id = %s",
        (ids["case_id"],),
    )
    result = evaluate_once(conn, conflicting_history=False)
    assert result is not None
    assert result.applied is True
    assert case_row(conn, ids["case_id"])["state"] == "ACTION_READY"


def test_recommended_action_does_not_change_applied_outcome(
    conn: psycopg.Connection,
) -> None:
    """Diagnosis recommends RETRY_CHARGE; policy still emits CREATE_PAYMENT_LINK."""
    ids = seed_policy_eval(
        conn,
        cause="INSUFFICIENT_FUNDS",
        recommended_action="RETRY_CHARGE",
    )
    result = apply_policy(
        conn,
        ids["case_id"],
        facts=_facts(ids),
        diagnosis_id=ids["diagnosis_id"],
        fencing_token=0,
        config=load_policy_config(),
    )
    row = policy_decisions_for(conn, ids["case_id"])[0]
    assert result.applied is True
    assert row["selected_action"] == "CREATE_PAYMENT_LINK"
    assert row["selected_action"] != "RETRY_CHARGE"
