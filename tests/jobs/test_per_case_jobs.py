"""The per-case jobs: reconciler and verifier.

Registration and orchestration only. What `reconcile_case` and `verify_case`
decide about a case is settled by the domain suites; repeating it here would
create a second, weaker oracle.
"""

from __future__ import annotations

from contextlib import contextmanager

import psycopg
import pytest

from reclaim.api.db import app_conn, verifier_conn
from reclaim.config import lease_seconds_for, load_operational
from reclaim.domain.leases import claim_next
from reclaim.domain.reconciliation import reconcile_case
from reclaim.domain.states import CaseState
from reclaim.domain.verification import verify_case
from reclaim.jobs.jobs import RECONCILER, VERIFIER, register_per_case_jobs
from reclaim.jobs.registry import JobKind, JobRegistry
from reclaim.jobs.runner import at_most, run_per_case
from tests.db.helpers import insert_case, insert_obligation

# name, state, lease key, interval key, connection, domain function
JOBS = [
    (RECONCILER, CaseState.AMBIGUOUS, "reconciliation",
     "reconciliation_interval_seconds", app_conn, reconcile_case),
    (VERIFIER, CaseState.AWAITING_CUSTOMER, "verification",
     "verifier_interval_seconds", verifier_conn, verify_case),
]
IDS = [j[0] for j in JOBS]


class FakeClock:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


class UnusableProvider:
    """Any provider call is a failure: these tests never reach the network."""

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


def seed(conn: psycopg.Connection, state: CaseState, suffix: str) -> int:
    obligation_id = insert_obligation(
        conn,
        anchor_key=f"ord_pc_{suffix}",
        anchor_canonical=f"order:ord_pc_{suffix}",
        source_event_id=f"evt_pc_{suffix}",
    )
    case_id = insert_case(conn, obligation_id, state=state.value)
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


# ---- registration ---------------------------------------------------------


def test_exactly_the_defined_per_case_jobs_are_registered(
    registry: JobRegistry,
) -> None:
    """The case worker is absent: its contract is still incomplete."""
    assert registry.names() == ["executor", "reconciler", "verifier"]


@pytest.mark.parametrize("name,state,lease,interval,conn_fn,_fn", JOBS, ids=IDS)
def test_each_job_matches_its_declared_contract(
    registry: JobRegistry, name, state, lease, interval, conn_fn, _fn
) -> None:
    spec = registry.get(name)
    assert spec.kind is JobKind.PER_CASE
    assert spec.expected_state is state
    assert spec.lease_seconds == lease_seconds_for(lease)
    assert spec.interval_seconds == int(load_operational()[interval])
    assert spec.connect is conn_fn
    assert spec.limit is None, "a per-case job takes one case, not a batch"


def test_the_verifier_alone_runs_as_the_verifier_role(
    registry: JobRegistry,
) -> None:
    """Revenue needs a role the application role deliberately lacks."""
    assert registry.get(VERIFIER).connect is verifier_conn
    assert registry.get(RECONCILER).connect is app_conn


@pytest.mark.parametrize("name,_s,_l,_i,_c,domain_fn", JOBS, ids=IDS)
def test_each_job_calls_the_existing_domain_function(
    registry: JobRegistry, monkeypatch, name, _s, _l, _i, _c, domain_fn
) -> None:
    """The adapter binds a provider and a worker id; it decides nothing else."""
    import reclaim.jobs.percase as percase

    seen: list[tuple] = []
    monkeypatch.setattr(
        percase, domain_fn.__name__,
        lambda conn, case_id, **kw: seen.append((case_id, kw)),
    )
    registry.get(name).operation(object(), 77, fencing_token=9)

    assert len(seen) == 1
    case_id, kwargs = seen[0]
    assert case_id == 77
    assert kwargs["fencing_token"] == 9, "the token is passed straight through"


# ---- claim / release ------------------------------------------------------


@pytest.mark.parametrize("name,state,_l,_i,_c,_f", JOBS, ids=IDS)
def test_the_claims_token_reaches_the_domain_unchanged(
    registry: JobRegistry, conn: psycopg.Connection, name, state, _l, _i, _c, _f
) -> None:
    case_id = seed(conn, state, f"tok_{name}")
    before = lease_row(conn, case_id)[1]
    seen: list[tuple[int, int]] = []
    spec = registry.get(name)

    run_per_case(
        name=spec.name, connect=connect_to(conn),
        operation=lambda _c, cid, *, fencing_token: seen.append((cid, fencing_token)),
        expected_state=spec.expected_state, worker_id=spec.name,
        lease_seconds=spec.lease_seconds, interval_seconds=spec.interval_seconds,
        should_continue=at_most(1), clock=FakeClock(),
    )

    assert seen == [(case_id, before + 1)]


@pytest.mark.parametrize("name,state,_l,_i,_c,_f", JOBS, ids=IDS)
def test_a_successful_tick_releases_the_lease(
    registry: JobRegistry, conn: psycopg.Connection, name, state, _l, _i, _c, _f
) -> None:
    case_id = seed(conn, state, f"ok_{name}")
    spec = registry.get(name)

    run_per_case(
        name=spec.name, connect=connect_to(conn),
        operation=lambda _c, _cid, *, fencing_token: None,
        expected_state=spec.expected_state, worker_id=spec.name,
        lease_seconds=spec.lease_seconds, interval_seconds=spec.interval_seconds,
        should_continue=at_most(1), clock=FakeClock(),
    )

    worker_id, _, resulting_state = lease_row(conn, case_id)
    assert worker_id is None
    assert resulting_state == state.value, "the runtime moved no state itself"


@pytest.mark.parametrize("name,state,_l,_i,_c,_f", JOBS, ids=IDS)
def test_a_domain_failure_still_releases_the_lease(
    registry: JobRegistry, conn: psycopg.Connection, name, state, _l, _i, _c, _f
) -> None:
    """A raising operation must not strand the case for a whole lease period."""
    case_id = seed(conn, state, f"boom_{name}")
    spec = registry.get(name)

    def explode(_c, _cid, *, fencing_token):
        raise RuntimeError("the domain refused")

    ticks = run_per_case(
        name=spec.name, connect=connect_to(conn), operation=explode,
        expected_state=spec.expected_state, worker_id=spec.name,
        lease_seconds=spec.lease_seconds, interval_seconds=spec.interval_seconds,
        should_continue=at_most(1), clock=FakeClock(),
    )

    assert isinstance(ticks[0].error, RuntimeError)
    assert lease_row(conn, case_id)[0] is None
    assert lease_row(conn, case_id)[2] == state.value


@pytest.mark.parametrize("name,state,_l,_i,_c,_f", JOBS, ids=IDS)
def test_a_failing_tick_does_not_end_the_polling_loop(
    registry: JobRegistry, conn: psycopg.Connection, name, state, _l, _i, _c, _f
) -> None:
    seed(conn, state, f"loop_{name}")
    spec = registry.get(name)
    attempts = {"n": 0}

    def flaky(_c, _cid, *, fencing_token):
        attempts["n"] += 1
        raise RuntimeError("transient")

    ticks = run_per_case(
        name=spec.name, connect=connect_to(conn), operation=flaky,
        expected_state=spec.expected_state, worker_id=spec.name,
        lease_seconds=spec.lease_seconds, interval_seconds=spec.interval_seconds,
        should_continue=at_most(2), clock=FakeClock(),
    )

    assert len(ticks) == 2, "the loop kept going"
    assert attempts["n"] == 2, "and re-claimed the released case"


@pytest.mark.parametrize("name,state,_l,_i,_c,_f", JOBS, ids=IDS)
def test_an_idle_tick_is_not_an_error(
    registry: JobRegistry, conn: psycopg.Connection, name, state, _l, _i, _c, _f
) -> None:
    spec = registry.get(name)
    called: list[int] = []

    ticks = run_per_case(
        name=spec.name, connect=connect_to(conn),
        operation=lambda _c, cid, *, fencing_token: called.append(cid),
        expected_state=spec.expected_state, worker_id=spec.name,
        lease_seconds=spec.lease_seconds, interval_seconds=spec.interval_seconds,
        should_continue=at_most(1), clock=FakeClock(),
    )

    assert called == []
    assert ticks[0].worked is False and ticks[0].error is None


@pytest.mark.parametrize("name,state,_l,_i,_c,_f", JOBS, ids=IDS)
def test_a_case_held_by_another_worker_is_left_alone(
    registry: JobRegistry, conn: psycopg.Connection, name, state, _l, _i, _c, _f
) -> None:
    """Crash recovery is the lease's job: a live claim is simply invisible."""
    case_id = seed(conn, state, f"held_{name}")
    other = claim_next(conn, state, "someone-else", 300)
    assert other is not None

    spec = registry.get(name)
    called: list[int] = []
    run_per_case(
        name=spec.name, connect=connect_to(conn),
        operation=lambda _c, cid, *, fencing_token: called.append(cid),
        expected_state=spec.expected_state, worker_id=spec.name,
        lease_seconds=spec.lease_seconds, interval_seconds=spec.interval_seconds,
        should_continue=at_most(1), clock=FakeClock(),
    )

    assert called == []
    worker_id, token, _ = lease_row(conn, case_id)
    assert worker_id == "someone-else" and token == other.fencing_token


@pytest.mark.parametrize("name,state,_l,_i,_c,_f", JOBS, ids=IDS)
def test_a_stale_token_cannot_move_the_case(
    registry: JobRegistry, conn: psycopg.Connection, name, state, _l, _i, _c, _f
) -> None:
    """Fencing stays the domain's decision; the runtime has no branch for it."""
    from reclaim.domain.leases import fenced_transition

    case_id = seed(conn, state, f"stale_{name}")
    stale = lease_row(conn, case_id)[1]
    outcomes: list[bool] = []
    spec = registry.get(name)
    # A legal edge from each job's own state, so the refusal is the fencing
    # token's doing and not the state machine's.
    target = {
        CaseState.AMBIGUOUS: CaseState.RECONCILING,
        CaseState.AWAITING_CUSTOMER: CaseState.AMBIGUOUS,
    }[state]

    def write_stale(c, cid, *, fencing_token):
        outcomes.append(
            fenced_transition(
                c, cid, state, target, stale, "probe", worker_id="probe",
            )
        )

    run_per_case(
        name=spec.name, connect=connect_to(conn), operation=write_stale,
        expected_state=spec.expected_state, worker_id=spec.name,
        lease_seconds=spec.lease_seconds, interval_seconds=spec.interval_seconds,
        should_continue=at_most(1), clock=FakeClock(),
    )

    assert outcomes == [False]
    assert lease_row(conn, case_id)[2] == state.value


# ---- layering -------------------------------------------------------------


def test_the_per_case_adapters_contain_no_sql() -> None:
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[2]
        / "reclaim" / "jobs" / "percase.py"
    ).read_text()
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    for forbidden in ("conn.execute(", "SELECT", "INSERT", "UPDATE", "cursor("):
        assert forbidden not in code, f"percase.py contains {forbidden!r}"


def test_the_runtime_does_not_call_the_provider_itself(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    """Provider interaction belongs to the domain functions, not the adapter.

    The registry is built with a provider whose every attribute raises, so any
    call made by the runtime rather than passed through would fail loudly.
    """
    import reclaim.jobs.percase as percase

    captured: list = []
    monkey = percase.reconcile_case
    try:
        percase.reconcile_case = lambda conn, case_id, **kw: captured.append(
            kw["provider"]
        )
        registry.get(RECONCILER).operation(conn, 1, fencing_token=1)
    finally:
        percase.reconcile_case = monkey

    assert len(captured) == 1, "the provider was handed to the domain, not used"
