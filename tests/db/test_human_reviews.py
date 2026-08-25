"""Constraint tests for human_reviews."""

from __future__ import annotations

from datetime import datetime, timezone

import psycopg
import pytest
from psycopg.errors import CheckViolation, UniqueViolation

from tests.db.helpers import future_ts, insert_case, insert_obligation


def _insert_review(
    conn: psycopg.Connection,
    case_id: int,
    *,
    status: str = "PENDING",
    reviewer_ref: str | None = None,
    selected_action: str | None = None,
    decided_at: datetime | None = None,
) -> int:
    row = conn.execute(
        """
        INSERT INTO human_reviews (
            case_id, status, reviewer_ref, selected_action,
            review_expires_at, decided_at
        ) VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            case_id,
            status,
            reviewer_ref,
            selected_action,
            future_ts(60),
            decided_at,
        ),
    ).fetchone()
    assert row is not None
    return row[0]


def test_decided_review_requires_reviewer_and_timestamp(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    case_id = insert_case(conn, obligation_id)
    with pytest.raises(CheckViolation):
        _insert_review(conn, case_id, status="APPROVED", selected_action="CREATE_PAYMENT_LINK")


def test_pending_review_cannot_have_decision_fields(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    case_id = insert_case(conn, obligation_id)
    with pytest.raises(CheckViolation):
        _insert_review(
            conn,
            case_id,
            status="PENDING",
            reviewer_ref="alice",
            decided_at=datetime.now(timezone.utc),
        )


def test_approved_review_requires_selected_action(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    case_id = insert_case(conn, obligation_id)
    with pytest.raises(CheckViolation):
        _insert_review(
            conn,
            case_id,
            status="APPROVED",
            reviewer_ref="alice",
            decided_at=datetime.now(timezone.utc),
            selected_action=None,
        )


def test_only_one_pending_review_per_case(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    case_id = insert_case(conn, obligation_id)
    _insert_review(conn, case_id)
    with pytest.raises(UniqueViolation):
        _insert_review(conn, case_id)
