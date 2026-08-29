"""The diagnosis job: the one case-worker leg whose contract is settled.

Registration and orchestration only. What `diagnose_case` decides about a case
-- the retry ladder, the deterministic fallback, the shape of the `diagnoses`
row -- is settled by the diagnosis domain suite; asserting it again here would
create a second, weaker oracle.
"""

from __future__ import annotations

from contextlib import contextmanager

import psycopg
import pytest

from reclaim.api.db import app_conn
from reclaim.config import lease_seconds_for, load_operational
from reclaim.domain.diagnosis import diagnose_case
from reclaim.domain.states import CaseState
from reclaim.jobs.jobs import DIAGNOSIS, register_per_case_jobs
from reclaim.jobs.registry import JobKind, JobRegistry
from reclaim.jobs.runner import at_most, run_per_case
from tests.db.helpers import insert_case, insert_obligation


class FakeClock:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


class UnusableLlm:
    """Any model call is a failure: these tests never reach Ollama."""

    def complete(self, *_a, **_k):
        raise AssertionError("the runtime called the model")


def connect_to(conn: psycopg.Connection):
    @contextmanager
    def factory():
        yield conn

    return factory


@pytest.fixture
def registry() -> JobRegistry:
    return register_per_case_jobs(JobRegistry(), llm=UnusableLlm)


def seed_diagnosing(conn: psycopg.Connection, suffix: str) -> int:
    obligation_id = insert_obligation(
        conn,
        anchor_key=f"ord_dx_{suffix}",
        anchor_canonical=f"order:ord_dx_{suffix}",
        source_event_id=f"evt_dx_{suffix}",
    )
    case_id = insert_case(conn, obligation_id, state=CaseState.DIAGNOSING.value)
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


def test_the_job_claims_diagnosing_and_nothing_else(registry: JobRegistry) -> None:
    """The undefined legs of the case worker must not be claimed here."""
    spec = registry.get(DIAGNOSIS)
    assert spec.kind is JobKind.PER_CASE
    assert spec.expected_states == (CaseState.DIAGNOSING,)


def test_the_interval_is_the_case_workers_configured_cadence(
    registry: JobRegistry,
) -> None:
    """Read from configuration, not restated in the registration."""
    expected = int(load_operational()["case_worker_interval_seconds"])
    assert registry.get(DIAGNOSIS).interval_seconds == expected


def test_the_lease_is_the_one_diagnosis_work_is_sized_for(
    registry: JobRegistry,
) -> None:
    """A model call needs the long lease, not the default one."""
    assert registry.get(DIAGNOSIS).lease_seconds == lease_seconds_for("diagnosis")
    assert lease_seconds_for("diagnosis") == 90


def test_the_configured_lease_and_interval_are_the_contracts_values() -> None:
    """Pins the numbers themselves, so a config edit cannot move the assertion
    and the expectation together."""
    assert lease_seconds_for("diagnosis") == 90
    assert int(load_operational()["case_worker_interval_seconds"]) == 5


def test_the_job_runs_as_the_application_role(registry: JobRegistry) -> None:
    """Diagnosis writes no revenue, so it does not need the verifier role."""
    assert registry.get(DIAGNOSIS).connect is app_conn


# ---- orchestration --------------------------------------------------------


def test_the_registered_operation_calls_the_existing_domain_function(
    registry: JobRegistry, conn: psycopg.Connection, monkeypatch
) -> None:
    """The job drives `diagnose_case`; it does not re-implement diagnosis."""
    import reclaim.jobs.percase as percase

    seen: list[dict] = []
    monkeypatch.setattr(
        percase, "diagnose_case", lambda c, cid, **kw: seen.append({"case": cid, **kw})
    )

    case_id = seed_diagnosing(conn, "calls_domain")
    registry.get(DIAGNOSIS).operation(conn, case_id, fencing_token=3)

    assert len(seen) == 1
    assert seen[0]["case"] == case_id
    assert seen[0]["worker_id"] == "diagnosis"


def test_the_claims_token_reaches_the_domain_unchanged(
    registry: JobRegistry, conn: psycopg.Connection, monkeypatch
) -> None:
    """The runner claims; the token it observed is what the domain fences on."""
    import reclaim.jobs.percase as percase

    tokens: list[int] = []
    monkeypatch.setattr(
        percase, "diagnose_case", lambda c, cid, **kw: tokens.append(kw["fencing_token"])
    )

    case_id = seed_diagnosing(conn, "token")
    spec = registry.get(DIAGNOSIS)
    run_per_case(
        name=spec.name, connect=connect_to(conn), operation=spec.operation,
        expected_states=spec.expected_states, worker_id=spec.name,
        lease_seconds=spec.lease_seconds, interval_seconds=spec.interval_seconds,
        should_continue=at_most(1), clock=FakeClock(),
    )

    observed = conn.execute(
        "SELECT fencing_token FROM recovery_cases WHERE id = %s", (case_id,)
    ).fetchone()[0]
    assert tokens == [observed]


def test_the_lease_is_released_after_a_successful_tick(
    registry: JobRegistry, conn: psycopg.Connection, monkeypatch
) -> None:
    import reclaim.jobs.percase as percase

    monkeypatch.setattr(percase, "diagnose_case", lambda c, cid, **kw: "diagnosed")

    case_id = seed_diagnosing(conn, "release_ok")
    spec = registry.get(DIAGNOSIS)
    ticks = run_per_case(
        name=spec.name, connect=connect_to(conn), operation=spec.operation,
        expected_states=spec.expected_states, worker_id=spec.name,
        lease_seconds=spec.lease_seconds, interval_seconds=spec.interval_seconds,
        should_continue=at_most(1), clock=FakeClock(),
    )

    assert ticks[0].result == "diagnosed"
    assert lease_row(conn, case_id)[0] is None


def test_the_lease_is_released_after_a_domain_failure(
    registry: JobRegistry, conn: psycopg.Connection, monkeypatch
) -> None:
    """A raising model or database error must not strand the case."""
    import reclaim.jobs.percase as percase

    def explode(*_a, **_k):
        raise RuntimeError("diagnosis blew up")

    monkeypatch.setattr(percase, "diagnose_case", explode)

    case_id = seed_diagnosing(conn, "release_fail")
    spec = registry.get(DIAGNOSIS)
    ticks = run_per_case(
        name=spec.name, connect=connect_to(conn), operation=spec.operation,
        expected_states=spec.expected_states, worker_id=spec.name,
        lease_seconds=spec.lease_seconds, interval_seconds=spec.interval_seconds,
        should_continue=at_most(1), clock=FakeClock(),
    )

    assert isinstance(ticks[0].error, RuntimeError)
    assert lease_row(conn, case_id)[0] is None, "lease released despite the failure"
    assert lease_row(conn, case_id)[2] == "DIAGNOSING", "state left to the domain"


def test_a_failing_tick_does_not_end_the_polling_loop(
    registry: JobRegistry, conn: psycopg.Connection, monkeypatch
) -> None:
    import reclaim.jobs.percase as percase

    calls: list[int] = []

    def flaky(_c, cid, **_k):
        calls.append(cid)
        raise RuntimeError("still broken")

    monkeypatch.setattr(percase, "diagnose_case", flaky)

    seed_diagnosing(conn, "loop_a")
    seed_diagnosing(conn, "loop_b")
    spec = registry.get(DIAGNOSIS)
    ticks = run_per_case(
        name=spec.name, connect=connect_to(conn), operation=spec.operation,
        expected_states=spec.expected_states, worker_id=spec.name,
        lease_seconds=spec.lease_seconds, interval_seconds=spec.interval_seconds,
        should_continue=at_most(3), clock=FakeClock(),
    )

    assert len(ticks) == 3, "the loop ran its full course"
    assert len(calls) >= 2, "it kept claiming after the first failure"


def test_an_idle_tick_is_not_a_failure(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    """Nothing in DIAGNOSING is a quiet tick, not an error."""
    conn.execute("UPDATE recovery_cases SET state = 'NEW' WHERE state = 'DIAGNOSING'")
    spec = registry.get(DIAGNOSIS)
    ticks = run_per_case(
        name=spec.name, connect=connect_to(conn), operation=spec.operation,
        expected_states=spec.expected_states, worker_id=spec.name,
        lease_seconds=spec.lease_seconds, interval_seconds=spec.interval_seconds,
        should_continue=at_most(1), clock=FakeClock(),
    )
    assert ticks[0].worked is False
    assert ticks[0].error is None


def test_the_adapter_holds_no_sql() -> None:
    """Every write belongs to `diagnose_case`."""
    import inspect

    import reclaim.jobs.percase as percase

    source = inspect.getsource(percase.diagnosis_operation)
    for forbidden in ("conn.execute(", "SELECT", "INSERT", "UPDATE", "cursor("):
        assert forbidden not in source, f"the adapter runs {forbidden}"


def test_the_model_is_built_lazily_not_at_registration() -> None:
    """A model that is down must not stop the job table from loading."""
    from reclaim.jobs.percase import diagnosis_operation

    built: list[int] = []

    def factory():
        built.append(1)
        return UnusableLlm()

    diagnosis_operation(llm=factory)
    assert built == [], "registration constructed a client"
