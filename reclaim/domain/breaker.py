"""Circuit-breaker read gate and failure counting.

Scope boundary, deliberately narrow: this module **reads** breaker state and
**counts** consecutive execution failures. It never writes `state`.

Opening and resetting the breaker belong to a separate monitor job, not the
Executor. The executor's own breaker interaction is limited to reading the
gate before dispatch and aborting to HALTED when it is OPEN. The executor
still owns the failure counter because it is the only component that
observes an execution outcome. See ADR-011.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from reclaim.provider.contract import UNKNOWN_OUTCOMES, ProviderOutcome

BREAKER_ID = 1

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

    @property
    def is_open(self) -> bool:
        return self.state == "OPEN"


def read_breaker(conn: psycopg.Connection, *, for_update: bool = False) -> BreakerState:
    """Read the singleton. `for_update` serialises concurrent workers reading
    the gate before dispatch."""
    lock = " FOR UPDATE" if for_update else ""
    row = conn.execute(
        f"""
        SELECT state, consecutive_failures
          FROM circuit_breaker
         WHERE id = %s{lock}
        """,
        (BREAKER_ID,),
    ).fetchone()
    assert row is not None, "circuit_breaker singleton row is missing"
    return BreakerState(state=str(row[0]), consecutive_failures=int(row[1]))


def record_execution_outcome(
    conn: psycopg.Connection, outcome: ProviderOutcome
) -> int:
    """Count or clear consecutive failures. Never writes `state` (see ADR-011:
    opening and resetting the breaker belong to a separate monitor job).

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
