"""Constraint tests for circuit_breaker."""

from __future__ import annotations

import psycopg
import pytest
from psycopg.errors import CheckViolation


def test_circuit_breaker_singleton_enforced(conn: psycopg.Connection) -> None:
    with pytest.raises(CheckViolation):
        conn.execute(
            """
            INSERT INTO circuit_breaker (id, state)
            VALUES (2, 'CLOSED')
            """
        )


def test_open_state_requires_opened_at(conn: psycopg.Connection) -> None:
    with pytest.raises(CheckViolation):
        conn.execute(
            """
            UPDATE circuit_breaker
               SET state = 'OPEN',
                   opened_at = NULL
             WHERE id = 1
            """
        )


def test_closed_state_forbids_opened_at(conn: psycopg.Connection) -> None:
    with pytest.raises(CheckViolation):
        conn.execute(
            """
            UPDATE circuit_breaker
               SET state = 'CLOSED',
                   opened_at = now()
             WHERE id = 1
            """
        )
