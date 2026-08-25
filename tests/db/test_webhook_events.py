"""Constraint tests for webhook_events."""

from __future__ import annotations

import json

import psycopg
import pytest
from psycopg.errors import CheckViolation, UniqueViolation


def _insert_webhook(
    conn: psycopg.Connection,
    *,
    provider_event_id: str = "evt-1",
    resolution: str = "IGNORED",
    anchor_canonical: str | None = None,
) -> int:
    row = conn.execute(
        """
        INSERT INTO webhook_events (
            provider_event_id, event_type, signature_valid,
            resolution, anchor_canonical, payload
        ) VALUES (%s, 'payment.failed', true, %s, %s, %s::jsonb)
        RETURNING id
        """,
        (provider_event_id, resolution, anchor_canonical, json.dumps({})),
    ).fetchone()
    assert row is not None
    return row[0]


def test_duplicate_provider_event_rejected(conn: psycopg.Connection) -> None:
    _insert_webhook(conn, provider_event_id="dup")
    with pytest.raises(UniqueViolation):
        _insert_webhook(conn, provider_event_id="dup")


def test_resolved_requires_anchor(conn: psycopg.Connection) -> None:
    with pytest.raises(CheckViolation):
        _insert_webhook(conn, resolution="RESOLVED", anchor_canonical=None)
