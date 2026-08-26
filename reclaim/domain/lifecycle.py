"""Obligation + recovery-case creation. One case per obligation anchor."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

from reclaim.config import load_policy
from reclaim.domain.anchors import Anchor, FinancialFacts


EVENT_CASE_CREATED = "case_created"
EVENT_CASE_DEDUPLICATED = "case_deduplicated"


@dataclass(frozen=True)
class CaseCreationResult:
    obligation_id: int
    case_id: int | None
    created: bool


# Both events are written in the SAME transaction as the INSERT they describe --
# the caller wraps create_obligation_and_case, so the row cannot commit without
# its evidence.
def _audit_lifecycle(
    conn: psycopg.Connection,
    *,
    event_type: str,
    obligation_id: int,
    case_id: int,
    anchor: Anchor,
    facts: FinancialFacts,
    source_event_id: str,
    reason_code: str,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_events (
            event_type, obligation_id, case_id, reason_code, new_state, detail
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            event_type,
            obligation_id,
            case_id,
            reason_code,
            "NEW" if event_type == EVENT_CASE_CREATED else None,
            Jsonb(
                {
                    "anchor_kind": anchor.kind.value,
                    "anchor_key": anchor.key,
                    "anchor_canonical": anchor.canonical,
                    "amount_minor": facts.amount_minor,
                    "currency": facts.currency,
                    "customer_ref": facts.customer_ref,
                    "source_event_id": source_event_id,
                }
            ),
        ),
    )


def create_obligation_and_case(
    conn: psycopg.Connection,
    *,
    anchor: Anchor,
    facts: FinancialFacts,
    source_event_id: str,
    ttl_budget_ms: int | None = None,
    max_attempts: int | None = None,
) -> CaseCreationResult:
    policy = load_policy()
    ttl = ttl_budget_ms if ttl_budget_ms is not None else policy["ttl_budget_ms"]
    attempts = max_attempts if max_attempts is not None else policy["max_attempts"]

    obligation = conn.execute(
        """
        INSERT INTO financial_obligations (
            anchor_kind, anchor_key, anchor_canonical,
            amount_minor, currency, customer_ref, source_event_id,
            first_seen_at, last_seen_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now())
        ON CONFLICT (anchor_canonical)
        DO UPDATE SET last_seen_at = now()
        RETURNING id
        """,
        (
            anchor.kind.value,
            anchor.key,
            anchor.canonical,
            facts.amount_minor,
            facts.currency,
            facts.customer_ref,
            source_event_id,
        ),
    ).fetchone()
    assert obligation is not None
    obligation_id = obligation[0]

    case_row = conn.execute(
        """
        INSERT INTO recovery_cases (
            obligation_id, state, active_since, ttl_budget_ms, max_attempts
        ) VALUES (%s, 'NEW', now(), %s, %s)
        ON CONFLICT (obligation_id) DO NOTHING
        RETURNING id
        """,
        (obligation_id, ttl, attempts),
    ).fetchone()

    if case_row is None:
        existing = conn.execute(
            "SELECT id FROM recovery_cases WHERE obligation_id = %s",
            (obligation_id,),
        ).fetchone()
        assert existing is not None
        _audit_lifecycle(
            conn,
            event_type=EVENT_CASE_DEDUPLICATED,
            obligation_id=obligation_id,
            case_id=existing[0],
            anchor=anchor,
            facts=facts,
            source_event_id=source_event_id,
            reason_code="case_deduplicated",
        )
        return CaseCreationResult(
            obligation_id=obligation_id,
            case_id=existing[0],
            created=False,
        )

    _audit_lifecycle(
        conn,
        event_type=EVENT_CASE_CREATED,
        obligation_id=obligation_id,
        case_id=case_row[0],
        anchor=anchor,
        facts=facts,
        source_event_id=source_event_id,
        reason_code="case_created",
    )
    return CaseCreationResult(
        obligation_id=obligation_id,
        case_id=case_row[0],
        created=True,
    )
