"""Obligation + recovery-case creation. One case per obligation anchor."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from reclaim.config import load_policy
from reclaim.domain.anchors import Anchor, FinancialFacts


@dataclass(frozen=True)
class CaseCreationResult:
    obligation_id: int
    case_id: int | None
    created: bool


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
        return CaseCreationResult(
            obligation_id=obligation_id,
            case_id=existing[0],
            created=False,
        )

    return CaseCreationResult(
        obligation_id=obligation_id,
        case_id=case_row[0],
        created=True,
    )
