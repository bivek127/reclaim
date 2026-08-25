"""Lease claim, fenced write-back, and stale-write audit."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from reclaim.config import LEASE_SECONDS
from reclaim.domain.states import TERMINAL_STATES, CaseState, as_case_state
from reclaim.domain.transitions import SideEffects, transition

PROVIDER_HTTP_TIMEOUT_SECONDS = 30
assert LEASE_SECONDS["execution"] >= 2 * PROVIDER_HTTP_TIMEOUT_SECONDS

UNCLAIMABLE_STATES = TERMINAL_STATES | {CaseState.HALTED}

REMAINING_TTL_SQL = """
(
  ttl_budget_ms
  - active_elapsed_ms
  - CASE WHEN active_since IS NULL THEN 0
         ELSE (EXTRACT(EPOCH FROM (now() - active_since)) * 1000)::bigint
    END
)
"""


@dataclass(frozen=True)
class Claim:
    case_id: int
    fencing_token: int
    worker_id: str
    state: CaseState


def remaining_ttl_ms(conn: psycopg.Connection, case_id: int) -> int:
    row = conn.execute(
        f"SELECT {REMAINING_TTL_SQL} FROM recovery_cases WHERE id = %s",
        (case_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def claim_case(
    conn: psycopg.Connection,
    case_id: int,
    expected_state: CaseState | str,
    worker_id: str,
    lease_seconds: int,
) -> Claim | None:
    expected = as_case_state(expected_state)
    if expected in UNCLAIMABLE_STATES:
        return None

    with conn.transaction():
        row = conn.execute(
            """
            UPDATE recovery_cases
               SET worker_id = %s,
                   lease_expires_at = now() + (%s || ' seconds')::interval,
                   fencing_token = fencing_token + 1,
                   updated_at = now()
             WHERE id = %s
               AND state = %s
               AND lease_expires_at < now()
            RETURNING fencing_token, obligation_id
            """,
            (worker_id, str(lease_seconds), case_id, expected.value),
        ).fetchone()
        if row is None:
            return None
        token, obligation_id = row
        _audit(
            conn,
            event_type="lease_claimed",
            obligation_id=obligation_id,
            case_id=case_id,
            worker_id=worker_id,
            fencing_token=token,
            reason_code="lease_claimed",
            prev_state=expected.value,
            new_state=expected.value,
        )
        return Claim(
            case_id=case_id,
            fencing_token=token,
            worker_id=worker_id,
            state=expected,
        )


def claim_next(
    conn: psycopg.Connection,
    expected_state: CaseState | str,
    worker_id: str,
    lease_seconds: int,
) -> Claim | None:
    expected = as_case_state(expected_state)
    if expected in UNCLAIMABLE_STATES:
        return None

    with conn.transaction():
        selected = conn.execute(
            """
            SELECT id
              FROM recovery_cases
             WHERE state = %s
               AND lease_expires_at < now()
               AND state NOT IN (
                   'VERIFIED_RECOVERED', 'VERIFIED_FAILED',
                   'EXPIRED_UNRESOLVED', 'HALTED'
               )
             FOR UPDATE SKIP LOCKED
             LIMIT 1
            """,
            (expected.value,),
        ).fetchone()
        if selected is None:
            return None
        case_id = selected[0]
        row = conn.execute(
            """
            UPDATE recovery_cases
               SET worker_id = %s,
                   lease_expires_at = now() + (%s || ' seconds')::interval,
                   fencing_token = fencing_token + 1,
                   updated_at = now()
             WHERE id = %s
               AND state = %s
               AND lease_expires_at < now()
            RETURNING fencing_token, obligation_id
            """,
            (worker_id, str(lease_seconds), case_id, expected.value),
        ).fetchone()
        if row is None:
            return None
        token, obligation_id = row
        _audit(
            conn,
            event_type="lease_claimed",
            obligation_id=obligation_id,
            case_id=case_id,
            worker_id=worker_id,
            fencing_token=token,
            reason_code="lease_claimed",
            prev_state=expected.value,
            new_state=expected.value,
        )
        return Claim(
            case_id=case_id,
            fencing_token=token,
            worker_id=worker_id,
            state=expected,
        )


def release_lease(conn: psycopg.Connection, case_id: int) -> None:
    conn.execute(
        """
        UPDATE recovery_cases
           SET worker_id = NULL,
               lease_expires_at = '-infinity',
               updated_at = now()
         WHERE id = %s
        """,
        (case_id,),
    )


def fenced_transition(
    conn: psycopg.Connection,
    case_id: int,
    expected_state: CaseState | str,
    new_state: CaseState | str,
    fencing_token: int,
    reason_code: str,
    worker_id: str | None = None,
    side_effects: SideEffects | None = None,
) -> bool:
    """Write-back through transition(). Stale tokens return False and audit once."""
    applied = transition(
        conn,
        case_id,
        expected_state,
        new_state,
        fencing_token,
        reason_code,
        side_effects=side_effects,
    )
    if applied:
        return True
    _record_stale_write(
        conn,
        case_id=case_id,
        expected_state=as_case_state(expected_state),
        fencing_token=fencing_token,
        worker_id=worker_id,
        reason_code=reason_code,
    )
    return False


def _record_stale_write(
    conn: psycopg.Connection,
    *,
    case_id: int,
    expected_state: CaseState,
    fencing_token: int,
    worker_id: str | None,
    reason_code: str,
) -> None:
    row = conn.execute(
        "SELECT obligation_id, state, fencing_token FROM recovery_cases WHERE id = %s",
        (case_id,),
    ).fetchone()
    if row is None:
        return
    obligation_id, current_state, current_token = row
    conn.execute(
        """
        INSERT INTO audit_events (
            event_type, obligation_id, case_id, worker_id,
            fencing_token, prev_state, new_state, reason_code, detail
        ) VALUES (
            'stale_write_rejected', %s, %s, %s, %s, %s, %s, 'stale_write_rejected',
            jsonb_build_object(
                'observed_token', %s::bigint,
                'attempted_reason', %s::text,
                'current_token', %s::bigint
            )
        )
        """,
        (
            obligation_id,
            case_id,
            worker_id,
            fencing_token,
            expected_state.value,
            current_state,
            fencing_token,
            reason_code,
            current_token,
        ),
    )


def _audit(
    conn: psycopg.Connection,
    *,
    event_type: str,
    obligation_id: int,
    case_id: int,
    worker_id: str | None,
    fencing_token: int,
    reason_code: str,
    prev_state: str | None,
    new_state: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_events (
            event_type, obligation_id, case_id, worker_id,
            fencing_token, prev_state, new_state, reason_code
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            event_type,
            obligation_id,
            case_id,
            worker_id,
            fencing_token,
            prev_state,
            new_state,
            reason_code,
        ),
    )
