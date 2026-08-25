"""Constraint tests for recovery_actions."""

from __future__ import annotations

import psycopg
import pytest
from psycopg.errors import CheckViolation, UniqueViolation

from tests.db.helpers import (
    build_action_graph,
    future_ts,
    insert_action,
    insert_policy_decision,
)


def test_duplicate_action_sequence_rejected(conn: psycopg.Connection) -> None:
    graph = build_action_graph(conn)
    with pytest.raises(UniqueViolation):
        insert_action(conn, graph["case_id"], graph["policy_id"], sequence_no=1)


def test_superseded_without_pointer_rejected(conn: psycopg.Connection) -> None:
    graph = build_action_graph(conn)
    with pytest.raises(CheckViolation):
        conn.execute(
            """
            UPDATE recovery_actions SET status = 'SUPERSEDED' WHERE id = %s
            """,
            (graph["action_id"],),
        )


def test_superseded_pointer_without_status_rejected(conn: psycopg.Connection) -> None:
    graph = build_action_graph(conn)
    second_policy = insert_policy_decision(conn, graph["case_id"], reason_code="second")
    with pytest.raises(CheckViolation):
        insert_action(
            conn,
            graph["case_id"],
            second_policy,
            sequence_no=2,
            superseded_by=graph["action_id"],
            status="PROPOSED",
        )


def test_terminal_without_resolved_at_rejected(conn: psycopg.Connection) -> None:
    graph = build_action_graph(conn)
    with pytest.raises(CheckViolation):
        conn.execute(
            "UPDATE recovery_actions SET status = 'TERMINAL_FAILED' WHERE id = %s",
            (graph["action_id"],),
        )


def test_resolved_at_without_terminal_status_rejected(conn: psycopg.Connection) -> None:
    graph = build_action_graph(conn)
    with pytest.raises(CheckViolation):
        conn.execute(
            "UPDATE recovery_actions SET resolved_at = now() WHERE id = %s",
            (graph["action_id"],),
        )


def test_deadline_before_provider_expiry_rejected(conn: psycopg.Connection) -> None:
    graph = build_action_graph(conn)
    provider_expires = future_ts(120)
    deadline = future_ts(30)
    with pytest.raises(CheckViolation):
        conn.execute(
            """
            UPDATE recovery_actions
               SET provider_expires_at = %s,
                   action_deadline_at = %s
             WHERE id = %s
            """,
            (provider_expires, deadline, graph["action_id"]),
        )


def test_only_one_open_action_per_case(conn: psycopg.Connection) -> None:
    graph = build_action_graph(conn)
    second_policy = insert_policy_decision(conn, graph["case_id"], reason_code="second")
    with pytest.raises(UniqueViolation):
        insert_action(conn, graph["case_id"], second_policy, sequence_no=2, status="LIVE")
