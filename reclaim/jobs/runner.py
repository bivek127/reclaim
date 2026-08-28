"""Polling loops that drive existing domain operations.

Orchestration only. A runner decides *when* an operation runs and guarantees
its lease is released afterwards. It never decides what a case's state should
become, never writes `recovery_cases` or `circuit_breaker`, and never
re-implements claiming or fencing -- every state change still happens inside
the domain function the runner calls.

Two shapes, because the domain already splits that way:

  * Batch operations select their own rows under `FOR UPDATE SKIP LOCKED` and
    open their own transaction. They need a timer and nothing else.
  * Per-case operations take `(conn, case_id, fencing_token=...)` and require
    the caller to hold a claim. The runner claims, passes the token through
    unchanged, and releases in a `finally`, so a raising domain call cannot
    strand a case under a worker that has gone away.

The loop is bounded by an injected stop condition rather than `while True`, and
sleeps through an injected clock, so the production code path is the one tests
exercise -- no thread, no timeout, no separate test-only loop.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, ContextManager, Protocol

import psycopg

from reclaim.domain.leases import claim_next, release_lease
from reclaim.domain.states import CaseState

log = logging.getLogger(__name__)

#: Opens a connection; `app_conn` and `verifier_conn` both satisfy this.
Connect = Callable[[], ContextManager[psycopg.Connection]]


class Clock(Protocol):
    """Injected so a test can run the real loop without real delay."""

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def forever() -> bool:
    """Production stop condition: the loop does not stop on its own."""
    return True


def at_most(count: int) -> Callable[[], bool]:
    """A stop condition that yields exactly `count` iterations."""
    remaining = count

    def should_continue() -> bool:
        nonlocal remaining
        if remaining <= 0:
            return False
        remaining -= 1
        return True

    return should_continue


@dataclass(frozen=True)
class Tick:
    """The outcome of one pass. Returned so callers can log or assert on it."""

    job: str
    worked: bool
    result: Any = None
    error: BaseException | None = None


def run_batch(
    *,
    name: str,
    connect: Connect,
    operation: Callable[..., Any],
    interval_seconds: int,
    limit: int,
    should_continue: Callable[[], bool] = forever,
    clock: Clock | None = None,
) -> list[Tick]:
    """Call a self-selecting batch operation on a fixed interval.

    The operation owns its own transaction and row selection. A failure is
    logged and the loop continues: a sweep that cannot run this tick is not a
    reason to stop sweeping.
    """
    ticks: list[Tick] = []
    tick_clock = clock or SystemClock()

    while should_continue():
        try:
            with connect() as conn:
                result = operation(conn, limit=limit)
            ticks.append(Tick(job=name, worked=True, result=result))
            log.info("job=%s result=%s", name, result)
        except Exception as exc:  # noqa: BLE001 - a failed tick must not end the loop
            ticks.append(Tick(job=name, worked=False, error=exc))
            log.exception("job=%s failed", name)
        tick_clock.sleep(interval_seconds)

    return ticks


def run_per_case(
    *,
    name: str,
    connect: Connect,
    operation: Callable[..., Any],
    expected_state: CaseState | str,
    worker_id: str,
    lease_seconds: int,
    interval_seconds: int,
    should_continue: Callable[[], bool] = forever,
    clock: Clock | None = None,
) -> list[Tick]:
    """Claim one case, run a domain operation on it, always release the lease.

    The fencing token from the claim is handed to the operation unchanged. The
    runner never inspects or re-checks it: a stale token is refused by the
    domain's own fenced write, which is where that decision belongs.

    An idle tick -- nothing claimable -- is not a failure; it sleeps and tries
    again. A raising operation still releases, so the case is immediately
    available to another worker rather than waiting out its lease.
    """
    ticks: list[Tick] = []
    tick_clock = clock or SystemClock()

    while should_continue():
        try:
            with connect() as conn:
                claim = claim_next(conn, expected_state, worker_id, lease_seconds)
                if claim is None:
                    ticks.append(Tick(job=name, worked=False))
                else:
                    try:
                        result = operation(
                            conn, claim.case_id, fencing_token=claim.fencing_token
                        )
                        ticks.append(Tick(job=name, worked=True, result=result))
                        log.info(
                            "job=%s case=%s result=%s", name, claim.case_id, result
                        )
                    except Exception as exc:  # noqa: BLE001 - see module docstring
                        ticks.append(Tick(job=name, worked=True, error=exc))
                        log.exception("job=%s case=%s failed", name, claim.case_id)
                    finally:
                        release_lease(conn, claim.case_id)
        except Exception as exc:  # noqa: BLE001 - a bad connection must not end the loop
            ticks.append(Tick(job=name, worked=False, error=exc))
            log.exception("job=%s could not claim", name)
        tick_clock.sleep(interval_seconds)

    return ticks
