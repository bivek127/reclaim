"""The breaker monitor: when the gate opens, when it closes, and when it must not.

Breaker *mechanics* -- what `set_breaker_state` writes, what it audits, how the
counter moves -- are the domain suite's subject. These tests cover the decision:
given a breaker state and a clock, does the monitor call the domain, and with
what.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from reclaim.config import load_operational, load_policy
from reclaim.domain.breaker import (
    read_breaker,
    record_execution_outcome,
    resume_halted_cases,
    set_breaker_state,
)
from reclaim.jobs.breaker import MONITOR_WORKER_ID, monitor_breaker
from reclaim.jobs.jobs import BREAKER_MONITOR, register_batch_jobs
from reclaim.jobs.registry import JobKind, JobRegistry
from reclaim.jobs.runner import at_most, run_batch
from reclaim.provider.contract import ProviderOutcome
from tests.db.helpers import insert_case, insert_obligation

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


def tick(
    conn: psycopg.Connection,
    *,
    now: datetime | None = None,
    resume_limit: int | None = None,
) -> str:
    return monitor_breaker(
        conn,
        failure_threshold=THRESHOLD,
        reset_seconds=RESET_SECONDS,
        resume_limit=resume_limit,
        now=now,
    )


def fail(conn: psycopg.Connection, times: int) -> None:
    for _ in range(times):
        record_execution_outcome(conn, ProviderOutcome.TRANSPORT_ERROR)


def succeed(conn: psycopg.Connection) -> None:
    record_execution_outcome(conn, ProviderOutcome.ACCEPTED)


def seed_halted(
    conn: psycopg.Connection,
    *,
    active_elapsed_ms: int = 4_000,
    fencing_token: int = 0,
) -> int:
    """A case the executor halted before dispatch: `active_since` NULL, the
    banked elapsed time it already had when the breaker opened, no lease."""
    token = uuid.uuid4().hex
    obligation_id = insert_obligation(
        conn,
        anchor_key=token,
        anchor_canonical=f"order:{token}",
        source_event_id=f"evt-{token}",
    )
    case_id = insert_case(conn, obligation_id, state="HALTED", active_since=None)
    conn.execute(
        "UPDATE recovery_cases"
        "   SET active_elapsed_ms = %s, fencing_token = %s,"
        "       worker_id = NULL, lease_expires_at = '-infinity'"
        " WHERE id = %s",
        (active_elapsed_ms, fencing_token, case_id),
    )
    return case_id


def halted_case_row(conn: psycopg.Connection, case_id: int) -> dict:
    row = conn.execute(
        "SELECT state::text, active_since, active_elapsed_ms, fencing_token,"
        "       worker_id"
        "  FROM recovery_cases WHERE id = %s",
        (case_id,),
    ).fetchone()
    assert row is not None
    return {
        "state": row[0],
        "active_since": row[1],
        "active_elapsed_ms": row[2],
        "fencing_token": row[3],
        "worker_id": row[4],
    }


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


# ---- resuming HALTED cases --------------------------------------------------
#
# Whether a HALTED case ever *should* have dispatched again is settled by the
# breaker gate inside dispatch itself, unchanged by any of this: resuming only
# makes the case ACTION_READY again, eligible for the executor to reconsider.


def test_a_closed_breaker_resumes_a_halted_case(conn: psycopg.Connection) -> None:
    case_id = seed_halted(conn)

    resumed = resume_halted_cases(conn, limit=100)

    assert resumed == 1
    assert halted_case_row(conn, case_id)["state"] == "ACTION_READY"


def test_resumption_happens_through_the_registered_monitor_operation(
    conn: psycopg.Connection,
) -> None:
    """Integration through the real, registered batch job -- not the domain
    function called directly."""
    case_id = seed_halted(conn)
    spec = register_batch_jobs(JobRegistry()).get(BREAKER_MONITOR)

    ticks = run_batch(
        name=spec.name, connect=connect_to(conn), operation=spec.operation,
        interval_seconds=spec.interval_seconds, limit=spec.limit,
        should_continue=at_most(1), clock=FakeClock(),
    )

    assert ticks[0].worked is True and ticks[0].error is None
    assert halted_case_row(conn, case_id)["state"] == "ACTION_READY"


def test_ttl_active_since_restarts_from_now(conn: psycopg.Connection) -> None:
    case_id = seed_halted(conn)
    before = datetime.now(timezone.utc)

    resume_halted_cases(conn, limit=100)

    row = halted_case_row(conn, case_id)
    assert row["active_since"] is not None
    assert row["active_since"] >= before


def test_previously_banked_elapsed_time_is_preserved(
    conn: psycopg.Connection,
) -> None:
    case_id = seed_halted(conn, active_elapsed_ms=17_500)

    resume_halted_cases(conn, limit=100)

    assert halted_case_row(conn, case_id)["active_elapsed_ms"] == 17_500


def test_multiple_halted_cases_resume_in_one_batch(
    conn: psycopg.Connection,
) -> None:
    ids = [seed_halted(conn) for _ in range(3)]

    resumed = resume_halted_cases(conn, limit=100)

    assert resumed == 3
    for case_id in ids:
        assert halted_case_row(conn, case_id)["state"] == "ACTION_READY"


def test_the_batch_limit_is_respected(conn: psycopg.Connection) -> None:
    ids = [seed_halted(conn) for _ in range(5)]

    resumed = resume_halted_cases(conn, limit=2)

    assert resumed == 2
    states = [halted_case_row(conn, cid)["state"] for cid in ids]
    assert states.count("ACTION_READY") == 2
    assert states.count("HALTED") == 3


def test_concurrent_workers_do_not_resume_the_same_case_twice(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    """Two real connections racing the same row: SKIP LOCKED must mean only
    one of them ever claims it."""
    case_id = seed_halted(conn)

    with psycopg.connect(migrated_database, autocommit=True) as other:
        with conn.transaction():
            held = conn.execute(
                "SELECT id FROM recovery_cases WHERE id = %s FOR UPDATE",
                (case_id,),
            ).fetchone()
            assert held is not None

            # The second connection's own SELECT ... FOR UPDATE SKIP LOCKED
            # must skip the row the first still holds, and therefore resume
            # nothing.
            resumed_by_other = resume_halted_cases(other, limit=100)
            assert resumed_by_other == 0

        # Only after the first transaction releases the lock does the row
        # become available; a third caller here would now resume it.
        assert halted_case_row(conn, case_id)["state"] == "HALTED"


def test_the_fencing_token_is_bumped_and_the_bumped_value_is_used(
    conn: psycopg.Connection,
) -> None:
    case_id = seed_halted(conn, fencing_token=7)

    resume_halted_cases(conn, limit=100)

    row = halted_case_row(conn, case_id)
    assert row["fencing_token"] == 8, "bumped exactly once"
    events = conn.execute(
        "SELECT fencing_token FROM audit_events"
        " WHERE case_id = %s AND event_type = 'state_transition'"
        " ORDER BY id DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    assert events is not None and events[0] == 8, "the transition used the bumped token"


def test_an_open_breaker_does_not_resume_halted_cases(
    conn: psycopg.Connection,
) -> None:
    fail(conn, THRESHOLD)
    tick(conn)  # opens it
    assert read_breaker(conn).is_open
    case_id = seed_halted(conn)

    result = tick(conn, now=datetime.now(timezone.utc), resume_limit=100)

    assert result == "held_open_before_deadline"
    assert halted_case_row(conn, case_id)["state"] == "HALTED"


def test_a_breaker_below_threshold_still_resumes_leftover_halted_cases(
    conn: psycopg.Connection,
) -> None:
    """A HALTED case can outlive the tick that halted it (the breaker may
    have opened and closed again before this one is next looked at); a
    steady-closed tick must not ignore it."""
    case_id = seed_halted(conn)

    result = tick(conn, resume_limit=100)

    assert result == "below_threshold"
    assert halted_case_row(conn, case_id)["state"] == "ACTION_READY"


def test_the_tick_that_closes_the_breaker_also_resumes_in_the_same_tick(
    conn: psycopg.Connection,
) -> None:
    fail(conn, THRESHOLD)
    tick(conn)
    deadline = read_breaker(conn).reset_after
    assert deadline is not None
    case_id = seed_halted(conn)

    result = tick(conn, now=deadline + timedelta(seconds=1), resume_limit=100)

    assert result == "closed"
    assert halted_case_row(conn, case_id)["state"] == "ACTION_READY"


def test_a_failed_transition_leaves_the_case_exactly_as_it_was(
    conn: psycopg.Connection, monkeypatch
) -> None:
    """An illegal-transition or other domain error must not half-apply: no
    fencing bump survives without the matching state change."""
    import reclaim.domain.breaker as breaker_mod

    def explode(*_a, **_k):
        raise RuntimeError("transition blew up")

    monkeypatch.setattr(breaker_mod, "transition", explode)
    case_id = seed_halted(conn, fencing_token=3)

    with pytest.raises(RuntimeError):
        resume_halted_cases(conn, limit=100)

    row = halted_case_row(conn, case_id)
    assert row["state"] == "HALTED", "no partial state change"
    assert row["fencing_token"] == 3, "the bump did not survive the failed transition"


def test_no_provider_call_occurs(conn: psycopg.Connection) -> None:
    """Nothing about resuming reaches the network -- there is no provider
    handle anywhere in this path to call.

    Executable code only: prose explaining that no provider is called must
    not read as evidence that one is.
    """
    import ast
    import inspect

    seed_halted(conn)

    tree = ast.parse(inspect.getsource(resume_halted_cases))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:]
    source = ast.unparse(tree)

    for forbidden in ("provider", "RazorpayAdapter", "http", "requests"):
        assert forbidden not in source, f"resume path references {forbidden}"


def test_no_action_is_created(conn: psycopg.Connection) -> None:
    case_id = seed_halted(conn)
    before = conn.execute(
        "SELECT count(*) FROM recovery_actions WHERE case_id = %s", (case_id,)
    ).fetchone()[0]

    resume_halted_cases(conn, limit=100)

    after = conn.execute(
        "SELECT count(*) FROM recovery_actions WHERE case_id = %s", (case_id,)
    ).fetchone()[0]
    assert after == before == 0


def test_only_halted_cases_are_selected(conn: psycopg.Connection) -> None:
    """A batch bounded by state alone must never touch an unrelated case."""
    obligation_id = insert_obligation(
        conn, anchor_key="ord_other", anchor_canonical="order:ord_other",
        source_event_id="evt_other",
    )
    other_case = insert_case(conn, obligation_id, state="NEW")

    resume_halted_cases(conn, limit=100)

    row = conn.execute(
        "SELECT state::text FROM recovery_cases WHERE id = %s", (other_case,)
    ).fetchone()
    assert row is not None and row[0] == "NEW"
