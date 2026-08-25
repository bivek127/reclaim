"""Webhook ingest: record the event, then create obligation/case when required."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from reclaim.domain.anchors import EventResolution, resolve_event
from reclaim.domain.lifecycle import create_obligation_and_case


@dataclass(frozen=True)
class IngestResult:
    http_status: int
    resolution: str | None
    duplicate_event: bool
    case_created: bool
    case_id: int | None
    obligation_id: int | None
    webhook_event_id: int | None


def ingest_webhook(
    conn: psycopg.Connection,
    *,
    signature_valid: bool,
    raw_body: bytes | str,
    provider_event_id: str,
) -> IngestResult:
    if not signature_valid:
        return IngestResult(
            http_status=400,
            resolution=None,
            duplicate_event=False,
            case_created=False,
            case_id=None,
            obligation_id=None,
            webhook_event_id=None,
        )

    if isinstance(raw_body, str):
        raw_text = raw_body
    else:
        raw_text = raw_body.decode("utf-8")

    try:
        with conn.transaction():
            try:
                body: dict[str, Any] = json.loads(raw_text)
                if not isinstance(body, dict):
                    raise ValueError("webhook body must be an object")
            except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
                return _record_only(
                    conn,
                    provider_event_id=provider_event_id,
                    event_type="unknown",
                    resolution=EventResolution.MALFORMED.value,
                    payload={"raw": raw_text[:4000]},
                    anchor_canonical=None,
                )

            event_type = body.get("event")
            if not isinstance(event_type, str) or not event_type:
                return _record_only(
                    conn,
                    provider_event_id=provider_event_id,
                    event_type="unknown",
                    resolution=EventResolution.UNMAPPABLE.value,
                    payload=body,
                    anchor_canonical=None,
                )

            payload = body.get("payload")
            if not isinstance(payload, dict):
                payload = {}

            resolved = resolve_event(event_type, payload)
            recorded = _insert_webhook_event(
                conn,
                provider_event_id=provider_event_id,
                event_type=event_type,
                resolution=resolved.resolution.value,
                payload=body,
                anchor_canonical=resolved.anchor.canonical if resolved.anchor else None,
            )
            if recorded.duplicate_event:
                return recorded

            if not resolved.creates_case or resolved.anchor is None or resolved.facts is None:
                return recorded

            created = create_obligation_and_case(
                conn,
                anchor=resolved.anchor,
                facts=resolved.facts,
                source_event_id=provider_event_id,
            )
            if recorded.webhook_event_id is not None and created.case_id is not None:
                conn.execute(
                    """
                    UPDATE webhook_events
                       SET case_id = %s, processed_at = now()
                     WHERE id = %s
                    """,
                    (created.case_id, recorded.webhook_event_id),
                )

            return IngestResult(
                http_status=200,
                resolution=resolved.resolution.value,
                duplicate_event=False,
                case_created=created.created,
                case_id=created.case_id,
                obligation_id=created.obligation_id,
                webhook_event_id=recorded.webhook_event_id,
            )
    except UniqueViolation:
        return IngestResult(
            http_status=200,
            resolution=None,
            duplicate_event=True,
            case_created=False,
            case_id=None,
            obligation_id=None,
            webhook_event_id=None,
        )


def _record_only(
    conn: psycopg.Connection,
    *,
    provider_event_id: str,
    event_type: str,
    resolution: str,
    payload: dict[str, Any],
    anchor_canonical: str | None,
) -> IngestResult:
    return _insert_webhook_event(
        conn,
        provider_event_id=provider_event_id,
        event_type=event_type,
        resolution=resolution,
        payload=payload,
        anchor_canonical=anchor_canonical,
    )


def _insert_webhook_event(
    conn: psycopg.Connection,
    *,
    provider_event_id: str,
    event_type: str,
    resolution: str,
    payload: dict[str, Any],
    anchor_canonical: str | None,
) -> IngestResult:
    row = conn.execute(
        """
        INSERT INTO webhook_events (
            provider_event_id, event_type, signature_valid,
            resolution, anchor_canonical, payload
        ) VALUES (%s, %s, true, %s, %s, %s)
        RETURNING id
        """,
        (
            provider_event_id,
            event_type,
            resolution,
            anchor_canonical,
            Jsonb(payload),
        ),
    ).fetchone()
    assert row is not None
    return IngestResult(
        http_status=200,
        resolution=resolution,
        duplicate_event=False,
        case_created=False,
        case_id=None,
        obligation_id=None,
        webhook_event_id=row[0],
    )
