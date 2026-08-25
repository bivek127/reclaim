"""Constraint tests for verifications."""

from __future__ import annotations

import psycopg
import pytest
from psycopg.errors import CheckViolation

from tests.db.helpers import build_action_graph, insert_attempt


def _insert_verification(
    conn: psycopg.Connection,
    case_id: int,
    attempt_id: int,
    *,
    agrees: bool,
    verified_amount_minor: int,
) -> int:
    row = conn.execute(
        """
        INSERT INTO verifications (
            case_id, attempt_id, agrees, verified_amount_minor
        ) VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (case_id, attempt_id, agrees, verified_amount_minor),
    ).fetchone()
    assert row is not None
    return row[0]


def test_disagree_requires_zero_amount(conn: psycopg.Connection) -> None:
    graph = build_action_graph(conn)
    attempt_id = insert_attempt(conn, graph["action_id"], graph["case_id"])
    with pytest.raises(CheckViolation):
        _insert_verification(
            conn,
            graph["case_id"],
            attempt_id,
            agrees=False,
            verified_amount_minor=100,
        )


def test_agree_requires_positive_amount(conn: psycopg.Connection) -> None:
    graph = build_action_graph(conn)
    attempt_id = insert_attempt(conn, graph["action_id"], graph["case_id"])
    with pytest.raises(CheckViolation):
        _insert_verification(
            conn,
            graph["case_id"],
            attempt_id,
            agrees=True,
            verified_amount_minor=0,
        )
