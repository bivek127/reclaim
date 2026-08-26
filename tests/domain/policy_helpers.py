"""Shared fixtures for policy tests."""

from __future__ import annotations

import itertools
from typing import Any

import psycopg

from tests.db.helpers import insert_case, insert_diagnosis, insert_obligation

_SEQ = itertools.count(1)


def seed_policy_eval(
    conn: psycopg.Connection,
    *,
    cause: str = "INSUFFICIENT_FUNDS",
    attempt_count: int = 0,
    max_attempts: int = 2,
    recommended_action: str | None = "RETRY_CHARGE",
    confidence: float | None = 0.99,
) -> dict[str, Any]:
    """A case in POLICY_EVAL with one diagnosis row."""
    n = str(next(_SEQ))
    obligation_id = insert_obligation(
        conn,
        anchor_key=f"ord_pol{n}",
        anchor_canonical=f"order:ord_pol{n}",
        source_event_id=f"evt_pol{n}",
    )
    case_id = insert_case(
        conn,
        obligation_id,
        state="POLICY_EVAL",
        attempt_count=attempt_count,
        max_attempts=max_attempts,
    )
    diagnosis_id = insert_diagnosis(
        conn,
        case_id,
        cause=cause,
        recommended_action=recommended_action,
        confidence=confidence,
    )
    return {
        "obligation_id": obligation_id,
        "case_id": case_id,
        "diagnosis_id": diagnosis_id,
        "cause": cause,
    }


def case_row(conn: psycopg.Connection, case_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT state, fencing_token FROM recovery_cases WHERE id = %s",
        (case_id,),
    ).fetchone()
    assert row is not None
    return {"state": row[0], "fencing_token": row[1]}


def policy_decisions_for(conn: psycopg.Connection, case_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, verdict, selected_action, lookup_miss, conflicting_history,
               ambiguity_signal, policy_version, reason_code
          FROM policy_decisions
         WHERE case_id = %s
         ORDER BY id
        """,
        (case_id,),
    ).fetchall()
    return [
        {
            "id": r[0],
            "verdict": r[1],
            "selected_action": r[2],
            "lookup_miss": r[3],
            "conflicting_history": r[4],
            "ambiguity_signal": r[5],
            "policy_version": r[6],
            "reason_code": r[7],
        }
        for r in rows
    ]


def policy_audit_count(conn: psycopg.Connection, case_id: int) -> int:
    row = conn.execute(
        "SELECT count(*) FROM audit_events WHERE case_id = %s AND event_type = 'policy_decision'",
        (case_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])
