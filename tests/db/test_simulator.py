"""Constraint tests for simulator tables."""

from __future__ import annotations

import json

import psycopg
import pytest
from psycopg.errors import CheckViolation

from tests.db.helpers import insert_case, insert_obligation


def test_control_arm_cannot_have_action_type(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    case_id = insert_case(conn, obligation_id)
    run_id = conn.execute(
        """
        INSERT INTO sim_runs (seed, n_per_arm, params)
        VALUES (1, 10, %s::jsonb)
        RETURNING id
        """,
        (json.dumps({"baseline": 0.1}),),
    ).fetchone()[0]
    with pytest.raises(CheckViolation):
        conn.execute(
            """
            INSERT INTO sim_outcomes (
                run_id, arm, case_id, pre_decision_features,
                action_type, resolved, amount_minor
            ) VALUES (%s, 'CONTROL', %s, '{}'::jsonb, 'CREATE_PAYMENT_LINK', true, 100)
            """,
            (run_id, case_id),
        )
