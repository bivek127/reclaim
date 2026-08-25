"""Constraint tests for provider_requests."""

from __future__ import annotations

import json

import psycopg
import pytest
from psycopg.errors import CheckViolation, UniqueViolation

from tests.db.helpers import build_action_graph, insert_attempt


def _insert_request(
    conn: psycopg.Connection,
    attempt_id: int,
    *,
    request_no: int = 1,
    outcome: str = "IN_FLIGHT",
    completed_at: str | None = None,
) -> int:
    row = conn.execute(
        """
        INSERT INTO provider_requests (
            attempt_id, operation, request_no, idempotency_key,
            request_body, outcome, completed_at
        ) VALUES (%s, 'create_payment_link', %s, %s, %s::jsonb, %s, %s)
        RETURNING id
        """,
        (
            attempt_id,
            request_no,
            f"req-{request_no}",
            json.dumps({"amount": 100}),
            outcome,
            completed_at,
        ),
    ).fetchone()
    assert row is not None
    return row[0]


def test_duplicate_request_sequence_rejected(conn: psycopg.Connection) -> None:
    graph = build_action_graph(conn)
    attempt_id = insert_attempt(conn, graph["action_id"], graph["case_id"])
    _insert_request(conn, attempt_id, request_no=1)
    with pytest.raises(UniqueViolation):
        _insert_request(conn, attempt_id, request_no=1)


def test_completed_shape_requires_null_completed_at_for_inflight(conn: psycopg.Connection) -> None:
    graph = build_action_graph(conn)
    attempt_id = insert_attempt(conn, graph["action_id"], graph["case_id"])
    with pytest.raises(CheckViolation):
        conn.execute(
            """
            INSERT INTO provider_requests (
                attempt_id, operation, request_no, idempotency_key,
                request_body, outcome, completed_at
            ) VALUES (%s, 'op', 1, 'k', '{}'::jsonb, 'IN_FLIGHT', now())
            """,
            (attempt_id,),
        )


def test_completed_shape_requires_timestamp_for_terminal_outcome(conn: psycopg.Connection) -> None:
    graph = build_action_graph(conn)
    attempt_id = insert_attempt(conn, graph["action_id"], graph["case_id"])
    with pytest.raises(CheckViolation):
        conn.execute(
            """
            INSERT INTO provider_requests (
                attempt_id, operation, request_no, idempotency_key,
                request_body, outcome, completed_at
            ) VALUES (%s, 'op', 1, 'k', '{}'::jsonb, 'ACCEPTED', NULL)
            """,
            (attempt_id,),
        )
