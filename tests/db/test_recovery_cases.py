"""Constraint tests for recovery_cases."""

from __future__ import annotations

from datetime import datetime, timezone

import psycopg
import pytest
from psycopg.errors import CheckViolation, UniqueViolation

from tests.db.helpers import insert_case, insert_obligation


def test_duplicate_case_per_obligation_rejected(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    insert_case(conn, obligation_id)
    with pytest.raises(UniqueViolation):
        insert_case(conn, obligation_id)


def test_attempt_count_above_max_rejected(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    with pytest.raises(CheckViolation):
        insert_case(conn, obligation_id, max_attempts=2, attempt_count=3)


def test_negative_attempt_count_rejected(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    with pytest.raises(CheckViolation):
        insert_case(conn, obligation_id, attempt_count=-1)


def test_negative_fencing_token_rejected(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    with pytest.raises(CheckViolation):
        conn.execute(
            """
            INSERT INTO recovery_cases (
                obligation_id, state, ttl_budget_ms, fencing_token, active_since
            ) VALUES (%s, 'NEW', 1000, -1, now())
            """,
            (obligation_id,),
        )


def test_negative_recovered_amount_rejected(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    with pytest.raises(CheckViolation):
        insert_case(conn, obligation_id, recovered_amount_minor=-1)


def test_recovered_amount_only_when_verified(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    with pytest.raises(CheckViolation):
        conn.execute(
            """
            INSERT INTO recovery_cases (
                obligation_id, state, ttl_budget_ms, recovered_amount_minor, active_since
            ) VALUES (%s, 'NEW', 1000, 100, now())
            """,
            (obligation_id,),
        )


def test_ttl_clock_requires_active_since_for_non_terminal(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    with pytest.raises(CheckViolation):
        conn.execute(
            """
            INSERT INTO recovery_cases (obligation_id, state, ttl_budget_ms, active_since)
            VALUES (%s, 'NEW', 1000, NULL)
            """,
            (obligation_id,),
        )


def test_ttl_clock_forbids_active_since_when_halted(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    with pytest.raises(CheckViolation):
        insert_case(
            conn,
            obligation_id,
            state="HALTED",
            active_since=datetime.now(timezone.utc),
        )


def test_ttl_clock_forbids_active_since_when_terminal(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    with pytest.raises(CheckViolation):
        conn.execute(
            """
            INSERT INTO recovery_cases (
                obligation_id, state, ttl_budget_ms, active_since
            ) VALUES (%s, 'VERIFIED_FAILED', 1000, now())
            """,
            (obligation_id,),
        )


def test_terminal_states_cannot_hold_lease(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    with pytest.raises(CheckViolation):
        conn.execute(
            """
            INSERT INTO recovery_cases (
                obligation_id, state, ttl_budget_ms, worker_id, active_since
            ) VALUES (%s, 'VERIFIED_RECOVERED', 1000, 'worker-1', NULL)
            """,
            (obligation_id,),
        )
