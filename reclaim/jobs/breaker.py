"""The circuit-breaker monitor: decides *when* the breaker changes state.

The breaker's state is owned by `set_breaker_state`, which writes it and its
audit row in one transaction. This module holds no SQL, no state machine, and
no timestamp arithmetic of its own -- it reads the breaker through the domain,
compares two values, and calls the one audited mutation path.

Fail closed, precisely: every uncertainty here resolves toward *leaving the
gate as it is*. A tick that cannot read the breaker changes nothing. An open
breaker with no reset deadline recorded stays open, because "no deadline" is
not evidence that a deadline has passed. Only an elapsed, explicitly recorded
deadline closes it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import psycopg

from reclaim.domain.breaker import read_breaker, resume_halted_cases, set_breaker_state

log = logging.getLogger(__name__)

MONITOR_WORKER_ID = "breaker-monitor"
REASON_OPENED = "breaker_threshold_reached"
REASON_RESET = "breaker_reset_window_elapsed"


def monitor_breaker(
    conn: psycopg.Connection,
    *,
    failure_threshold: int,
    reset_seconds: int,
    resume_limit: int | None = None,
    now: datetime | None = None,
) -> str:
    """One monitor tick. Returns what it decided, for logging and assertions.

    Opening keeps the failure count that justified it; closing clears it. Both
    are existing `set_breaker_state` behaviour and are not re-implemented here.

    A breaker that is already CLOSED, or that this tick just closed, also
    resumes up to `resume_limit` HALTED cases in the same tick -- the same
    component that establishes "closed" is the one positioned to act on it,
    rather than a second job re-deriving the same fact from the same row.
    """
    at = now or datetime.now(timezone.utc)
    breaker = read_breaker(conn)

    if breaker.is_open:
        if breaker.reset_after is None:
            # An open breaker with no recorded deadline must not be closed on a
            # guess: the monitor has no evidence the window has passed.
            log.info("job=%s open, no reset deadline recorded; leaving open",
                     MONITOR_WORKER_ID)
            return "held_open_no_deadline"
        if at < breaker.reset_after:
            return "held_open_before_deadline"

        changed = set_breaker_state(
            conn,
            open_breaker=False,
            reason_code=REASON_RESET,
            worker_id=MONITOR_WORKER_ID,
        )
        log.info("job=%s closed=%s", MONITOR_WORKER_ID, changed)
        if resume_limit is not None:
            resumed = resume_halted_cases(conn, limit=resume_limit)
            log.info("job=%s resumed=%s", MONITOR_WORKER_ID, resumed)
        return "closed" if changed else "already_closed"

    if breaker.consecutive_failures < failure_threshold:
        if resume_limit is not None:
            # Already CLOSED: nothing above this branch closed it just now,
            # so any HALTED case here is left over from before this tick.
            resumed = resume_halted_cases(conn, limit=resume_limit)
            log.info("job=%s resumed=%s", MONITOR_WORKER_ID, resumed)
        return "below_threshold"

    changed = set_breaker_state(
        conn,
        open_breaker=True,
        reason_code=REASON_OPENED,
        trip_cause={
            "consecutive_failures": breaker.consecutive_failures,
            "threshold": failure_threshold,
        },
        reset_seconds=reset_seconds,
        worker_id=MONITOR_WORKER_ID,
    )
    log.info("job=%s opened=%s failures=%s", MONITOR_WORKER_ID, changed,
             breaker.consecutive_failures)
    return "opened" if changed else "already_open"


def breaker_monitor_operation(
    failure_threshold: int, reset_seconds: int
) -> Any:
    """Bind the configured thresholds to the batch-runner call shape.

    The runner passes `limit` -- the same `sweeper_batch_size` every other
    batch job bounds its work by -- which now doubles as the bound on how many
    HALTED cases one tick may resume, since the singleton breaker row itself
    has no use for it.
    """

    def operation(conn: psycopg.Connection, *, limit: int | None = None) -> str:
        return monitor_breaker(
            conn,
            failure_threshold=failure_threshold,
            reset_seconds=reset_seconds,
            resume_limit=limit,
        )

    operation.__name__ = "breaker_monitor_operation"
    return operation
