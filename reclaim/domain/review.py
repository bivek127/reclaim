"""Human review — PENDING entry, evidence, approve/reject/expire.

Approval creates a PROPOSED action only. The Executor alone dispatches it.
I10: this module never creates attempts, provider_requests, LIVE rows,
EXECUTING transitions, or provider calls, and never increments attempt_count.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from reclaim.config import lease_seconds_for, load_policy_config
from reclaim.domain.leases import claim_case, fenced_transition, release_lease
from reclaim.domain.states import CaseState

# RETRY_CHARGE is unsupported. Policy evaluation remaps it before review ever
# sees the case; human approval must not reopen it as a PROPOSED action.
EXECUTABLE_REVIEW_ACTIONS = frozenset({"CREATE_PAYMENT_LINK"})

REASON_TTL_EXHAUSTED = "ttl_exhausted"


class ReviewBlocked(Exception):
    """Review cannot proceed as presented."""


@dataclass(frozen=True)
class ReviewResult:
    case_id: int
    applied: bool
    review_id: int | None = None
    action_id: int | None = None
    case_state: CaseState | None = None
    reason: str = ""


@dataclass(frozen=True)
class ReviewEvidence:
    case_id: int
    obligation_id: int
    anchor_kind: str
    anchor_key: str
    amount_minor: int
    currency: str
    failure_codes: tuple[str, ...]
    diagnosis: dict[str, Any] | None
    policy: dict[str, Any] | None
    attempts: tuple[dict[str, Any], ...]
    audit_timeline: tuple[dict[str, Any], ...]


def on_entered_escalated(
    conn: psycopg.Connection,
    case_id: int,
    *,
    reason_code: str,
    policy_decision_id: int | None = None,
) -> tuple[int, int]:
    """Escalation entry (ADR-015 decision C). Call from side_effects after
    a transition into ESCALATED.

    If ``policy_decision_id`` is provided, reuse it (the routing decision that
    caused the escalation). Otherwise insert an escalation-provenance
    ``policy_decisions`` row (a TTL or deadline expiry with no prior decision
    to attach to). Always inserts exactly one PENDING ``human_reviews`` row.
    """
    if policy_decision_id is None:
        policy_decision_id = _insert_escalation_provenance(
            conn, case_id, reason_code=reason_code
        )
    review_id = insert_pending_review(conn, case_id)
    return policy_decision_id, review_id


def insert_pending_review(
    conn: psycopg.Connection,
    case_id: int,
    *,
    review_ttl_ms: int | None = None,
) -> int:
    ttl_ms = review_ttl_ms if review_ttl_ms is not None else load_policy_config().review_ttl_ms
    expires = datetime.now(timezone.utc) + timedelta(milliseconds=ttl_ms)
    row = conn.execute(
        """
        INSERT INTO human_reviews (case_id, status, review_expires_at)
        VALUES (%s, 'PENDING', %s)
        RETURNING id
        """,
        (case_id, expires),
    ).fetchone()
    assert row is not None
    return int(row[0])


def load_review_evidence(conn: psycopg.Connection, case_id: int) -> ReviewEvidence:
    """Read-only projection of existing tables for a reviewer's screen."""
    obl = conn.execute(
        """
        SELECT o.id, o.anchor_kind, o.anchor_key, o.amount_minor, o.currency
          FROM recovery_cases c
          JOIN financial_obligations o ON o.id = c.obligation_id
         WHERE c.id = %s
        """,
        (case_id,),
    ).fetchone()
    if obl is None:
        raise ReviewBlocked(f"case {case_id} not found")

    codes = conn.execute(
        """
        SELECT COALESCE(pr.response_body #>> '{error,code}', pr.outcome::text)
          FROM provider_requests pr
          JOIN execution_attempts ea ON ea.id = pr.attempt_id
         WHERE ea.case_id = %s
         ORDER BY pr.id
        """,
        (case_id,),
    ).fetchall()
    failure_codes = tuple(str(r[0]) for r in codes if r[0])

    diag_row = conn.execute(
        """
        SELECT id, source, cause, reasoning, recommended_action, confidence
          FROM diagnoses WHERE case_id = %s ORDER BY id DESC LIMIT 1
        """,
        (case_id,),
    ).fetchone()
    diagnosis = None
    if diag_row is not None:
        diagnosis = {
            "id": diag_row[0],
            "source": diag_row[1],
            "cause": diag_row[2],
            "reasoning": diag_row[3],
            "recommended_action": diag_row[4],
            "confidence": float(diag_row[5]) if diag_row[5] is not None else None,
        }

    pol_row = conn.execute(
        """
        SELECT id, reason_code, ambiguity_signal, verdict, policy_version, selected_action
          FROM policy_decisions WHERE case_id = %s ORDER BY id DESC LIMIT 1
        """,
        (case_id,),
    ).fetchone()
    policy = None
    if pol_row is not None:
        policy = {
            "id": pol_row[0],
            "reason_code": pol_row[1],
            "ambiguity_signal": pol_row[2],
            "verdict": pol_row[3],
            "policy_version": pol_row[4],
            "selected_action": pol_row[5],
        }

    attempts = tuple(
        {
            "id": r[0],
            "state": r[1],
            "provider_reference": r[2],
            "amount_minor": r[3],
            "attempt_no": r[4],
        }
        for r in conn.execute(
            """
            SELECT id, state, provider_reference, amount_minor, attempt_no
              FROM execution_attempts WHERE case_id = %s ORDER BY id
            """,
            (case_id,),
        ).fetchall()
    )

    audit = tuple(
        {
            "id": r[0],
            "occurred_at": r[1],
            "event_type": r[2],
            "reason_code": r[3],
            "prev_state": r[4],
            "new_state": r[5],
            "worker_id": r[6],
            "fencing_token": r[7],
            "reviewer_ref": r[8],
            "detail": r[9],
        }
        for r in conn.execute(
            """
            SELECT id, occurred_at, event_type, reason_code, prev_state, new_state,
                   worker_id, fencing_token, reviewer_ref, detail
              FROM audit_events WHERE case_id = %s ORDER BY occurred_at, id
            """,
            (case_id,),
        ).fetchall()
    )

    return ReviewEvidence(
        case_id=case_id,
        obligation_id=int(obl[0]),
        anchor_kind=str(obl[1]),
        anchor_key=str(obl[2]),
        amount_minor=int(obl[3]),
        currency=str(obl[4]),
        failure_codes=failure_codes,
        diagnosis=diagnosis,
        policy=policy,
        attempts=attempts,
        audit_timeline=audit,
    )


def approve_review(
    conn: psycopg.Connection,
    case_id: int,
    *,
    selected_action: str,
    reviewer_ref: str,
    fencing_token: int,
    worker_id: str | None = None,
) -> ReviewResult:
    """Approve: APPROVED + PROPOSED only. Case stays ESCALATED (decision A)."""
    if selected_action not in EXECUTABLE_REVIEW_ACTIONS:
        raise ReviewBlocked(
            f"selected_action {selected_action!r} is not an executable review action"
        )
    if not reviewer_ref:
        raise ReviewBlocked("reviewer_ref is required")

    with conn.transaction():
        case = conn.execute(
            """
            SELECT state, attempt_count FROM recovery_cases
             WHERE id = %s AND state = %s AND fencing_token = %s
             FOR UPDATE
            """,
            (case_id, CaseState.ESCALATED.value, fencing_token),
        ).fetchone()
        if case is None:
            fenced_transition(
                conn,
                case_id,
                CaseState.ESCALATED,
                CaseState.EXPIRED_UNRESOLVED,
                fencing_token,
                "review_approve",
                worker_id=worker_id,
            )
            return ReviewResult(
                case_id=case_id,
                applied=False,
                reason="stale_or_wrong_state",
            )

        review = conn.execute(
            """
            SELECT id, review_expires_at FROM human_reviews
             WHERE case_id = %s AND status = 'PENDING'
             FOR UPDATE
            """,
            (case_id,),
        ).fetchone()
        if review is None:
            raise ReviewBlocked(f"case {case_id} has no PENDING review")

        review_id = int(review[0])

        decision_id = _escalation_policy_decision_id(conn, case_id)
        if decision_id is None:
            raise ReviewBlocked(
                f"case {case_id} has no escalation policy_decisions row"
            )

        updated = conn.execute(
            """
            UPDATE human_reviews
               SET status = 'APPROVED',
                   selected_action = %s,
                   reviewer_ref = %s,
                   decided_at = now()
             WHERE id = %s AND status = 'PENDING'
            RETURNING id
            """,
            (selected_action, reviewer_ref, review_id),
        ).fetchone()
        if updated is None:
            raise ReviewBlocked(f"review {review_id} is no longer PENDING")

        seq = conn.execute(
            "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM recovery_actions WHERE case_id = %s",
            (case_id,),
        ).fetchone()
        assert seq is not None
        action = conn.execute(
            """
            INSERT INTO recovery_actions (
                case_id, action_type, status, sequence_no, policy_decision_id
            ) VALUES (%s, %s, 'PROPOSED', %s, %s)
            RETURNING id
            """,
            (case_id, selected_action, int(seq[0]), decision_id),
        ).fetchone()
        assert action is not None
        action_id = int(action[0])

        _audit_review_decision(
            conn,
            case_id=case_id,
            review_id=review_id,
            status="APPROVED",
            selected_action=selected_action,
            reviewer_ref=reviewer_ref,
            action_id=action_id,
            policy_decision_id=decision_id,
            worker_id=worker_id,
            fencing_token=fencing_token,
        )
        release_lease(conn, case_id)

    return ReviewResult(
        case_id=case_id,
        applied=True,
        review_id=review_id,
        action_id=action_id,
        case_state=CaseState.ESCALATED,
        reason="review_approved",
    )


def reject_review(
    conn: psycopg.Connection,
    case_id: int,
    *,
    reviewer_ref: str,
    fencing_token: int,
    worker_id: str | None = None,
) -> ReviewResult:
    """Reject: REJECTED + ESCALATED → VERIFIED_FAILED. No action/attempt."""
    if not reviewer_ref:
        raise ReviewBlocked("reviewer_ref is required")

    with conn.transaction():
        case = conn.execute(
            """
            SELECT 1 FROM recovery_cases
             WHERE id = %s AND state = %s AND fencing_token = %s
             FOR UPDATE
            """,
            (case_id, CaseState.ESCALATED.value, fencing_token),
        ).fetchone()
        if case is None:
            fenced_transition(
                conn,
                case_id,
                CaseState.ESCALATED,
                CaseState.VERIFIED_FAILED,
                fencing_token,
                "review_reject",
                worker_id=worker_id,
            )
            return ReviewResult(
                case_id=case_id, applied=False, reason="stale_or_wrong_state"
            )

        review = conn.execute(
            """
            SELECT id FROM human_reviews
             WHERE case_id = %s AND status = 'PENDING'
             FOR UPDATE
            """,
            (case_id,),
        ).fetchone()
        if review is None:
            raise ReviewBlocked(f"case {case_id} has no PENDING review")
        review_id = int(review[0])

        conn.execute(
            """
            UPDATE human_reviews
               SET status = 'REJECTED',
                   reviewer_ref = %s,
                   decided_at = now()
             WHERE id = %s AND status = 'PENDING'
            """,
            (reviewer_ref, review_id),
        )

        def _audit(inner: psycopg.Connection) -> None:
            _audit_review_decision(
                inner,
                case_id=case_id,
                review_id=review_id,
                status="REJECTED",
                selected_action=None,
                reviewer_ref=reviewer_ref,
                action_id=None,
                policy_decision_id=None,
                worker_id=worker_id,
                fencing_token=fencing_token,
            )

        applied = fenced_transition(
            conn,
            case_id,
            CaseState.ESCALATED,
            CaseState.VERIFIED_FAILED,
            fencing_token,
            "review_rejected",
            worker_id=worker_id,
            side_effects=_audit,
        )
        if not applied:
            raise ReviewBlocked(
                f"review {review_id} rejected but case transition failed"
            )

    return ReviewResult(
        case_id=case_id,
        applied=True,
        review_id=review_id,
        case_state=CaseState.VERIFIED_FAILED,
        reason="review_rejected",
    )


def expire_reviews(
    conn: psycopg.Connection,
    *,
    limit: int = 100,
    worker_id: str = "review-expiry",
) -> int:
    """A PENDING review past review_expires_at moves to EXPIRED, and the case
    to EXPIRED_UNRESOLVED -- an unattended review is not silently dropped."""
    expired = 0
    with conn.transaction():
        rows = conn.execute(
            """
            SELECT hr.id, hr.case_id, c.fencing_token
              FROM human_reviews hr
              JOIN recovery_cases c ON c.id = hr.case_id
             WHERE hr.status = 'PENDING'
               AND hr.review_expires_at <= now()
               AND c.state = %s
             FOR UPDATE OF hr, c SKIP LOCKED
             LIMIT %s
            """,
            (CaseState.ESCALATED.value, limit),
        ).fetchall()

        for review_id, case_id, token in rows:
            bumped = conn.execute(
                """
                UPDATE recovery_cases
                   SET fencing_token = fencing_token + 1,
                       updated_at = now()
                 WHERE id = %s AND state = %s
                RETURNING fencing_token
                """,
                (case_id, CaseState.ESCALATED.value),
            ).fetchone()
            if bumped is None:
                continue
            new_token = int(bumped[0])

            marked = conn.execute(
                """
                UPDATE human_reviews
                   SET status = 'EXPIRED'
                 WHERE id = %s AND status = 'PENDING'
                RETURNING id
                """,
                (review_id,),
            ).fetchone()
            if marked is None:
                continue

            def _audit(inner: psycopg.Connection, rid=int(review_id)) -> None:
                _audit_review_decision(
                    inner,
                    case_id=int(case_id),
                    review_id=rid,
                    status="EXPIRED",
                    selected_action=None,
                    reviewer_ref=None,
                    action_id=None,
                    policy_decision_id=None,
                    worker_id=worker_id,
                    fencing_token=new_token,
                )
                release_lease(inner, int(case_id))

            applied = fenced_transition(
                conn,
                int(case_id),
                CaseState.ESCALATED,
                CaseState.EXPIRED_UNRESOLVED,
                new_token,
                "review_expired",
                worker_id=worker_id,
                side_effects=_audit,
            )
            if applied:
                expired += 1

    return expired


def approve_once(
    conn: psycopg.Connection,
    case_id: int,
    *,
    selected_action: str,
    reviewer_ref: str,
    worker_id: str = "review",
    lease_seconds: int | None = None,
) -> ReviewResult | None:
    """Claim an ESCALATED case and approve its PENDING review."""
    lease = lease_seconds or lease_seconds_for("review")
    claim = claim_case(conn, case_id, CaseState.ESCALATED, worker_id, lease)
    if claim is None:
        return None
    return approve_review(
        conn,
        case_id,
        selected_action=selected_action,
        reviewer_ref=reviewer_ref,
        fencing_token=claim.fencing_token,
        worker_id=worker_id,
    )


def _insert_escalation_provenance(
    conn: psycopg.Connection,
    case_id: int,
    *,
    reason_code: str,
) -> int:
    """A synthetic provenance row for escalations with no prior policy
    decision, so recovery_actions.policy_decision_id stays NOT NULL."""
    cfg = load_policy_config()
    diag = conn.execute(
        "SELECT id FROM diagnoses WHERE case_id = %s ORDER BY id DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    diagnosis_id = int(diag[0]) if diag is not None else None
    row = conn.execute(
        """
        INSERT INTO policy_decisions (
            case_id, diagnosis_id, policy_version, lookup_miss,
            conflicting_history, ambiguity_signal, verdict,
            selected_action, reason_code
        ) VALUES (%s, %s, %s, false, false, false, 'ESCALATE', NULL, %s)
        RETURNING id
        """,
        (case_id, diagnosis_id, cfg.policy_version, reason_code),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _escalation_policy_decision_id(
    conn: psycopg.Connection, case_id: int
) -> int | None:
    row = conn.execute(
        """
        SELECT id FROM policy_decisions
         WHERE case_id = %s AND verdict = 'ESCALATE'
         ORDER BY id DESC
         LIMIT 1
        """,
        (case_id,),
    ).fetchone()
    return int(row[0]) if row is not None else None


def _audit_review_decision(
    conn: psycopg.Connection,
    *,
    case_id: int,
    review_id: int,
    status: str,
    selected_action: str | None,
    reviewer_ref: str | None,
    action_id: int | None,
    policy_decision_id: int | None,
    worker_id: str | None,
    fencing_token: int,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_events (
            event_type, obligation_id, case_id, action_id, worker_id,
            fencing_token, reason_code, reviewer_ref, detail
        )
        SELECT 'review_decision', c.obligation_id, %s, %s, %s, %s, %s, %s, %s
          FROM recovery_cases c WHERE c.id = %s
        """,
        (
            case_id,
            action_id,
            worker_id,
            fencing_token,
            f"review_{status.lower()}",
            reviewer_ref,
            Jsonb(
                {
                    "review_id": review_id,
                    "status": status,
                    "selected_action": selected_action,
                    "policy_decision_id": policy_decision_id,
                    "action_id": action_id,
                }
            ),
            case_id,
        ),
    )
