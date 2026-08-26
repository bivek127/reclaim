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

                    # Escalation provenance + PENDING review (ADR-015 decision C).
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
