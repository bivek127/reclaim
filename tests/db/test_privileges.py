"""Privilege and recovered-revenue guard tests."""

from __future__ import annotations

import psycopg
import pytest

from tests.db.helpers import build_action_graph, insert_attempt


def test_app_role_cannot_write_recovered_amount_minor(
    conn: psycopg.Connection,
    app_conn: psycopg.Connection,
) -> None:
    graph = build_action_graph(conn)
    with pytest.raises(psycopg.Error, match="permission denied"):
        app_conn.execute(
            """
            UPDATE recovery_cases
               SET recovered_amount_minor = 10000
             WHERE id = %s
            """,
            (graph["case_id"],),
        )


def test_verifier_can_write_recovered_amount_with_matching_verification(
    conn: psycopg.Connection,
    verifier_conn: psycopg.Connection,
) -> None:
    graph = build_action_graph(conn)
    attempt_id = insert_attempt(conn, graph["action_id"], graph["case_id"])
    conn.execute(
        """
        INSERT INTO verifications (
            case_id, attempt_id, agrees, verified_amount_minor
        ) VALUES (%s, %s, true, 10000)
        """,
        (graph["case_id"], attempt_id),
    )
    conn.execute(
        """
        UPDATE recovery_cases
           SET state = 'VERIFIED_RECOVERED',
               recovered_amount_minor = 10000,
               active_since = NULL
         WHERE id = %s
        """,
        (graph["case_id"],),
    )

    verifier_conn.execute(
        """
        UPDATE recovery_cases
           SET recovered_amount_minor = 10000,
               updated_at = now()
         WHERE id = %s
        """,
        (graph["case_id"],),
    )


def test_recovered_amount_requires_matching_verification(
    conn: psycopg.Connection,
) -> None:
    graph = build_action_graph(conn)
    with pytest.raises(psycopg.Error, match="requires a matching agreeing verification"):
        conn.execute(
            """
            UPDATE recovery_cases
               SET state = 'VERIFIED_RECOVERED',
                   recovered_amount_minor = 10000,
                   active_since = NULL
             WHERE id = %s
            """,
            (graph["case_id"],),
        )


def test_recovered_amount_requires_agreeing_verification_amount(
    conn: psycopg.Connection,
) -> None:
    graph = build_action_graph(conn)
    attempt_id = insert_attempt(conn, graph["action_id"], graph["case_id"])
    conn.execute(
        """
        INSERT INTO verifications (
            case_id, attempt_id, agrees, verified_amount_minor
        ) VALUES (%s, %s, true, 10000)
        """,
        (graph["case_id"], attempt_id),
    )
    with pytest.raises(psycopg.Error, match="requires a matching agreeing verification"):
        conn.execute(
            """
            UPDATE recovery_cases
               SET state = 'VERIFIED_RECOVERED',
                   recovered_amount_minor = 20000,
                   active_since = NULL
             WHERE id = %s
            """,
            (graph["case_id"],),
        )
