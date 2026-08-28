"""The breaker monitor: when the gate opens, when it closes, and when it must not.

Breaker *mechanics* -- what `set_breaker_state` writes, what it audits, how the
counter moves -- are the domain suite's subject. These tests cover the decision:
given a breaker state and a clock, does the monitor call the domain, and with
what.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from reclaim.config import load_operational, load_policy
from reclaim.domain.breaker import read_breaker, record_execution_outcome, set_breaker_state
from reclaim.jobs.breaker import MONITOR_WORKER_ID, monitor_breaker
from reclaim.jobs.jobs import BREAKER_MONITOR, register_batch_jobs
from reclaim.jobs.registry import JobKind, JobRegistry
from reclaim.jobs.runner import at_most, run_batch
from reclaim.provider.contract import ProviderOutcome

THRESHOLD = int(load_policy()["breaker_failure_threshold"])
RESET_SECONDS = int(load_policy()["breaker_reset_seconds"])


class FakeClock:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def connect_to(conn: psycopg.Connection):
    @contextmanager
    def factory():
        yield conn

    return factory


def tick(conn: psycopg.Connection, *, now: datetime | None = None) -> str:
    return monitor_breaker(
        conn,
        failure_threshold=THRESHOLD,
        reset_seconds=RESET_SECONDS,
        now=now,
    )


def fail(conn: psycopg.Connection, times: int) -> None:
    for _ in range(times):
        record_execution_outcome(conn, ProviderOutcome.TRANSPORT_ERROR)


def succeed(conn: psycopg.Connection) -> None:
    record_execution_outcome(conn, ProviderOutcome.ACCEPTED)


def breaker_events(conn: psycopg.Connection) -> list[tuple[str, str]]:
    return [
        (r[0], r[1])
        for r in conn.execute(
            "SELECT event_type, reason_code FROM audit_events "
            "WHERE event_type IN ('breaker_opened','breaker_reset') ORDER BY id"
        ).fetchall()
    ]


# ---- registration and configuration ---------------------------------------


def test_the_monitor_is_registered_as_a_batch_job() -> None:
    """A singleton row is not a queue of cases: no state to claim, no lease."""
    spec = register_batch_jobs(JobRegistry()).get(BREAKER_MONITOR)
    assert spec.kind is JobKind.BATCH
    assert spec.expected_states is None and spec.lease_seconds is None


def test_the_interval_comes_from_operational_configuration() -> None:
    spec = register_batch_jobs(JobRegistry()).get(BREAKER_MONITOR)
    assert spec.interval_seconds == int(
        load_operational()["breaker_monitor_interval_seconds"]
    )
    assert spec.interval_seconds == 10


def test_editing_the_configured_interval_changes_the_registration() -> None:
    values = load_operational()
    values["breaker_monitor_interval_seconds"] = 7
    spec = register_batch_jobs(JobRegistry(), config=values).get(BREAKER_MONITOR)
    assert spec.interval_seconds == 7


def test_the_configured_threshold_and_reset_window_match_the_job_contract() -> None:
    """Pinned, not merely read.

    The other tests derive their expectations from configuration, so they stay
    self-consistent if a value changes. These two numbers are the stated
    contract -- open on five consecutive failures, reset after 120 seconds --
    and a silent edit to either must fail here.
    """
    policy = load_policy()
    assert policy["breaker_failure_threshold"] == 5
    assert policy["breaker_reset_seconds"] == 120


def test_the_monitor_opens_on_the_fifth_failure_and_not_the_fourth(
    conn: psycopg.Connection,
) -> None:
    """The contract value, asserted literally rather than through the config."""
    fail(conn, 4)
    assert monitor_breaker(
        conn, failure_threshold=5, reset_seconds=120
    ) == "below_threshold"
    assert not read_breaker(conn).is_open

    fail(conn, 1)
    assert monitor_breaker(conn, failure_threshold=5, reset_seconds=120) == "opened"
    assert read_breaker(conn).is_open


def test_the_reset_deadline_is_two_minutes_after_opening(
    conn: psycopg.Connection,
) -> None:
    """120 seconds, asserted against the clock rather than against config."""
    fail(conn, 5)
    before = datetime.now(timezone.utc)
    monitor_breaker(conn, failure_threshold=5, reset_seconds=120)
    after = datetime.now(timezone.utc)

    deadline = read_breaker(conn).reset_after
    assert deadline is not None
    assert before + timedelta(seconds=120) <= deadline <= after + timedelta(seconds=120)


def test_the_threshold_and_reset_window_come_from_policy(
    conn: psycopg.Connection,
) -> None:
    """Both are business policy, so a test may vary them without touching code."""
    policy = dict(load_policy())
    policy["breaker_failure_threshold"] = 2
    registry = register_batch_jobs(JobRegistry(), policy=policy)
    operation = registry.get(BREAKER_MONITOR).operation

    fail(conn, 2)
    operation(conn, limit=100)

    assert read_breaker(conn).is_open, "the configured threshold was used"


# ---- the exposed value ----------------------------------------------------


def test_read_breaker_exposes_the_recorded_reset_deadline(
    conn: psycopg.Connection,
) -> None:
    assert read_breaker(conn).reset_after is None, "closed breaker has no deadline"

    set_breaker_state(
        conn, open_breaker=True, reason_code="test", reset_seconds=RESET_SECONDS
    )
    state = read_breaker(conn)

    assert state.is_open
    assert state.reset_after is not None
    assert state.reset_after > datetime.now(timezone.utc)


# ---- opening --------------------------------------------------------------


def test_reaching_the_threshold_opens_the_breaker(conn: psycopg.Connection) -> None:
    fail(conn, THRESHOLD)

    assert tick(conn) == "opened"
    assert read_breaker(conn).is_open
    assert breaker_events(conn) == [("breaker_opened", "breaker_threshold_reached")]


def test_one_short_of_the_threshold_does_not_open_it(
    conn: psycopg.Connection,
) -> None:
    fail(conn, THRESHOLD - 1)

    assert tick(conn) == "below_threshold"
    assert not read_breaker(conn).is_open
    assert breaker_events(conn) == []


def test_a_success_between_failures_breaks_the_run(conn: psycopg.Connection) -> None:
    """'Consecutive' is the counter's own semantics, not something the monitor
    re-derives: an accepted dispatch clears it."""
    fail(conn, THRESHOLD - 1)
    succeed(conn)
    fail(conn, THRESHOLD - 1)

    assert read_breaker(conn).consecutive_failures == THRESHOLD - 1
    assert tick(conn) == "below_threshold"
    assert not read_breaker(conn).is_open


def test_an_open_breaker_is_not_opened_again(conn: psycopg.Connection) -> None:
    """A repeated open would manufacture a second opening in the trail."""
    fail(conn, THRESHOLD)
    tick(conn)
    fail(conn, THRESHOLD)

    assert tick(conn) == "held_open_before_deadline"
    assert len(breaker_events(conn)) == 1, "still exactly one opening"


# ---- resetting ------------------------------------------------------------


def test_an_open_breaker_stays_open_before_its_deadline(
    conn: psycopg.Connection,
) -> None:
    fail(conn, THRESHOLD)
    tick(conn)
    deadline = read_breaker(conn).reset_after
    assert deadline is not None

    assert tick(conn, now=deadline - timedelta(seconds=1)) == "held_open_before_deadline"
    assert read_breaker(conn).is_open


def test_an_open_breaker_closes_once_its_deadline_has_elapsed(
    conn: psycopg.Connection,
) -> None:
    fail(conn, THRESHOLD)
    tick(conn)
    deadline = read_breaker(conn).reset_after
    assert deadline is not None

    assert tick(conn, now=deadline + timedelta(seconds=1)) == "closed"
    state = read_breaker(conn)
    assert not state.is_open
    assert state.reset_after is None, "the domain cleared the deadline"


def test_closing_clears_the_failure_counter_through_the_domain(
    conn: psycopg.Connection,
) -> None:
    """The monitor does not zero the counter; `set_breaker_state` does."""
    fail(conn, THRESHOLD)
    tick(conn)
    assert read_breaker(conn).consecutive_failures == THRESHOLD

    deadline = read_breaker(conn).reset_after
    tick(conn, now=deadline + timedelta(seconds=1))

    assert read_breaker(conn).consecutive_failures == 0


def test_closing_emits_the_audited_reset_event(conn: psycopg.Connection) -> None:
    fail(conn, THRESHOLD)
    tick(conn)
    deadline = read_breaker(conn).reset_after
    tick(conn, now=deadline + timedelta(seconds=1))

    assert breaker_events(conn) == [
        ("breaker_opened", "breaker_threshold_reached"),
        ("breaker_reset", "breaker_reset_window_elapsed"),
    ]


def test_an_open_breaker_without_a_recorded_deadline_stays_open(
    conn: psycopg.Connection,
) -> None:
    """No deadline is not evidence a deadline has passed. Fail closed."""
    set_breaker_state(conn, open_breaker=True, reason_code="test")  # no reset_seconds
    assert read_breaker(conn).reset_after is None

    assert tick(conn) == "held_open_no_deadline"
    assert read_breaker(conn).is_open


def test_a_successful_tick_alone_does_not_close_the_breaker(
    conn: psycopg.Connection,
) -> None:
    """Only an elapsed deadline closes it -- never merely running without error."""
    fail(conn, THRESHOLD)
    tick(conn)

    for _ in range(3):
        tick(conn)

    assert read_breaker(conn).is_open


# ---- fail closed ----------------------------------------------------------


def test_a_read_failure_cannot_close_an_open_breaker(
    conn: psycopg.Connection, monkeypatch
) -> None:
    """An error is never permission to open the gate."""
    fail(conn, THRESHOLD)
    tick(conn)
    assert read_breaker(conn).is_open

    import reclaim.jobs.breaker as monitor

    monkeypatch.setattr(
        monitor, "read_breaker", lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("cannot read breaker")
        )
    )
    with pytest.raises(RuntimeError):
        tick(conn)

    monkeypatch.undo()
    assert read_breaker(conn).is_open, "the breaker survived the failed tick"


def test_a_failing_tick_does_not_end_the_polling_loop(
    conn: psycopg.Connection,
) -> None:
    """Stage 1's runner contract: log the tick, keep the process alive."""
    spec = register_batch_jobs(JobRegistry()).get(BREAKER_MONITOR)
    attempts = {"n": 0}

    def flaky(c, *, limit):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient")
        return spec.operation(c, limit=limit)

    ticks = run_batch(
        name=spec.name, connect=connect_to(conn), operation=flaky,
        interval_seconds=spec.interval_seconds, limit=spec.limit,
        should_continue=at_most(2), clock=FakeClock(),
    )

    assert [t.worked for t in ticks] == [False, True]
    assert isinstance(ticks[0].error, RuntimeError)


def test_the_monitor_takes_no_per_case_lease(
    conn: psycopg.Connection, monkeypatch
) -> None:
    import reclaim.jobs.runner as runner

    def forbidden(*a, **k):  # pragma: no cover - asserted by never running
        raise AssertionError("the breaker monitor must not claim a case")

    monkeypatch.setattr(runner, "claim_next", forbidden)
    monkeypatch.setattr(runner, "release_lease", forbidden)

    spec = register_batch_jobs(JobRegistry()).get(BREAKER_MONITOR)
    run_batch(
        name=spec.name, connect=connect_to(conn), operation=spec.operation,
        interval_seconds=spec.interval_seconds, limit=spec.limit,
        should_continue=at_most(1), clock=FakeClock(),
    )


def test_the_monitor_writes_the_breaker_only_through_the_audited_operation(
    conn: psycopg.Connection, monkeypatch
) -> None:
    """If `set_breaker_state` is unavailable, no state change can happen."""
    import reclaim.jobs.breaker as monitor

    calls: list[dict] = []
    monkeypatch.setattr(
        monitor, "set_breaker_state",
        lambda _c, **kw: (calls.append(kw), True)[1],
    )

    fail(conn, THRESHOLD)
    tick(conn)

    assert len(calls) == 1 and calls[0]["open_breaker"] is True
    assert calls[0]["worker_id"] == MONITOR_WORKER_ID
    assert calls[0]["reset_seconds"] == RESET_SECONDS
    assert not read_breaker(conn).is_open, "nothing moved without the real domain call"
