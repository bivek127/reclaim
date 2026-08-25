"""Central recovery-case state transition. No ad-hoc state UPDATEs elsewhere."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeAlias

import psycopg

from reclaim.domain.states import (
    AUDIT_EVENT_TYPE,
    CLOCK_STOPPED_STATES,
    TERMINAL_STATES,
    CaseState,
    as_case_state,
    is_allowed,
)

SideEffects: TypeAlias = Callable[[psycopg.Connection], None]


class TransitionIllegal(Exception):
    def __init__(self, expected_state: CaseState, new_state: CaseState) -> None:
        self.expected_state = expected_state
        self.new_state = new_state
        super().__init__(
            f"illegal transition {expected_state.value} -> {new_state.value}"
        )


def transition(
    conn: psycopg.Connection,
    case_id: int,
    expected_state: CaseState | str,
    new_state: CaseState | str,
    fencing_token: int,
    reason_code: str,
    side_effects: SideEffects | None = None,
) -> bool:
    expected = as_case_state(expected_state)
    new = as_case_state(new_state)
    if not is_allowed(expected, new):
        raise TransitionIllegal(expected, new)

    stop_clock = new in CLOCK_STOPPED_STATES
    restart_clock = expected is CaseState.HALTED and new not in CLOCK_STOPPED_STATES
    clear_worker = new in TERMINAL_STATES

    with conn.transaction():
        prior = conn.execute(
            """
            SELECT worker_id, obligation_id
              FROM recovery_cases
             WHERE id = %s
               AND state = %s
               AND fencing_token = %s
             FOR UPDATE
            """,
            (case_id, expected.value, fencing_token),
        ).fetchone()
        if prior is None:
            return False

        prior_worker_id, obligation_id = prior

        updated = conn.execute(
            """
            UPDATE recovery_cases
               SET state = %s,
                   active_elapsed_ms = CASE
                       WHEN %s AND active_since IS NOT NULL
                       THEN active_elapsed_ms
                            + (EXTRACT(EPOCH FROM (now() - active_since)) * 1000)::bigint
                       ELSE active_elapsed_ms
                   END,
                   active_since = CASE
                       WHEN %s THEN NULL
                       WHEN %s THEN now()
                       ELSE active_since
                   END,
                   worker_id = CASE WHEN %s THEN NULL ELSE worker_id END,
                   updated_at = now()
             WHERE id = %s
               AND state = %s
               AND fencing_token = %s
            RETURNING id
            """,
            (
                new.value,
                stop_clock,
                stop_clock,
                restart_clock,
                clear_worker,
                case_id,
                expected.value,
                fencing_token,
            ),
        ).fetchone()
        if updated is None:
            return False

        if side_effects is not None:
            side_effects(conn)

        conn.execute(
            """
            INSERT INTO audit_events (
                event_type, obligation_id, case_id, worker_id,
                fencing_token, prev_state, new_state, reason_code
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                AUDIT_EVENT_TYPE,
                obligation_id,
                case_id,
                prior_worker_id,
                fencing_token,
                expected.value,
                new.value,
                reason_code,
            ),
        )

    return True
