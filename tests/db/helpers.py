"""Shared helpers for database constraint tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg


def insert_obligation(
    conn: psycopg.Connection,
    *,
    anchor_kind: str = "ORDER",
    anchor_key: str = "ord_1",
    anchor_canonical: str = "order:ord_1",
    amount_minor: int = 10_000,
    currency: str = "INR",
    customer_ref: str = "cust_1",
    source_event_id: str = "evt_1",
) -> int:
    row = conn.execute(
        """
        INSERT INTO financial_obligations (
            anchor_kind, anchor_key, anchor_canonical,
            amount_minor, currency, customer_ref, source_event_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            anchor_kind,
            anchor_key,
            anchor_canonical,
            amount_minor,
            currency,
            customer_ref,
            source_event_id,
        ),
    ).fetchone()
    assert row is not None
    return row[0]


def insert_case(
    conn: psycopg.Connection,
    obligation_id: int,
    *,
    state: str = "NEW",
    ttl_budget_ms: int = 72 * 60 * 60 * 1000,
    max_attempts: int = 2,
    attempt_count: int = 0,
    active_since: datetime | None = None,
    worker_id: str | None = None,
    recovered_amount_minor: int = 0,
) -> int:
    if active_since is None and state not in {
        "HALTED",
        "VERIFIED_RECOVERED",
        "VERIFIED_FAILED",
        "EXPIRED_UNRESOLVED",
    }:
        active_since = datetime.now(timezone.utc)

    row = conn.execute(
        """
        INSERT INTO recovery_cases (
            obligation_id, state, ttl_budget_ms, max_attempts,
            attempt_count, active_since, worker_id, recovered_amount_minor
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            obligation_id,
            state,
            ttl_budget_ms,
            max_attempts,
            attempt_count,
            active_since,
            worker_id,
            recovered_amount_minor,
        ),
    ).fetchone()
    assert row is not None
    return row[0]


def insert_diagnosis(conn: psycopg.Connection, case_id: int, **overrides: Any) -> int:
    values = {
        "case_id": case_id,
        "source": "LLM",
        "model": "test-model",
        "prompt_version": "v1",
        "cause": "insufficient_funds",
        "recommended_action": "CREATE_PAYMENT_LINK",
    }
    values.update(overrides)
    row = conn.execute(
        """
        INSERT INTO diagnoses (
            case_id, source, model, prompt_version, cause, recommended_action,
            reasoning, confidence, llm_retry_count
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            values["case_id"],
            values["source"],
            values.get("model"),
            values["prompt_version"],
            values["cause"],
            values.get("recommended_action"),
            values.get("reasoning"),
            values.get("confidence"),
            values.get("llm_retry_count", 0),
        ),
    ).fetchone()
    assert row is not None
    return row[0]


def insert_policy_decision(conn: psycopg.Connection, case_id: int, **overrides: Any) -> int:
    values = {
        "case_id": case_id,
        "diagnosis_id": None,
        "policy_version": "1.0",
        "lookup_miss": False,
        "conflicting_history": False,
        "ambiguity_signal": False,
        "verdict": "ALLOW",
        "selected_action": "CREATE_PAYMENT_LINK",
        "reason_code": "allow_link",
    }
    values.update(overrides)
    row = conn.execute(
        """
        INSERT INTO policy_decisions (
            case_id, diagnosis_id, policy_version, lookup_miss, conflicting_history,
            ambiguity_signal, verdict, selected_action, reason_code
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            values["case_id"],
            values["diagnosis_id"],
            values["policy_version"],
            values["lookup_miss"],
            values["conflicting_history"],
            values["ambiguity_signal"],
            values["verdict"],
            values.get("selected_action"),
            values["reason_code"],
        ),
    ).fetchone()
    assert row is not None
    return row[0]


def insert_action(
    conn: psycopg.Connection,
    case_id: int,
    policy_decision_id: int,
    *,
    sequence_no: int = 1,
    status: str = "PROPOSED",
    action_type: str = "CREATE_PAYMENT_LINK",
    superseded_by: int | None = None,
    provider_expires_at: datetime | None = None,
    action_deadline_at: datetime | None = None,
    resolved_at: datetime | None = None,
) -> int:
    row = conn.execute(
        """
        INSERT INTO recovery_actions (
            case_id, action_type, status, sequence_no, policy_decision_id,
            superseded_by, provider_expires_at, action_deadline_at, resolved_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            case_id,
            action_type,
            status,
            sequence_no,
            policy_decision_id,
            superseded_by,
            provider_expires_at,
            action_deadline_at,
            resolved_at,
        ),
    ).fetchone()
    assert row is not None
    return row[0]


def insert_attempt(
    conn: psycopg.Connection,
    action_id: int,
    case_id: int,
    *,
    attempt_no: int = 1,
    idempotency_key: str = "key-1",
    provider_reference: str | None = None,
    state: str = "PREPARED",
    amount_minor: int = 10_000,
    currency: str = "INR",
) -> int:
    row = conn.execute(
        """
        INSERT INTO execution_attempts (
            action_id, case_id, attempt_no, idempotency_key, provider_reference,
            state, amount_minor, currency
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            action_id,
            case_id,
            attempt_no,
            idempotency_key,
            provider_reference,
            state,
            amount_minor,
            currency,
        ),
    ).fetchone()
    assert row is not None
    return row[0]


def build_action_graph(conn: psycopg.Connection) -> dict[str, int]:
    obligation_id = insert_obligation(conn)
    case_id = insert_case(conn, obligation_id)
    diagnosis_id = insert_diagnosis(conn, case_id)
    policy_id = insert_policy_decision(conn, case_id, diagnosis_id=diagnosis_id)
    action_id = insert_action(conn, case_id, policy_id)
    return {
        "obligation_id": obligation_id,
        "case_id": case_id,
        "diagnosis_id": diagnosis_id,
        "policy_id": policy_id,
        "action_id": action_id,
    }


def future_ts(minutes: int = 60) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)
