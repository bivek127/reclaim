"""The executor job and the domain accessor it depends on.

Execution itself -- attempts, idempotency keys, the breaker gate, outcome
mapping -- is settled by the execution suites. What matters here is that the
job claims the right state, asks the domain which decision authorised the
action, and hands both that id and the fencing token to `dispatch` unchanged.
"""

from __future__ import annotations

from contextlib import contextmanager

import psycopg
import pytest

from reclaim.api.db import app_conn
from reclaim.config import lease_seconds_for, load_operational
from reclaim.domain.leases import claim_next
from reclaim.domain.policy import authorising_decision_id
from reclaim.domain.states import CaseState
from reclaim.jobs.jobs import EXECUTOR, register_per_case_jobs
from reclaim.jobs.registry import JobKind, JobRegistry
from reclaim.jobs.runner import at_most, run_per_case
from tests.db.helpers import insert_case, insert_obligation, insert_policy_decision


class FakeClock:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


class UnusableProvider:
    def __getattr__(self, name):
        def boom(*_a, **_k):
            raise AssertionError(f"the runtime called the provider: {name}")

        return boom


def connect_to(conn: psycopg.Connection):
    @contextmanager
    def factory():
        yield conn

    return factory


@pytest.fixture
def registry() -> JobRegistry:
    return register_per_case_jobs(JobRegistry(), provider=UnusableProvider)


def seed_case(conn: psycopg.Connection, suffix: str) -> int:
    obligation_id = insert_obligation(
        conn,
        anchor_key=f"ord_ex_{suffix}",
        anchor_canonical=f"order:ord_ex_{suffix}",
        source_event_id=f"evt_ex_{suffix}",
    )
    case_id = insert_case(conn, obligation_id, state="ACTION_READY")
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = '-infinity' WHERE id = %s",
        (case_id,),
    )
    return case_id


def lease_row(conn: psycopg.Connection, case_id: int) -> tuple:
    return conn.execute(
        "SELECT worker_id, fencing_token, state::text FROM recovery_cases WHERE id = %s",
        (case_id,),
    ).fetchone()


# ---- the domain accessor --------------------------------------------------


def test_the_accessor_returns_the_allow_decision(conn: psycopg.Connection) -> None:
    case_id = seed_case(conn, "one_allow")
    allow = insert_policy_decision(conn, case_id, verdict="ALLOW",
                                   selected_action="CREATE_PAYMENT_LINK")

    assert authorising_decision_id(conn, case_id) == allow


def test_the_accessor_ignores_an_escalate_decision(conn: psycopg.Connection) -> None:
    """A later ESCALATE does not authorise a dispatch, however recent it is."""
    case_id = seed_case(conn, "escalate")
    allow = insert_policy_decision(conn, case_id, verdict="ALLOW",
                                   selected_action="CREATE_PAYMENT_LINK")
    # ck_allow_has_action: only an ALLOW may carry a selected_action.
    insert_policy_decision(
        conn, case_id, verdict="ESCALATE", selected_action=None,
        reason_code="policy_escalate_budget",
    )

    assert authorising_decision_id(conn, case_id) == allow


def test_the_accessor_takes_the_most_recent_allow(conn: psycopg.Connection) -> None:
    """A failed attempt routes back through POLICY_EVAL and records another."""
    case_id = seed_case(conn, "two_allows")
    insert_policy_decision(conn, case_id, verdict="ALLOW",
                           selected_action="CREATE_PAYMENT_LINK")
    second = insert_policy_decision(conn, case_id, verdict="ALLOW",
                                    selected_action="CREATE_PAYMENT_LINK")

    assert second > 0
    assert authorising_decision_id(conn, case_id) == second, "ordered by id, latest wins"


def test_the_accessor_returns_none_when_nothing_authorised_anything(
    conn: psycopg.Connection,
) -> None:
    case_id = seed_case(conn, "none")
    assert authorising_decision_id(conn, case_id) is None


def test_the_accessor_is_scoped_to_one_case(conn: psycopg.Connection) -> None:
    mine = seed_case(conn, "mine")
    theirs = seed_case(conn, "theirs")
    insert_policy_decision(conn, theirs, verdict="ALLOW",
                           selected_action="CREATE_PAYMENT_LINK")

    assert authorising_decision_id(conn, mine) is None


def test_the_accessor_mutates_nothing(conn: psycopg.Connection) -> None:
    case_id = seed_case(conn, "readonly")
    insert_policy_decision(conn, case_id, verdict="ALLOW",
                           selected_action="CREATE_PAYMENT_LINK")
    before = lease_row(conn, case_id)
    decisions = conn.execute("SELECT count(*) FROM policy_decisions").fetchone()[0]

    authorising_decision_id(conn, case_id)

    assert lease_row(conn, case_id) == before
    assert conn.execute("SELECT count(*) FROM policy_decisions").fetchone()[0] == decisions


# ---- registration ---------------------------------------------------------


def test_the_executor_matches_its_declared_contract(registry: JobRegistry) -> None:
    spec = registry.get(EXECUTOR)
    assert spec.kind is JobKind.PER_CASE
    assert spec.expected_state is CaseState.ACTION_READY
    assert spec.lease_seconds == lease_seconds_for("execution") == 60
    assert spec.interval_seconds == int(load_operational()["executor_interval_seconds"])
    assert spec.interval_seconds == 5
    assert spec.connect is app_conn
    assert spec.limit is None


# ---- orchestration --------------------------------------------------------


def test_the_decision_id_and_token_reach_dispatch_unchanged(
    registry: JobRegistry, conn: psycopg.Connection, monkeypatch
) -> None:
    import reclaim.jobs.percase as percase

    case_id = seed_case(conn, "passthrough")
    allow = insert_policy_decision(conn, case_id, verdict="ALLOW",
                                   selected_action="CREATE_PAYMENT_LINK")
    seen: list[dict] = []
    monkeypatch.setattr(
        percase, "dispatch", lambda conn, cid, **kw: seen.append({"case": cid, **kw})
    )

    registry.get(EXECUTOR).operation(conn, case_id, fencing_token=41)

    assert len(seen) == 1
    call = seen[0]
    assert call["case"] == case_id
    assert call["policy_decision_id"] == allow
    assert call["fencing_token"] == 41
    assert call["link_ttl_seconds"] == int(
        load_operational()["payment_link_ttl_seconds"]
    )


def test_the_executor_uses_the_existing_dispatch_function() -> None:
    """Not a reimplementation: the adapter calls the domain's own entry point."""
    import inspect

    import reclaim.jobs.percase as percase
    from reclaim.domain.execution import dispatch

    assert percase.dispatch is dispatch
    source = inspect.getsource(percase.executor_operation)
    assert "dispatch(" in source
    assert "authorising_decision_id(" in source


def test_a_case_with_no_authorising_decision_is_refused(
    registry: JobRegistry, conn: psycopg.Connection, monkeypatch
) -> None:
    """Rather than inventing an authorisation, the tick fails and releases."""
    import reclaim.jobs.percase as percase

    case_id = seed_case(conn, "unauthorised")
    monkeypatch.setattr(
        percase, "dispatch",
        lambda *a, **k: pytest.fail("dispatch ran without an authorising decision"),
    )

    with pytest.raises(RuntimeError, match="no authorising policy decision"):
        registry.get(EXECUTOR).operation(conn, case_id, fencing_token=1)


def test_a_successful_tick_releases_the_lease(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    case_id = seed_case(conn, "release_ok")
    insert_policy_decision(conn, case_id, verdict="ALLOW",
                           selected_action="CREATE_PAYMENT_LINK")
    spec = registry.get(EXECUTOR)

    run_per_case(
        name=spec.name, connect=connect_to(conn),
        operation=lambda _c, _cid, *, fencing_token: None,
        expected_state=spec.expected_state, worker_id=spec.name,
        lease_seconds=spec.lease_seconds, interval_seconds=spec.interval_seconds,
        should_continue=at_most(1), clock=FakeClock(),
    )

    worker_id, _, state = lease_row(conn, case_id)
    assert worker_id is None
    assert state == "ACTION_READY", "the runtime moved no state itself"


def test_a_domain_failure_releases_the_lease(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    case_id = seed_case(conn, "release_fail")
    spec = registry.get(EXECUTOR)

    ticks = run_per_case(
        name=spec.name, connect=connect_to(conn), operation=spec.operation,
        expected_state=spec.expected_state, worker_id=spec.name,
        lease_seconds=spec.lease_seconds, interval_seconds=spec.interval_seconds,
        should_continue=at_most(1), clock=FakeClock(),
    )

    # No ALLOW decision exists, so the adapter raises before any dispatch.
    assert isinstance(ticks[0].error, RuntimeError)
    assert lease_row(conn, case_id)[0] is None


def test_a_case_held_by_another_worker_is_left_alone(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    case_id = seed_case(conn, "held")
    other = claim_next(conn, CaseState.ACTION_READY, "someone-else", 300)
    assert other is not None

    spec = registry.get(EXECUTOR)
    called: list[int] = []
    run_per_case(
        name=spec.name, connect=connect_to(conn),
        operation=lambda _c, cid, *, fencing_token: called.append(cid),
        expected_state=spec.expected_state, worker_id=spec.name,
        lease_seconds=spec.lease_seconds, interval_seconds=spec.interval_seconds,
        should_continue=at_most(1), clock=FakeClock(),
    )

    assert called == []
    assert lease_row(conn, case_id)[0] == "someone-else"


def test_a_stale_token_cannot_move_the_case(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    from reclaim.domain.leases import fenced_transition

    case_id = seed_case(conn, "stale")
    stale = lease_row(conn, case_id)[1]
    outcomes: list[bool] = []
    spec = registry.get(EXECUTOR)

    def write_stale(c, cid, *, fencing_token):
        outcomes.append(
            fenced_transition(
                c, cid, CaseState.ACTION_READY, CaseState.EXECUTING, stale,
                "probe", worker_id="probe",
            )
        )

    run_per_case(
        name=spec.name, connect=connect_to(conn), operation=write_stale,
        expected_state=spec.expected_state, worker_id=spec.name,
        lease_seconds=spec.lease_seconds, interval_seconds=spec.interval_seconds,
        should_continue=at_most(1), clock=FakeClock(),
    )

    assert outcomes == [False]
    assert lease_row(conn, case_id)[2] == "ACTION_READY"


def test_the_executor_does_not_call_the_provider_itself(
    registry: JobRegistry, conn: psycopg.Connection, monkeypatch
) -> None:
    """The provider is handed to dispatch; the adapter never uses it."""
    import reclaim.jobs.percase as percase

    case_id = seed_case(conn, "provider")
    insert_policy_decision(conn, case_id, verdict="ALLOW",
                           selected_action="CREATE_PAYMENT_LINK")
    captured: list = []
    monkeypatch.setattr(
        percase, "dispatch", lambda conn, cid, **kw: captured.append(kw["provider"])
    )

    registry.get(EXECUTOR).operation(conn, case_id, fencing_token=1)

    assert len(captured) == 1, "passed through, not invoked"
