"""Constraint tests for the request_outcome enum (migration 019)."""

from __future__ import annotations

import psycopg
import pytest
from psycopg.errors import InvalidTextRepresentation

from tests.db.helpers import build_action_graph, insert_attempt

ORIGINAL = {"IN_FLIGHT", "ACCEPTED", "REJECTED", "TIMEOUT", "TRANSPORT_ERROR",
            "DUPLICATE_REFERENCE"}
EXECUTION_ADDED = {"PROVIDER_ERROR", "RATE_LIMITED", "UNPARSEABLE", "AUTH_ERROR",
                   "UNKNOWN"}
READ_ADDED = {"FOUND", "NOT_FOUND", "NO_EVIDENCE"}


def _labels(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT enumlabel FROM pg_enum e
          JOIN pg_type t ON t.oid = e.enumtypid
         WHERE t.typname = 'request_outcome'
        """
    ).fetchall()
    return {r[0] for r in rows}


def test_original_values_survive(conn: psycopg.Connection) -> None:
    assert ORIGINAL <= _labels(conn)


def test_execution_values_present(conn: psycopg.Connection) -> None:
    assert EXECUTION_ADDED <= _labels(conn)


def test_read_values_present(conn: psycopg.Connection) -> None:
    assert READ_ADDED <= _labels(conn)


def test_no_speculative_values(conn: psycopg.Connection) -> None:
    """Only what execution and reconciliation actually produce."""
    assert _labels(conn) == ORIGINAL | EXECUTION_ADDED | READ_ADDED


def _insert_request(conn: psycopg.Connection, attempt_id: int, outcome: str) -> None:
    conn.execute(
        """
        INSERT INTO provider_requests (
            attempt_id, operation, request_no, idempotency_key,
            request_body, outcome, completed_at
        ) VALUES (%s, 'op', 1, 'k', '{}'::jsonb, %s,
                  CASE WHEN %s = 'IN_FLIGHT' THEN NULL ELSE now() END)
        """,
        (attempt_id, outcome, outcome),
    )


@pytest.mark.parametrize("outcome", sorted(EXECUTION_ADDED | READ_ADDED))
def test_new_values_are_writable(conn: psycopg.Connection, outcome: str) -> None:
    graph = build_action_graph(conn)
    attempt_id = insert_attempt(
        conn, graph["action_id"], graph["case_id"], idempotency_key=f"k-{outcome}"
    )

    _insert_request(conn, attempt_id, outcome)

    row = conn.execute(
        "SELECT outcome FROM provider_requests WHERE attempt_id = %s", (attempt_id,)
    ).fetchone()
    assert row is not None and row[0] == outcome


def test_unknown_label_still_rejected(conn: psycopg.Connection) -> None:
    """The enum is still closed; migration 019 widened it, not opened it."""
    graph = build_action_graph(conn)
    attempt_id = insert_attempt(conn, graph["action_id"], graph["case_id"])

    with pytest.raises(InvalidTextRepresentation):
        _insert_request(conn, attempt_id, "NOT_A_REAL_OUTCOME")


def test_completed_shape_still_holds_for_new_values(conn: psycopg.Connection) -> None:
    """ck_completed_shape is unaffected by the widened enum."""
    from psycopg.errors import CheckViolation

    graph = build_action_graph(conn)
    attempt_id = insert_attempt(conn, graph["action_id"], graph["case_id"])

    with pytest.raises(CheckViolation):
        conn.execute(
            """
            INSERT INTO provider_requests (
                attempt_id, operation, request_no, idempotency_key,
                request_body, outcome, completed_at
            ) VALUES (%s, 'op', 1, 'k', '{}'::jsonb, 'NO_EVIDENCE', NULL)
            """,
            (attempt_id,),
        )
