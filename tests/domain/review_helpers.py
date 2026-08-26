"""Shared fixtures for human-review tests."""

from __future__ import annotations

from typing import Any

import psycopg

from reclaim.domain.policy import PolicyFacts, apply_policy
from reclaim.domain.states import CaseState
from tests.domain.policy_helpers import seed_policy_eval


def seed_escalated(
    conn: psycopg.Connection,
    *,
    cause: str = "CARD_DECLINED_ISSUER",
    attempt_count: int = 0,
) -> dict[str, Any]:
    """POLICY_EVAL → ESCALATED via policy evaluation (creates PENDING review + policy row)."""
    ids = seed_policy_eval(conn, cause=cause, attempt_count=attempt_count)
    result = apply_policy(
        conn,
        ids["case_id"],
        facts=PolicyFacts(
            cause=cause,
            attempt_count=attempt_count,
            max_attempts=2,
            conflicting_history=False,
        ),
        diagnosis_id=ids["diagnosis_id"],
        fencing_token=0,
    )
    assert result.applied is True
    assert result.case_state is CaseState.ESCALATED
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = '-infinity' WHERE id = %s",
        (ids["case_id"],),
    )
    return {
        **ids,
        "policy_decision_id": result.policy_decision_id,
        "fencing_token": 0,
    }


def reviews_for(conn: psycopg.Connection, case_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, status, reviewer_ref, selected_action, review_expires_at, decided_at
          FROM human_reviews WHERE case_id = %s ORDER BY id
        """,
        (case_id,),
    ).fetchall()
    return [
        {
            "id": r[0],
            "status": r[1],
            "reviewer_ref": r[2],
            "selected_action": r[3],
            "review_expires_at": r[4],
            "decided_at": r[5],
        }
        for r in rows
    ]


def actions_for(conn: psycopg.Connection, case_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, status, action_type, policy_decision_id, sequence_no
          FROM recovery_actions WHERE case_id = %s ORDER BY id
        """,
        (case_id,),
    ).fetchall()
    return [
        {
            "id": r[0],
            "status": r[1],
            "action_type": r[2],
            "policy_decision_id": r[3],
            "sequence_no": r[4],
        }
        for r in rows
    ]


def case_row(conn: psycopg.Connection, case_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT state, attempt_count, fencing_token
          FROM recovery_cases WHERE id = %s
        """,
        (case_id,),
    ).fetchone()
    assert row is not None
    return {"state": row[0], "attempt_count": row[1], "fencing_token": row[2]}


def attempts_for(conn: psycopg.Connection, case_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, state FROM execution_attempts WHERE case_id = %s ORDER BY id",
        (case_id,),
    ).fetchall()
    return [{"id": r[0], "state": r[1]} for r in rows]


def provider_requests_for(conn: psycopg.Connection, case_id: int) -> list[Any]:
    return conn.execute(
        """
        SELECT pr.id FROM provider_requests pr
          JOIN execution_attempts ea ON ea.id = pr.attempt_id
         WHERE ea.case_id = %s
        """,
        (case_id,),
    ).fetchall()


def audit_types(conn: psycopg.Connection, case_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT event_type FROM audit_events
         WHERE case_id = %s ORDER BY occurred_at, id
        """,
        (case_id,),
    ).fetchall()
    return [str(r[0]) for r in rows]


def policy_decisions_for(conn: psycopg.Connection, case_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, verdict, reason_code, selected_action, diagnosis_id
          FROM policy_decisions WHERE case_id = %s ORDER BY id
        """,
        (case_id,),
    ).fetchall()
    return [
        {
            "id": r[0],
            "verdict": r[1],
            "reason_code": r[2],
            "selected_action": r[3],
            "diagnosis_id": r[4],
        }
        for r in rows
    ]
