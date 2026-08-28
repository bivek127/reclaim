"""The two runtime loop primitives.

These exercise the production loop directly: the stop condition and the clock
are injected, so there is no thread, no timeout, and no test-only code path.
"""

from __future__ import annotations

from contextlib import contextmanager

import psycopg
import pytest

from reclaim.domain.leases import claim_next
from reclaim.domain.states import CaseState
from reclaim.jobs.runner import at_most, run_batch, run_per_case
from tests.db.helpers import insert_case, insert_obligation


class FakeClock:
    """Records what the loop asked to sleep for, without sleeping."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def connect_to(conn: psycopg.Connection):
    """Hand the loop the test's own connection, without closing it."""

    @contextmanager
    def factory():
        yield conn

    return factory


def seed_case(conn: psycopg.Connection, state: str, suffix: str) -> int:
    obligation_id = insert_obligation(
        conn,
        anchor_key=f"ord_{suffix}",
        anchor_canonical=f"order:ord_{suffix}",
        source_event_id=f"evt_{suffix}",
    )
    case_id = insert_case(conn, obligation_id, state=state)
    # Seeded cases hold no lease, so the runner can claim immediately.
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = '-infinity' WHERE id = %s",
        (case_id,),
    )
    return case_id


def lease_row(conn: psycopg.Connection, case_id: int) -> tuple:
    return conn.execute(
        # lease_expires_at is read as text: release_lease sets it to '-infinity',
        # which has no Python datetime equivalent.
        "SELECT worker_id, lease_expires_at::text, fencing_token, state::text "
        "FROM recovery_cases WHERE id = %s",
        (case_id,),
    ).fetchone()


# ---- batch runner ---------------------------------------------------------


def test_batch_runner_invokes_its_operation_once_per_tick(
    conn: psycopg.Connection,
) -> None:
    calls: list[int] = []
    clock = FakeClock()

    ticks = run_batch(
        name="probe",
        connect=connect_to(conn),
        operation=lambda _conn, limit: calls.append(limit),
        interval_seconds=15,
        limit=100,
        should_continue=at_most(3),
        clock=clock,
    )

    assert len(calls) == 3
    assert [t.worked for t in ticks] == [True, True, True]


def test_batch_runner_sleeps_for_the_configured_interval(
    conn: psycopg.Connection,
) -> None:
    """The interval is a caller's decision; the runner carries no schedule."""
    clock = FakeClock()

    run_batch(
        name="probe",
        connect=connect_to(conn),
        operation=lambda _conn, limit: None,
        interval_seconds=60,
        limit=10,
        should_continue=at_most(2),
        clock=clock,
    )

    assert clock.slept == [60, 60]


def test_batch_runner_passes_the_configured_limit(conn: psycopg.Connection) -> None:
    seen: list[int] = []

    run_batch(
        name="probe",
        connect=connect_to(conn),
        operation=lambda _conn, limit: seen.append(limit),
        interval_seconds=1,
        limit=42,
        should_continue=at_most(1),
        clock=FakeClock(),
    )

    assert seen == [42]


def test_a_failing_batch_operation_does_not_end_the_loop(
    conn: psycopg.Connection,
) -> None:
    """A sweep that cannot run this tick is not a reason to stop sweeping."""
    attempts = {"n": 0}

    def flaky(_conn, limit):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient")
        return "ok"

    ticks = run_batch(
        name="probe",
        connect=connect_to(conn),
        operation=flaky,
        interval_seconds=1,
        limit=1,
        should_continue=at_most(2),
        clock=FakeClock(),
    )

    assert [t.worked for t in ticks] == [False, True]
    assert isinstance(ticks[0].error, RuntimeError)
    assert ticks[1].result == "ok"


# ---- per-case runner ------------------------------------------------------


def test_per_case_runner_passes_the_claims_fencing_token_unchanged(
    conn: psycopg.Connection,
) -> None:
    case_id = seed_case(conn, "NEW", "tok")
    before = lease_row(conn, case_id)[2]
    seen: list[tuple[int, int]] = []

    run_per_case(
        name="probe",
        connect=connect_to(conn),
        operation=lambda _c, cid, *, fencing_token, **_: seen.append((cid, fencing_token)),
        expected_states=(CaseState.NEW,),
        worker_id="probe",
        lease_seconds=30,
        interval_seconds=5,
        should_continue=at_most(1),
        clock=FakeClock(),
    )

    assert seen == [(case_id, before + 1)], "the runner must not alter the token"


def test_per_case_runner_releases_the_lease_after_success(
    conn: psycopg.Connection,
) -> None:
    case_id = seed_case(conn, "NEW", "ok")

    run_per_case(
        name="probe",
        connect=connect_to(conn),
        operation=lambda _c, _cid, *, fencing_token, **_: None,
        expected_states=(CaseState.NEW,),
        worker_id="probe",
        lease_seconds=30,
        interval_seconds=5,
        should_continue=at_most(1),
        clock=FakeClock(),
    )

    worker_id, _, _, state = lease_row(conn, case_id)
    assert worker_id is None, "a finished case must not stay held"
    assert state == "NEW", "the runner changes no state of its own"


def test_per_case_runner_releases_the_lease_after_a_domain_failure(
    conn: psycopg.Connection,
) -> None:
    """A raising operation must not strand the case for a full lease period."""
    case_id = seed_case(conn, "NEW", "boom")

    def explode(_c, _cid, *, fencing_token, **_):
        raise RuntimeError("domain refused")

    ticks = run_per_case(
        name="probe",
        connect=connect_to(conn),
        operation=explode,
        expected_states=(CaseState.NEW,),
        worker_id="probe",
        lease_seconds=30,
        interval_seconds=5,
        should_continue=at_most(1),
        clock=FakeClock(),
    )

    assert isinstance(ticks[0].error, RuntimeError)
    worker_id, _, _, state = lease_row(conn, case_id)
    assert worker_id is None
    assert state == "NEW", "a failed tick leaves state unchanged"


def test_an_idle_tick_is_not_a_failure(conn: psycopg.Connection) -> None:
    """Nothing claimable is the normal case, not an error."""
    called: list[int] = []

    ticks = run_per_case(
        name="probe",
        connect=connect_to(conn),
        operation=lambda _c, cid, *, fencing_token, **_: called.append(cid),
        expected_states=(CaseState.RECONCILING,),
        worker_id="probe",
        lease_seconds=30,
        interval_seconds=5,
        should_continue=at_most(2),
        clock=FakeClock(),
    )

    assert called == []
    assert [(t.worked, t.error) for t in ticks] == [(False, None), (False, None)]


def test_the_runner_claims_through_the_domain_not_by_writing_columns(
    conn: psycopg.Connection,
) -> None:
    """Ownership columns are the domain's. The runner only calls its API.

    Proven behaviourally: a case already held by another worker on an unexpired
    lease is invisible to the runner, because `claim_next` refuses it -- which
    could not be true if the runner set `worker_id` itself.
    """
    case_id = seed_case(conn, "NEW", "held")
    other = claim_next(conn, CaseState.NEW, "someone-else", 300)
    assert other is not None and other.case_id == case_id

    called: list[int] = []
    run_per_case(
        name="probe",
        connect=connect_to(conn),
        operation=lambda _c, cid, *, fencing_token, **_: called.append(cid),
        expected_states=(CaseState.NEW,),
        worker_id="probe",
        lease_seconds=30,
        interval_seconds=5,
        should_continue=at_most(1),
        clock=FakeClock(),
    )

    assert called == [], "a held case must not be picked up"
    worker_id, _, token, _ = lease_row(conn, case_id)
    assert worker_id == "someone-else", "the runner did not steal the lease"
    assert token == other.fencing_token, "the runner did not bump the token"


def test_stale_fencing_stays_the_domains_decision(conn: psycopg.Connection) -> None:
    """The runner hands the token over and does not second-guess it.

    A fenced write made with an outdated token is refused by the domain. The
    runner has no branch for that: it neither re-checks the token nor suppresses
    the refusal.
    """
    from reclaim.domain.leases import fenced_transition

    case_id = seed_case(conn, "NEW", "stale")
    stale_token = lease_row(conn, case_id)[2]
    outcomes: list[bool] = []

    def write_with_a_stale_token(c, cid, *, fencing_token, **_):
        # Deliberately ignore the fresh token and use the pre-claim one.
        outcomes.append(
            fenced_transition(
                c, cid, CaseState.NEW, CaseState.ENRICHING, stale_token,
                "probe", worker_id="probe",
            )
        )

    run_per_case(
        name="probe",
        connect=connect_to(conn),
        operation=write_with_a_stale_token,
        expected_states=(CaseState.NEW,),
        worker_id="probe",
        lease_seconds=30,
        interval_seconds=5,
        should_continue=at_most(1),
        clock=FakeClock(),
    )

    assert outcomes == [False], "the domain refused the stale write"
    assert lease_row(conn, case_id)[3] == "NEW", "and no state moved"
