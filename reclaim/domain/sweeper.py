"""Sweeper and TTL expiry jobs. State changes go through transition()."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from reclaim.domain.leases import REMAINING_TTL_SQL, release_lease
from reclaim.domain.states import CaseState, as_case_state
from reclaim.domain.transitions import transition

SWEEPER_LIMIT = 100
SWEEPER_WORKER_ID = "sweeper"
TTL_WORKER_ID = "ttl-expiry"
DEADLINE_WORKER_ID = "deadline-expiry"

# Distinct from ttl_exhausted: the case still has TTL budget; it is the
# ACTION whose window closed.
REASON_ACTION_DEADLINE = "action_deadline_expired"

RELEASE_ON_EXPIRED_LEASE = frozenset(
    {
        CaseState.NEW,
        CaseState.ENRICHING,
        CaseState.DIAGNOSING,
        CaseState.POLICY_EVAL,
        CaseState.ACTION_READY,
        CaseState.ATTEMPT_FAILED,
        CaseState.AMBIGUOUS,
        CaseState.RECONCILING,
        CaseState.AWAITING_CUSTOMER,
        CaseState.ESCALATED,
    }
)

# TTL routing per the state-machine's terminal-on-timeout edges. No invented
# transitions -- every destination here is also a legal transition() edge.
TTL_EXPIRY_DESTINATION = {
    CaseState.ENRICHING: CaseState.ESCALATED,
    CaseState.DIAGNOSING: CaseState.ESCALATED,
    CaseState.POLICY_EVAL: CaseState.ESCALATED,
    CaseState.AMBIGUOUS: CaseState.EXPIRED_UNRESOLVED,
    CaseState.RECONCILING: CaseState.EXPIRED_UNRESOLVED,
    CaseState.HALTED: CaseState.EXPIRED_UNRESOLVED,
}


@dataclass(frozen=True)
class SweepResult:
    released: int
    executing_to_ambiguous: int


@dataclass(frozen=True)
class TtlExpiryResult:
    expired: int
    skipped: int


@dataclass(frozen=True)
class DeadlineExpiryResult:
    escalated: int
    skipped: int


def sweep_expired_leases(
    conn: psycopg.Connection,
    *,
    limit: int = SWEEPER_LIMIT,
    worker_id: str = SWEEPER_WORKER_ID,
) -> SweepResult:
    released = 0
    executing_to_ambiguous = 0
    with conn.transaction():
        rows = conn.execute(
            """
            SELECT id, state, fencing_token, obligation_id, worker_id
              FROM recovery_cases
             WHERE lease_expires_at < now()
               AND worker_id IS NOT NULL
               AND state NOT IN (
                   'VERIFIED_RECOVERED', 'VERIFIED_FAILED',
                   'EXPIRED_UNRESOLVED', 'HALTED'
               )
             FOR UPDATE SKIP LOCKED
             LIMIT %s
            """,
            (limit,),
        ).fetchall()

        for case_id, state_value, token, obligation_id, prior_worker in rows:
            state = as_case_state(state_value)
            bumped = conn.execute(
                """
                UPDATE recovery_cases
                   SET fencing_token = fencing_token + 1,
                       updated_at = now()
                 WHERE id = %s
                   AND lease_expires_at < now()
                   AND worker_id IS NOT NULL
                RETURNING fencing_token
                """,
                (case_id,),
            ).fetchone()
            if bumped is None:
                continue
            new_token = bumped[0]

            if state is CaseState.EXECUTING:
                applied = transition(
                    conn,
                    case_id,
                    CaseState.EXECUTING,
                    CaseState.AMBIGUOUS,
                    new_token,
                    "lease_expired",
                    side_effects=lambda tx, cid=case_id: release_lease(tx, cid),
                )
                if applied:
                    executing_to_ambiguous += 1
                continue

            if state in RELEASE_ON_EXPIRED_LEASE:
                release_lease(conn, case_id)
                conn.execute(
                    """
                    INSERT INTO audit_events (
                        event_type, obligation_id, case_id, worker_id,
                        fencing_token, prev_state, new_state, reason_code, detail
                    ) VALUES (
                        'lease_released', %s, %s, %s, %s, %s, %s, 'lease_expired',
                        jsonb_build_object('prior_worker_id', %s::text)
                    )
                    """,
                    (
                        obligation_id,
                        case_id,
                        worker_id,
                        new_token,
                        state.value,
                        state.value,
                        prior_worker,
                    ),
                )
                released += 1

    return SweepResult(
        released=released,
        executing_to_ambiguous=executing_to_ambiguous,
    )


def expire_ttl(
    conn: psycopg.Connection,
    *,
    limit: int = SWEEPER_LIMIT,
) -> TtlExpiryResult:
    expired = 0
    skipped = 0
    with conn.transaction():
        rows = conn.execute(
            f"""
            SELECT id, state, fencing_token
              FROM recovery_cases
             WHERE state NOT IN (
                   'VERIFIED_RECOVERED', 'VERIFIED_FAILED', 'EXPIRED_UNRESOLVED'
               )
               AND {REMAINING_TTL_SQL} <= 0
             FOR UPDATE SKIP LOCKED
             LIMIT %s
            """,
            (limit,),
        ).fetchall()

        for case_id, state_value, token in rows:
            state = as_case_state(state_value)
            destination = TTL_EXPIRY_DESTINATION.get(state)
            if destination is None:
                skipped += 1
                continue
            bumped = conn.execute(
                """
                UPDATE recovery_cases
                   SET fencing_token = fencing_token + 1,
                       updated_at = now()
                 WHERE id = %s
                RETURNING fencing_token
                """,
                (case_id,),
            ).fetchone()
            if bumped is None:
                skipped += 1
                continue
            new_token = bumped[0]
            def _ttl_side_effects(
                inner: psycopg.Connection,
                *,
                cid: int = case_id,
                dest: CaseState = destination,
            ) -> None:
                if dest is CaseState.ESCALATED:
                    from reclaim.domain.review import (
                        REASON_TTL_EXHAUSTED,
                        on_entered_escalated,
                    )

                    # Records why the case escalated and opens the single
                    # PENDING review a human will decide on.
                    on_entered_escalated(
                        inner,
                        cid,
                        reason_code=REASON_TTL_EXHAUSTED,
                        policy_decision_id=None,
                    )

            applied = transition(
                conn,
                case_id,
                state,
                destination,
                new_token,
                "ttl_exhausted",
                side_effects=_ttl_side_effects,
            )
            if applied:
                expired += 1
            else:
                skipped += 1

    return TtlExpiryResult(expired=expired, skipped=skipped)


def expire_action_deadlines(
    conn: psycopg.Connection,
    *,
    limit: int = SWEEPER_LIMIT,
) -> DeadlineExpiryResult:
    """A payment window that closed without payment moves the case to ESCALATED.

    Deliberately separate from `expire_ttl`. That function implements case-level
    TTL *budget* exhaustion; this one fires while the case may still have plenty
    of TTL left -- what expired is the ACTION's window, not the case's clock.
    Both run on the same periodic tick without entangling the TTL arithmetic.

    Triggered on `action_deadline_at`, not `provider_expires_at`. The former is
    set to `expire_by + 10min`, and `ck_deadline_after_provider` enforces the
    ordering, because our internal notion of "this action is dead" must never
    arrive before the provider's. Sweeping on the provider's own expiry would
    collapse that grace window and could escalate a customer who paid in the
    final seconds.

    What this deliberately does NOT do: it never marks the action
    TERMINAL_FAILED, never creates a second action, never claims budget, and
    never touches a provider. An expired link is not evidence that the payment
    failed -- only that we stopped waiting. A human decides.
    """
    escalated = 0
    skipped = 0
    with conn.transaction():
        rows = conn.execute(
            """
            SELECT c.id, c.state::text
              FROM recovery_cases c
              JOIN recovery_actions ra ON ra.case_id = c.id
             WHERE c.state = %s
               AND ra.status = 'LIVE'
               AND ra.action_deadline_at IS NOT NULL
               AND ra.action_deadline_at < now()
             FOR UPDATE OF c SKIP LOCKED
             LIMIT %s
            """,
            (CaseState.AWAITING_CUSTOMER.value, limit),
        ).fetchall()

        for case_id, state_value in rows:
            state = as_case_state(state_value)
            bumped = conn.execute(
                """
                UPDATE recovery_cases
                   SET fencing_token = fencing_token + 1,
                       updated_at = now()
                 WHERE id = %s
                   AND state = %s
                RETURNING fencing_token
                """,
                (case_id, state.value),
            ).fetchone()
            if bumped is None:
                skipped += 1
                continue
            new_token = bumped[0]

            def _deadline_side_effects(
                inner: psycopg.Connection, *, cid: int = case_id
            ) -> None:
                from reclaim.domain.review import on_entered_escalated

                # Escalation provenance + exactly one PENDING review, reusing
                # the same entry point the TTL-expiry path uses.
                on_entered_escalated(
                    inner,
                    cid,
                    reason_code=REASON_ACTION_DEADLINE,
                    policy_decision_id=None,
                )

            applied = transition(
                conn,
                case_id,
                state,
                CaseState.ESCALATED,
                new_token,
                REASON_ACTION_DEADLINE,
                side_effects=_deadline_side_effects,
            )
            if applied:
                escalated += 1
            else:
                skipped += 1

    return DeadlineExpiryResult(escalated=escalated, skipped=skipped)
