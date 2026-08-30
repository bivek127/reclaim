"""Circuit-breaker read gate and failure counting.

Scope boundary, deliberately narrow: this module **reads** breaker state and
**counts** consecutive execution failures. It never writes `state`.

Opening and resetting the breaker belong to a separate monitor job, not the
Executor. The executor's own breaker interaction is limited to reading the
gate before dispatch and aborting to HALTED when it is OPEN. The executor
still owns the failure counter because it is the only component that
observes an execution outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from reclaim.domain.states import CaseState
from reclaim.domain.transitions import transition
from reclaim.provider.contract import UNKNOWN_OUTCOMES, ProviderOutcome

BREAKER_ID = 1

EVENT_BREAKER_OPENED = "breaker_opened"
EVENT_BREAKER_RESET = "breaker_reset"

REASON_BREAKER_RESUMED = "breaker_closed_resumed"

# Outcomes that count against breaker health. The breaker is an
# infrastructure-health signal, not a financial verdict: an outage produces
# unknown outcomes and that is exactly when dispatch should stop. Counting is
# entirely separate from case routing -- an unknown still goes to AMBIGUOUS.
FAILURE_OUTCOMES = (
    frozenset(
        {
            ProviderOutcome.REJECTED,
            ProviderOutcome.TRANSPORT_ERROR,
            # Not in UNKNOWN_OUTCOMES, but a misconfigured key still means the
            # executor cannot dispatch. Counted so a bad deploy trips the
            # monitor rather than silently burning every case's budget.
            ProviderOutcome.AUTH_ERROR,
        }
    )
    | UNKNOWN_OUTCOMES
)

# A dispatch that reached the provider and was accepted clears the counter.
SUCCESS_OUTCOMES = frozenset(
    {ProviderOutcome.ACCEPTED, ProviderOutcome.DUPLICATE_REFERENCE}
)


class BreakerOpen(Exception):
    """Raised inside TXN 1 so the whole dispatch transaction rolls back."""

    def __init__(self, case_id: int) -> None:
        self.case_id = case_id
        super().__init__(f"circuit breaker is OPEN; case {case_id} halted before dispatch")


@dataclass(frozen=True)
class BreakerState:
    state: str
    consecutive_failures: int
    #: When an open breaker becomes eligible to close. Written by
    #: `set_breaker_state` and exposed here so the monitor can enforce the
    #: reset without reaching into the table itself. NULL while CLOSED, and
    #: also while OPEN if no reset window was supplied.
    reset_after: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.state == "OPEN"


def read_breaker(conn: psycopg.Connection, *, for_update: bool = False) -> BreakerState:
    """Read the singleton. `for_update` serialises concurrent workers reading
    the gate before dispatch."""
    lock = " FOR UPDATE" if for_update else ""
    row = conn.execute(
        f"""
        SELECT state, consecutive_failures, reset_after
          FROM circuit_breaker
         WHERE id = %s{lock}
        """,
        (BREAKER_ID,),
    ).fetchone()
    assert row is not None, "circuit_breaker singleton row is missing"
    return BreakerState(
        state=str(row[0]),
        consecutive_failures=int(row[1]),
        reset_after=row[2],
    )


def record_execution_outcome(
    conn: psycopg.Connection, outcome: ProviderOutcome
) -> int:
    """Count or clear consecutive failures. Never writes `state`: opening and
    resetting the breaker belong to a separate monitor job.

    Returns the resulting counter value.
    """
    if outcome in SUCCESS_OUTCOMES:
        return _set_failures(conn, reset=True)
    if outcome in FAILURE_OUTCOMES:
        return _set_failures(conn, reset=False)
    # AUTH_ERROR and any future value land here only if the classification sets
    # above stop being exhaustive. Counting an unrecognised outcome as a failure
    # is the safe direction: it slows dispatch rather than hiding a problem.
    return _set_failures(conn, reset=False)


def _set_failures(conn: psycopg.Connection, *, reset: bool) -> int:
    row = conn.execute(
        """
        UPDATE circuit_breaker
           SET consecutive_failures = CASE WHEN %s THEN 0
                                           ELSE consecutive_failures + 1 END
         WHERE id = %s
        RETURNING consecutive_failures
        """,
        (reset, BREAKER_ID),
    ).fetchone()
    assert row is not None
    return int(row[0])


def set_breaker_state(
    conn: psycopg.Connection,
    *,
    open_breaker: bool,
    reason_code: str,
    trip_cause: dict[str, Any] | None = None,
    reset_seconds: int | None = None,
    worker_id: str | None = None,
) -> bool:
    """Change breaker state and audit it in the SAME transaction.

    The audit INSERT is not optional and not separable: state cannot move
    without evidence, because both statements are in one transaction and the
    caller cannot reach the UPDATE without going through this function.

    Breaker *state* changes belong to a monitor job that is not yet built; no
    other production code calls this today, so the executor counts failures
    and reads the gate but never opens it. This function exists so
    that whoever builds the monitor cannot open the breaker unaudited.

    Returns False when the requested state is already in effect (no-op, no
    event) -- a repeated open must not manufacture a second opening in history.
    """
    target = "OPEN" if open_breaker else "CLOSED"
    row = conn.execute(
        """
        UPDATE circuit_breaker
           SET state = %s,
               opened_at = CASE WHEN %s THEN now() ELSE NULL END,
               reset_after = CASE WHEN %s AND %s::int IS NOT NULL
                                  THEN now() + (%s::text || ' seconds')::interval
                                  ELSE NULL END,
               trip_cause = %s::jsonb,
               consecutive_failures = CASE WHEN %s THEN consecutive_failures
                                           ELSE 0 END
         WHERE id = %s
           AND state <> %s
        RETURNING state::text, consecutive_failures
        """,
        (
            target,
            open_breaker,
            open_breaker,
            reset_seconds,
            str(reset_seconds) if reset_seconds is not None else None,
            Jsonb(trip_cause) if trip_cause is not None else None,
            open_breaker,
            BREAKER_ID,
            target,
        ),
    ).fetchone()
    if row is None:
        return False

    conn.execute(
        """
        INSERT INTO audit_events (event_type, worker_id, reason_code, detail)
        VALUES (%s, %s, %s, %s)
        """,
        (
            EVENT_BREAKER_OPENED if open_breaker else EVENT_BREAKER_RESET,
            worker_id,
            reason_code,
            Jsonb(
                {
                    "state": row[0],
                    "consecutive_failures": int(row[1]),
                    "trip_cause": trip_cause,
                    "reset_seconds": reset_seconds,
                }
            ),
        ),
    )
    return True


def resume_halted_cases(conn: psycopg.Connection, *, limit: int) -> int:
    """HALTED -> ACTION_READY for up to `limit` cases, once the breaker is
    closed. The batch counterpart to the lease claim `HALTED` cannot use:
    `claim_next` refuses it outright (`UNCLAIMABLE_STATES`), because there is
    nothing case-specific to decide -- every currently HALTED case gets the
    same answer at the same instant, which is a batch decision, not a lease.

    Same shape as `sweeper.expire_ttl`: select and lock a bounded batch,
    bump each row's fencing token while still holding that lock (safe without
    re-checking the token, because `FOR UPDATE SKIP LOCKED` already means no
    other worker could have touched this row since the select), then hand the
    freshly observed token to `transition()`, which is the only thing that
    writes `state` and already implements the TTL-clock resume (§4.4) this
    needs -- entering `ACTION_READY` restarts `active_since` from `now()` and
    keeps the elapsed time the halt had already banked. Nothing here decides
    that; `transition()` does, unchanged.

    Performs no financial operation: it writes only `recovery_cases`, calls no
    provider, and creates no action. The breaker is not re-checked here on
    purpose -- resuming is not financially consequential, and the executor's
    own dispatch-time gate re-verifies the breaker fresh before anything is
    ever charged, so a breaker that reopens in the gap between this call and
    that check is caught there, not here.

    Caller's responsibility: only invoke this once the breaker is confirmed
    CLOSED. This function does not read the breaker itself, so it never
    second-guesses that decision.
    """
    resumed = 0
    with conn.transaction():
        rows = conn.execute(
            """
            SELECT id, fencing_token
              FROM recovery_cases
             WHERE state = 'HALTED'
             FOR UPDATE SKIP LOCKED
             LIMIT %s
            """,
            (limit,),
        ).fetchall()

        for case_id, token in rows:
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
                continue
            new_token = bumped[0]
            applied = transition(
                conn,
                case_id,
                CaseState.HALTED,
                CaseState.ACTION_READY,
                new_token,
                REASON_BREAKER_RESUMED,
            )
            if applied:
                resumed += 1

    return resumed
