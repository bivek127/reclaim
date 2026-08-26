"""Shared fixtures for diagnosis tests."""

from __future__ import annotations

import itertools
import json
from typing import Any

import psycopg

from tests.db.helpers import insert_case, insert_obligation

_SEQ = itertools.count(1)

VALID_PAYLOAD = {
    "cause": "INSUFFICIENT_FUNDS",
    "recommended_action": "CREATE_PAYMENT_LINK",
    "reasoning": "Customer likely needs a new payment attempt.",
    "confidence": 0.7,
}


def valid_json(**overrides: Any) -> str:
    data = dict(VALID_PAYLOAD)
    data.update(overrides)
    return json.dumps(data)


def seed_diagnosing(
    conn: psycopg.Connection,
    *,
    amount_minor: int = 10_000,
    attempt_count: int = 0,
    failure_code: str | None = None,
) -> dict[str, Any]:
    n = str(next(_SEQ))
    obligation_id = insert_obligation(
        conn,
        anchor_key=f"ord_dx{n}",
        anchor_canonical=f"order:ord_dx{n}",
        amount_minor=amount_minor,
        source_event_id=f"evt_dx{n}",
    )
    case_id = insert_case(
        conn,
        obligation_id,
        state="DIAGNOSING",
        attempt_count=attempt_count,
    )
    if failure_code is not None:
        # Minimal attempt + request so failure-code history is readable.
        policy_id = conn.execute(
            """
            INSERT INTO policy_decisions (
                case_id, policy_version, lookup_miss, conflicting_history,
                ambiguity_signal, verdict, selected_action, reason_code
            ) VALUES (%s, '1.0', false, false, false, 'ALLOW',
                      'CREATE_PAYMENT_LINK', 'seed')
            RETURNING id
            """,
            (case_id,),
        ).fetchone()
        assert policy_id is not None
        action_id = conn.execute(
            """
            INSERT INTO recovery_actions (
                case_id, action_type, status, sequence_no, policy_decision_id,
                resolved_at
            ) VALUES (%s, 'CREATE_PAYMENT_LINK', 'TERMINAL_FAILED', 1, %s, now())
            RETURNING id
            """,
            (case_id, policy_id[0]),
        ).fetchone()
        assert action_id is not None
        attempt_id = conn.execute(
            """
            INSERT INTO execution_attempts (
                action_id, case_id, attempt_no, idempotency_key,
                provider_reference, state, amount_minor, currency
            ) VALUES (%s, %s, 1, %s, %s, 'REJECTED', %s, 'INR')
            RETURNING id
            """,
            (
                action_id[0],
                case_id,
                f"rcv_dx{n}",
                f"rcv_dx{n}",
                amount_minor,
            ),
        ).fetchone()
        assert attempt_id is not None
        conn.execute(
            """
            INSERT INTO provider_requests (
                attempt_id, operation, request_no, idempotency_key,
                request_body, outcome, response_body, completed_at
            ) VALUES (%s, 'create_payment_link', 1, %s, '{}'::jsonb, 'REJECTED',
                      %s, now())
            """,
            (
                attempt_id[0],
                f"rcv_dx{n}",
                psycopg.types.json.Jsonb({"error": {"code": failure_code}}),
            ),
        )

    return {
        "obligation_id": obligation_id,
        "case_id": case_id,
        "amount_minor": amount_minor,
    }


def case_row(conn: psycopg.Connection, case_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT state, attempt_count, fencing_token FROM recovery_cases WHERE id = %s",
        (case_id,),
    ).fetchone()
    assert row is not None
    return {"state": row[0], "attempt_count": row[1], "fencing_token": row[2]}


def diagnoses_for(conn: psycopg.Connection, case_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, source, model, cause, recommended_action, reasoning,
               confidence, llm_retry_count, prompt_version
          FROM diagnoses WHERE case_id = %s ORDER BY id
        """,
        (case_id,),
    ).fetchall()
    return [
        {
            "id": r[0],
            "source": r[1],
            "model": r[2],
            "cause": r[3],
            "recommended_action": r[4],
            "reasoning": r[5],
            "confidence": r[6],
            "llm_retry_count": r[7],
            "prompt_version": r[8],
        }
        for r in rows
    ]


def obligation_amount(conn: psycopg.Connection, obligation_id: int) -> int:
    row = conn.execute(
        "SELECT amount_minor FROM financial_obligations WHERE id = %s",
        (obligation_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])
