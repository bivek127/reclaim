"""Read-only projections for the operations console.

Every function here issues SELECT statements and nothing else. No transitions,
no inserts, no leases, no domain decisions. Business meaning stays in
`reclaim.domain`; this module only shapes existing rows for display.

Amounts are returned as integer minor units exactly as stored. Formatting is a
presentation concern and never happens here.

The per-case audit timeline is deliberately absent: it is served by
`reclaim.audit`, which reads `audit_events` and nothing else.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg

from reclaim.domain.states import CASE_STATES, CaseState

# Cases an operator is expected to act on rather than wait for.
ATTENTION_STATES: tuple[str, ...] = (
    CaseState.ESCALATED.value,
    CaseState.AMBIGUOUS.value,
    CaseState.HALTED.value,
)

IN_FLIGHT_STATES: tuple[str, ...] = (
    CaseState.EXECUTING.value,
    CaseState.RECONCILING.value,
    CaseState.AWAITING_CUSTOMER.value,
)

VALID_STATES: frozenset[str] = frozenset(s.value for s in CASE_STATES)

_SORTABLE = {
    "created_at": "c.created_at",
    "updated_at": "c.updated_at",
    "amount": "o.amount_minor",
}


@dataclass(frozen=True)
class CaseRow:
    case_id: int
    state: str
    amount_minor: int
    currency: str
    customer_ref: str
    anchor_kind: str
    anchor_key: str
    attempt_count: int
    max_attempts: int
    recovered_amount_minor: int
    created_at: datetime
    updated_at: datetime
    has_pending_review: bool
    action_deadline_at: datetime | None


@dataclass(frozen=True)
class CasePage:
    rows: tuple[CaseRow, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class Overview:
    state_counts: dict[str, int]
    attention_total: int
    in_flight_total: int
    recovered_count: int
    recovered_amount_minor: int
    pending_reviews: int
    oldest_pending_review_at: datetime | None
    breaker_state: str
    breaker_consecutive_failures: int
    recent_activity: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class CaseDetail:
    case: CaseRow
    obligation: dict[str, Any]
    diagnoses: tuple[dict[str, Any], ...]
    policy_decisions: tuple[dict[str, Any], ...]
    actions: tuple[dict[str, Any], ...]
    attempts: tuple[dict[str, Any], ...]
    provider_requests: tuple[dict[str, Any], ...]
    verifications: tuple[dict[str, Any], ...]
    reviews: tuple[dict[str, Any], ...]


def _rows(cur: Any) -> list[dict[str, Any]]:
    cols = [d.name for d in cur.description] if cur.description else []
    return [dict(zip(cols, r)) for r in cur.fetchall()]


_CASE_SELECT = """
    SELECT c.id                     AS case_id,
           c.state::text            AS state,
           o.amount_minor           AS amount_minor,
           o.currency               AS currency,
           o.customer_ref           AS customer_ref,
           o.anchor_kind::text      AS anchor_kind,
           o.anchor_key             AS anchor_key,
           c.attempt_count          AS attempt_count,
           c.max_attempts           AS max_attempts,
           c.recovered_amount_minor AS recovered_amount_minor,
           c.created_at             AS created_at,
           c.updated_at             AS updated_at,
           EXISTS (SELECT 1 FROM human_reviews hr
                    WHERE hr.case_id = c.id AND hr.status = 'PENDING')
                                    AS has_pending_review,
           (SELECT max(ra.action_deadline_at) FROM recovery_actions ra
             WHERE ra.case_id = c.id AND ra.status = 'LIVE')
                                    AS action_deadline_at
      FROM recovery_cases c
      JOIN financial_obligations o ON o.id = c.obligation_id
"""


def list_cases(
    conn: psycopg.Connection,
    *,
    states: tuple[str, ...] = (),
    query: str | None = None,
    needs_attention: bool = False,
    has_pending_review: bool = False,
    sort: str = "updated_at",
    direction: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> CasePage:
    """Filtered, sorted, paginated case queue."""
    where: list[str] = []
    params: list[Any] = []

    requested = tuple(s for s in states if s in VALID_STATES)
    if requested:
        where.append("c.state::text = ANY(%s)")
        params.append(list(requested))
    if needs_attention:
        where.append("c.state::text = ANY(%s)")
        params.append(list(ATTENTION_STATES))
    if has_pending_review:
        where.append(
            "EXISTS (SELECT 1 FROM human_reviews hr "
            "WHERE hr.case_id = c.id AND hr.status = 'PENDING')"
        )
    if query:
        where.append(
            "(o.customer_ref ILIKE %s OR o.anchor_key ILIKE %s "
            " OR o.anchor_canonical ILIKE %s "
            " OR EXISTS (SELECT 1 FROM execution_attempts ea "
            "             WHERE ea.case_id = c.id "
            "               AND (ea.provider_reference ILIKE %s "
            "                    OR ea.idempotency_key ILIKE %s))"
            f"{' OR c.id = %s' if query.isdigit() else ''})"
        )
        like = f"%{query}%"
        params.extend([like, like, like, like, like])
        if query.isdigit():
            params.append(int(query))

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    column = _SORTABLE.get(sort, _SORTABLE["updated_at"])
    order = "ASC" if direction.lower() == "asc" else "DESC"
    bounded = max(1, min(int(limit), 200))
    skip = max(0, int(offset))

    total_cur = conn.execute(
        "SELECT count(*) FROM recovery_cases c "
        "JOIN financial_obligations o ON o.id = c.obligation_id" + clause,
        params,
    )
    total_row = total_cur.fetchone()
    total = int(total_row[0]) if total_row else 0

    cur = conn.execute(
        f"{_CASE_SELECT}{clause} ORDER BY {column} {order}, c.id DESC "
        "LIMIT %s OFFSET %s",
        [*params, bounded, skip],
    )
    rows = tuple(CaseRow(**r) for r in _rows(cur))
    return CasePage(rows=rows, total=total, limit=bounded, offset=skip)


def get_case(conn: psycopg.Connection, case_id: int) -> CaseDetail | None:
    """Everything the investigation workspace shows, except the audit trail."""
    cur = conn.execute(f"{_CASE_SELECT} WHERE c.id = %s", (case_id,))
    head = _rows(cur)
    if not head:
        return None
    case = CaseRow(**head[0])

    obligation = _rows(conn.execute(
        """
        SELECT o.id, o.anchor_kind::text AS anchor_kind, o.anchor_key,
               o.anchor_canonical, o.amount_minor, o.currency, o.customer_ref,
               o.source_event_id, o.first_seen_at, o.last_seen_at
          FROM financial_obligations o
          JOIN recovery_cases c ON c.obligation_id = o.id
         WHERE c.id = %s
        """, (case_id,)))

    diagnoses = _rows(conn.execute(
        """
        SELECT id, source::text AS source, model, model_version, prompt_version,
               cause, recommended_action::text AS recommended_action, reasoning,
               confidence, llm_retry_count, created_at
          FROM diagnoses WHERE case_id = %s ORDER BY id
        """, (case_id,)))

    policy = _rows(conn.execute(
        """
        SELECT id, diagnosis_id, policy_version, lookup_miss, conflicting_history,
               ambiguity_signal, verdict::text AS verdict,
               selected_action::text AS selected_action, reason_code, created_at
          FROM policy_decisions WHERE case_id = %s ORDER BY id
        """, (case_id,)))

    actions = _rows(conn.execute(
        """
        SELECT id, action_type::text AS action_type, status::text AS status,
               sequence_no, policy_decision_id, superseded_by,
               provider_expires_at, action_deadline_at, created_at, resolved_at
          FROM recovery_actions WHERE case_id = %s ORDER BY sequence_no, id
        """, (case_id,)))

    attempts = _rows(conn.execute(
        """
        SELECT id, action_id, attempt_no, idempotency_key, provider_reference,
               state::text AS state, amount_minor, currency, created_at, settled_at
          FROM execution_attempts WHERE case_id = %s ORDER BY attempt_no, id
        """, (case_id,)))

    requests = _rows(conn.execute(
        """
        SELECT pr.id, pr.attempt_id, pr.operation, pr.request_no,
               pr.idempotency_key, pr.outcome::text AS outcome, pr.http_status,
               pr.provider_correlation_id, pr.sent_at, pr.completed_at,
               pr.response_body
          FROM provider_requests pr
          JOIN execution_attempts ea ON ea.id = pr.attempt_id
         WHERE ea.case_id = %s ORDER BY pr.id
        """, (case_id,)))

    verifications = _rows(conn.execute(
        """
        SELECT id, attempt_id, webhook_event_id, webhook_status, query_status,
               query_correlation_id, agrees, verified_amount_minor, created_at
          FROM verifications WHERE case_id = %s ORDER BY id
        """, (case_id,)))

    reviews = _rows(conn.execute(
        """
        SELECT id, status::text AS status, reviewer_ref,
               selected_action::text AS selected_action,
               review_expires_at, created_at, decided_at
          FROM human_reviews WHERE case_id = %s ORDER BY id
        """, (case_id,)))

    return CaseDetail(
        case=case,
        obligation=obligation[0] if obligation else {},
        diagnoses=tuple(diagnoses),
        policy_decisions=tuple(policy),
        actions=tuple(actions),
        attempts=tuple(attempts),
        provider_requests=tuple(requests),
        verifications=tuple(verifications),
        reviews=tuple(reviews),
    )


def overview(conn: psycopg.Connection, *, activity_limit: int = 12) -> Overview:
    """Operational awareness. Every figure traces to a stored row."""
    counts = {
        str(r[0]): int(r[1])
        for r in conn.execute(
            "SELECT state::text, count(*) FROM recovery_cases GROUP BY 1"
        ).fetchall()
    }
    for state in CASE_STATES:
        counts.setdefault(state.value, 0)

    recovered = conn.execute(
        """
        SELECT count(*), COALESCE(sum(recovered_amount_minor), 0)
          FROM recovery_cases WHERE state = 'VERIFIED_RECOVERED'
        """
    ).fetchone()

    reviews = conn.execute(
        """
        SELECT count(*), min(created_at)
          FROM human_reviews WHERE status = 'PENDING'
        """
    ).fetchone()

    breaker = conn.execute(
        "SELECT state::text, consecutive_failures FROM circuit_breaker WHERE id = 1"
    ).fetchone()

    activity = _rows(conn.execute(
        """
        SELECT id, occurred_at, event_type, case_id, reason_code,
               prev_state::text AS prev_state, new_state::text AS new_state,
               worker_id, reviewer_ref
          FROM audit_events
         ORDER BY occurred_at DESC, id DESC
         LIMIT %s
        """, (max(1, min(int(activity_limit), 100)),)))

    return Overview(
        state_counts=counts,
        attention_total=sum(counts.get(s, 0) for s in ATTENTION_STATES),
        in_flight_total=sum(counts.get(s, 0) for s in IN_FLIGHT_STATES),
        recovered_count=int(recovered[0]) if recovered else 0,
        recovered_amount_minor=int(recovered[1]) if recovered else 0,
        pending_reviews=int(reviews[0]) if reviews else 0,
        oldest_pending_review_at=reviews[1] if reviews else None,
        breaker_state=str(breaker[0]) if breaker else "UNKNOWN",
        breaker_consecutive_failures=int(breaker[1]) if breaker else 0,
        recent_activity=tuple(activity),
    )


def list_reviews(
    conn: psycopg.Connection,
    *,
    status: str = "PENDING",
    limit: int = 50,
    offset: int = 0,
) -> tuple[tuple[dict[str, Any], ...], int]:
    """Review queue with just enough case context to triage."""
    allowed = {"PENDING", "APPROVED", "REJECTED", "EXPIRED"}
    wanted = status.upper() if status.upper() in allowed else "PENDING"
    bounded = max(1, min(int(limit), 200))
    skip = max(0, int(offset))

    total_row = conn.execute(
        "SELECT count(*) FROM human_reviews WHERE status = %s", (wanted,)
    ).fetchone()

    rows = _rows(conn.execute(
        """
        SELECT hr.id AS review_id, hr.case_id, hr.status::text AS status,
               hr.reviewer_ref, hr.selected_action::text AS selected_action,
               hr.review_expires_at, hr.created_at, hr.decided_at,
               c.state::text AS case_state, o.amount_minor, o.currency,
               o.customer_ref, o.anchor_key
          FROM human_reviews hr
          JOIN recovery_cases c ON c.id = hr.case_id
          JOIN financial_obligations o ON o.id = c.obligation_id
         WHERE hr.status = %s
         ORDER BY hr.created_at ASC, hr.id ASC
         LIMIT %s OFFSET %s
        """, (wanted, bounded, skip)))
    return tuple(rows), int(total_row[0]) if total_row else 0


def system_status(conn: psycopg.Connection) -> dict[str, Any]:
    """Breaker plus lease/queue health, for the operational status view."""
    breaker = _rows(conn.execute(
        """
        SELECT state::text AS state, consecutive_failures, opened_at,
               reset_after, trip_cause
          FROM circuit_breaker WHERE id = 1
        """))

    leases = conn.execute(
        """
        SELECT count(*) FILTER (WHERE worker_id IS NOT NULL
                                  AND lease_expires_at > now()) AS held,
               count(*) FILTER (WHERE worker_id IS NOT NULL
                                  AND lease_expires_at <= now()) AS expired
          FROM recovery_cases
        """
    ).fetchone()

    open_actions = conn.execute(
        "SELECT count(*) FROM recovery_actions "
        "WHERE status IN ('PROPOSED', 'LIVE', 'UNRESOLVED')"
    ).fetchone()

    unresolved = conn.execute(
        "SELECT count(*) FROM execution_attempts "
        "WHERE state IN ('PREPARED', 'IN_FLIGHT', 'UNKNOWN')"
    ).fetchone()

    stale = conn.execute(
        "SELECT count(*) FROM audit_events WHERE reason_code = 'stale_write_rejected'"
    ).fetchone()

    return {
        "breaker": breaker[0] if breaker else None,
        "leases_held": int(leases[0]) if leases else 0,
        "leases_expired": int(leases[1]) if leases else 0,
        "open_actions": int(open_actions[0]) if open_actions else 0,
        "unresolved_attempts": int(unresolved[0]) if unresolved else 0,
        "stale_writes_rejected": int(stale[0]) if stale else 0,
    }
