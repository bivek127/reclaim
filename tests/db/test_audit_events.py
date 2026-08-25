"""Constraint tests for audit_events."""

from __future__ import annotations

import psycopg
import pytest


def test_audit_events_are_append_only_on_update(conn: psycopg.Connection) -> None:
    row = conn.execute(
        """
        INSERT INTO audit_events (event_type)
        VALUES ('test.event')
        RETURNING id
        """
    ).fetchone()
    assert row is not None
    with pytest.raises(psycopg.Error, match="append-only"):
        conn.execute(
            "UPDATE audit_events SET event_type = 'changed' WHERE id = %s",
            (row[0],),
        )


def test_audit_events_are_append_only_on_delete(conn: psycopg.Connection) -> None:
    row = conn.execute(
        """
        INSERT INTO audit_events (event_type)
        VALUES ('test.event')
        RETURNING id
        """
    ).fetchone()
    assert row is not None
    with pytest.raises(psycopg.Error, match="append-only"):
        conn.execute("DELETE FROM audit_events WHERE id = %s", (row[0],))
