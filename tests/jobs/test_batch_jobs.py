"""The registered batch jobs.

These prove wiring, not recovery behaviour: what each job does to a case is
already pinned by the domain suites, and repeating those assertions here would
create a second, weaker oracle for the same rules.
"""

from __future__ import annotations

from contextlib import contextmanager

import psycopg
import pytest

from reclaim.config import load_operational
from reclaim.domain import (
    expire_action_deadlines,
    expire_reviews,
    expire_ttl,
    sweep_expired_leases,
)
from reclaim.jobs.jobs import (
    ACTION_DEADLINE_EXPIRY,
    REVIEW_EXPIRY,
    SWEEPER,
    TTL_EXPIRY,
    register_batch_jobs,
)
from reclaim.jobs.registry import JobKind, JobRegistry
from reclaim.jobs.runner import at_most, run_batch
from tests.db.helpers import insert_case, insert_obligation

BATCH_JOBS = [
    (SWEEPER, "sweeper_interval_seconds", sweep_expired_leases),
    (TTL_EXPIRY, "ttl_expiry_interval_seconds", expire_ttl),
    (REVIEW_EXPIRY, "review_expiry_interval_seconds", expire_reviews),
    (
        ACTION_DEADLINE_EXPIRY,
        "action_deadline_expiry_interval_seconds",
        expire_action_deadlines,
    ),
]


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


@pytest.fixture
def registry() -> JobRegistry:
    return register_batch_jobs(JobRegistry())


# ---- registration ---------------------------------------------------------


def test_exactly_the_batch_jobs_are_registered(registry: JobRegistry) -> None:
    """register_batch_jobs registers batch jobs only; per-case jobs register
    through their own entry point."""
    assert registry.names() == [
        "action-deadline-expiry",
        "breaker-monitor",
        "review-expiry",
        "sweeper",
        "ttl-expiry",
    ]


def test_the_two_expiry_sweeps_are_independently_tunable() -> None:
    """A closed payment window is not TTL exhaustion. They share a default
    cadence today, but changing one must never move the other."""
    values = load_operational()
    values["action_deadline_expiry_interval_seconds"] = 17
    registry = register_batch_jobs(JobRegistry(), config=values)

    assert registry.get(ACTION_DEADLINE_EXPIRY).interval_seconds == 17
    assert registry.get(TTL_EXPIRY).interval_seconds == int(
        load_operational()["ttl_expiry_interval_seconds"]
    )


@pytest.mark.parametrize("name,_key,_fn", BATCH_JOBS, ids=[j[0] for j in BATCH_JOBS])
def test_each_job_is_registered_as_a_batch_job(
    registry: JobRegistry, name: str, _key: str, _fn
) -> None:
    spec = registry.get(name)
    assert spec.kind is JobKind.BATCH
    assert spec.expected_state is None, "a batch job claims no single case"
    assert spec.lease_seconds is None, "the runtime holds no lease for a batch job"


@pytest.mark.parametrize("name,key,_fn", BATCH_JOBS, ids=[j[0] for j in BATCH_JOBS])
def test_each_interval_comes_from_configuration(
    registry: JobRegistry, name: str, key: str, _fn
) -> None:
    assert registry.get(name).interval_seconds == int(load_operational()[key])


@pytest.mark.parametrize("name,_key,fn", BATCH_JOBS, ids=[j[0] for j in BATCH_JOBS])
def test_each_job_invokes_the_existing_domain_callable(
    registry: JobRegistry, name: str, _key: str, fn
) -> None:
    """Identity, not a wrapper: the runtime adds no layer around the domain."""
    assert registry.get(name).operation is fn


@pytest.mark.parametrize("name", [j[0] for j in BATCH_JOBS])
def test_each_job_passes_the_configured_batch_size(
    registry: JobRegistry, name: str
) -> None:
    assert registry.get(name).limit == int(load_operational()["sweeper_batch_size"])


def test_an_edited_interval_reaches_the_registration(tmp_path) -> None:
    """Proves the value is read, not merely mirrored by a default."""
    edited = load_operational()
    edited["sweeper_interval_seconds"] = 99
    spec = register_batch_jobs(JobRegistry(), config=edited).get(SWEEPER)
    assert spec.interval_seconds == 99


# ---- execution ------------------------------------------------------------


@pytest.mark.parametrize("name", [j[0] for j in BATCH_JOBS])
def test_an_empty_batch_is_a_normal_tick(
    registry: JobRegistry, conn: psycopg.Connection, name: str
) -> None:
    """Nothing to sweep is the steady state, not a failure."""
    spec = registry.get(name)
    ticks = run_batch(
        name=spec.name,
        connect=connect_to(conn),
        operation=spec.operation,
        interval_seconds=spec.interval_seconds,
        limit=spec.limit,
        should_continue=at_most(2),
        clock=FakeClock(),
    )
    assert [t.worked for t in ticks] == [True, True]
    assert all(t.error is None for t in ticks)


@pytest.mark.parametrize("name", [j[0] for j in BATCH_JOBS])
def test_a_domain_failure_is_logged_and_the_next_tick_still_runs(
    registry: JobRegistry, conn: psycopg.Connection, name: str, caplog
) -> None:
    spec = registry.get(name)
    attempts = {"n": 0}

    def flaky(c, *, limit):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient")
        return spec.operation(c, limit=limit)

    with caplog.at_level("ERROR"):
        ticks = run_batch(
            name=spec.name, connect=connect_to(conn), operation=flaky,
            interval_seconds=spec.interval_seconds, limit=spec.limit,
            should_continue=at_most(2), clock=FakeClock(),
        )

    assert [t.worked for t in ticks] == [False, True]
    assert isinstance(ticks[0].error, RuntimeError)
    assert any(spec.name in r.message or spec.name in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("name", [j[0] for j in BATCH_JOBS])
def test_a_batch_tick_takes_no_per_case_lease(
    registry: JobRegistry, conn: psycopg.Connection, monkeypatch, name: str
) -> None:
    """The runtime must not claim: these functions do their own selection."""
    import reclaim.jobs.runner as runner

    def forbidden(*a, **k):  # pragma: no cover - the assertion is that it never runs
        raise AssertionError("a batch job must not claim a case")

    monkeypatch.setattr(runner, "claim_next", forbidden)
    monkeypatch.setattr(runner, "release_lease", forbidden)

    spec = registry.get(name)
    run_batch(
        name=spec.name, connect=connect_to(conn), operation=spec.operation,
        interval_seconds=spec.interval_seconds, limit=spec.limit,
        should_continue=at_most(1), clock=FakeClock(),
    )


@pytest.mark.parametrize("name", [j[0] for j in BATCH_JOBS])
def test_a_tick_over_an_untouched_case_changes_nothing(
    registry: JobRegistry, conn: psycopg.Connection, name: str
) -> None:
    """A case that meets no expiry condition must survive every batch job.

    What each job does to an *eligible* case is the domain suites' subject; this
    only shows the runtime adds no state change of its own.
    """
    obligation_id = insert_obligation(
        conn,
        anchor_key=f"ord_b_{name}",
        anchor_canonical=f"order:ord_b_{name}",
        source_event_id=f"evt_b_{name}",
    )
    case_id = insert_case(conn, obligation_id, state="AWAITING_CUSTOMER")
    before = conn.execute(
        "SELECT state::text, worker_id, fencing_token, attempt_count, "
        "recovered_amount_minor FROM recovery_cases WHERE id = %s",
        (case_id,),
    ).fetchone()

    spec = registry.get(name)
    run_batch(
        name=spec.name, connect=connect_to(conn), operation=spec.operation,
        interval_seconds=spec.interval_seconds, limit=spec.limit,
        should_continue=at_most(1), clock=FakeClock(),
    )

    after = conn.execute(
        "SELECT state::text, worker_id, fencing_token, attempt_count, "
        "recovered_amount_minor FROM recovery_cases WHERE id = %s",
        (case_id,),
    ).fetchone()
    assert after == before
