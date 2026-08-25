"""Constraint tests for execution_attempts."""

from __future__ import annotations

import psycopg
import pytest
from psycopg.errors import CheckViolation, UniqueViolation

from tests.db.helpers import build_action_graph, insert_attempt


def test_duplicate_idempotency_key_rejected(conn: psycopg.Connection) -> None:
    graph = build_action_graph(conn)
    insert_attempt(conn, graph["action_id"], graph["case_id"], idempotency_key="dup-key")
    with pytest.raises(UniqueViolation):
        insert_attempt(
            conn,
            graph["action_id"],
            graph["case_id"],
            attempt_no=2,
            idempotency_key="dup-key",
        )


def test_duplicate_provider_reference_rejected(conn: psycopg.Connection) -> None:
    graph = build_action_graph(conn)
    insert_attempt(
        conn,
        graph["action_id"],
        graph["case_id"],
        idempotency_key="key-1",
        provider_reference="ref-1",
    )
    with pytest.raises(UniqueViolation):
        insert_attempt(
            conn,
            graph["action_id"],
            graph["case_id"],
            attempt_no=2,
            idempotency_key="key-2",
            provider_reference="ref-1",
        )


def test_duplicate_attempt_sequence_rejected(conn: psycopg.Connection) -> None:
    graph = build_action_graph(conn)
    insert_attempt(conn, graph["action_id"], graph["case_id"], attempt_no=1)
    with pytest.raises(UniqueViolation):
        insert_attempt(
            conn,
            graph["action_id"],
            graph["case_id"],
            attempt_no=1,
            idempotency_key="key-2",
        )


def test_non_positive_attempt_amount_rejected(conn: psycopg.Connection) -> None:
    graph = build_action_graph(conn)
    # guard_attempt_amount runs before ck_attempt_amount; both reject non-positive amounts.
    with pytest.raises(psycopg.Error):
        insert_attempt(
            conn,
            graph["action_id"],
            graph["case_id"],
            amount_minor=0,
        )


def test_attempt_amount_must_match_obligation(conn: psycopg.Connection) -> None:
    graph = build_action_graph(conn)
    with pytest.raises(psycopg.Error, match="must match the obligation"):
        insert_attempt(
            conn,
            graph["action_id"],
            graph["case_id"],
            amount_minor=999,
        )


def test_attempt_currency_must_match_obligation(conn: psycopg.Connection) -> None:
    graph = build_action_graph(conn)
    with pytest.raises(psycopg.Error, match="must match the obligation"):
        insert_attempt(
            conn,
            graph["action_id"],
            graph["case_id"],
            currency="USD",
        )


def test_only_one_open_attempt_per_action(conn: psycopg.Connection) -> None:
    graph = build_action_graph(conn)
    insert_attempt(
        conn,
        graph["action_id"],
        graph["case_id"],
        attempt_no=1,
        idempotency_key="open-1",
        state="IN_FLIGHT",
    )
    with pytest.raises(UniqueViolation):
        insert_attempt(
            conn,
            graph["action_id"],
            graph["case_id"],
            attempt_no=2,
            idempotency_key="open-2",
            state="UNKNOWN",
        )
