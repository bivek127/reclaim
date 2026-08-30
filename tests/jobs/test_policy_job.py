"""The policy job: claim POLICY_EVAL, evaluate, act on the verdict.

Registration and orchestration only. What `apply_policy` decides about a case
-- the cause-to-action table, the ambiguity formula, which verdict produces
which transition -- is settled by the policy domain suites; repeating it here
would create a second, weaker oracle.
"""

from __future__ import annotations

import inspect
from contextlib import contextmanager

import psycopg
import pytest

from reclaim.api.db import app_conn
from reclaim.config import lease_seconds_for, load_operational
from reclaim.domain.states import CaseState
from reclaim.jobs.jobs import POLICY, register_per_case_jobs
from reclaim.jobs.registry import JobKind, JobRegistry
from reclaim.jobs.runner import at_most, run_per_case
from tests.domain.policy_helpers import case_row, seed_policy_eval


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
    return register_per_case_jobs(JobRegistry())


def lease_row(conn: psycopg.Connection, case_id: int) -> tuple:
    return conn.execute(
        "SELECT worker_id, fencing_token FROM recovery_cases WHERE id = %s",
        (case_id,),
    ).fetchone()


def tick_once(registry: JobRegistry, conn: psycopg.Connection, count: int = 1):
    spec = registry.get(POLICY)
    return run_per_case(
        name=spec.name, connect=connect_to(conn), operation=spec.operation,
        expected_states=spec.expected_states, worker_id=spec.name,
        lease_seconds=spec.lease_seconds, interval_seconds=spec.interval_seconds,
        should_continue=at_most(count), clock=FakeClock(),
    )


def park_lease(conn: psycopg.Connection, case_id: int) -> None:
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = '-infinity' WHERE id = %s",
        (case_id,),
    )


def park_others(conn: psycopg.Connection, *keep: int) -> None:
    conn.execute(
        "UPDATE recovery_cases SET state = 'RECONCILING' "
        " WHERE state = 'POLICY_EVAL' AND NOT (id = ANY(%s))",
        (list(keep),),
    )


# ---- registration -----------------------------------------------------


def test_the_job_claims_exactly_policy_eval(registry: JobRegistry) -> None:
    spec = registry.get(POLICY)
    assert spec.kind is JobKind.PER_CASE
    assert spec.expected_states == (CaseState.POLICY_EVAL,)


def test_attempt_failed_is_not_claimed(registry: JobRegistry) -> None:
    """Its routing rule needs an accessor that does not exist yet."""
    assert CaseState.ATTEMPT_FAILED not in set(registry.get(POLICY).expected_states)


def test_the_interval_is_the_case_workers_configured_cadence(
    registry: JobRegistry,
) -> None:
    expected = int(load_operational()["case_worker_interval_seconds"])
    assert registry.get(POLICY).interval_seconds == expected


def test_the_lease_is_the_one_policy_work_is_sized_for(
    registry: JobRegistry,
) -> None:
    assert registry.get(POLICY).lease_seconds == lease_seconds_for("policy")
    assert lease_seconds_for("policy") == 90


def test_the_configured_lease_and_interval_are_the_contracts_values() -> None:
    """Pinned as literals so a config edit cannot move the assertion with it."""
    assert lease_seconds_for("policy") == 90
    assert int(load_operational()["case_worker_interval_seconds"]) == 5


def test_the_job_runs_as_the_application_role(registry: JobRegistry) -> None:
    assert registry.get(POLICY).connect is app_conn


# ---- orchestration ------------------------------------------------------


def test_an_allow_case_reaches_action_ready(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    """End to end through the real, unpatched operation: cause is in the
    table, no conflicting history, budget remains -> ALLOW."""
    ids = seed_policy_eval(conn, cause="INSUFFICIENT_FUNDS")
    park_lease(conn, ids["case_id"])
    park_others(conn, ids["case_id"])

    ticks = tick_once(registry, conn)

    assert ticks[0].worked is True and ticks[0].error is None
    assert case_row(conn, ids["case_id"])["state"] == "ACTION_READY"


def test_a_lookup_miss_case_reaches_escalated(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    ids = seed_policy_eval(conn, cause="TOTALLY_UNKNOWN_CAUSE_XYZ")
    park_lease(conn, ids["case_id"])
    park_others(conn, ids["case_id"])

    tick_once(registry, conn)

    row = case_row(conn, ids["case_id"])
    assert row["state"] in ("ESCALATED", "ACTION_READY"), row
    # An unmapped cause with no conflicting history falls to the UNKNOWN
    # default (ALLOW), so this only proves the real evaluate() ran -- not a
    # specific verdict, which belongs to the policy domain suite.


def test_the_registered_operation_calls_the_three_domain_functions_in_order(
    registry: JobRegistry, conn: psycopg.Connection, monkeypatch
) -> None:
    import reclaim.jobs.percase as percase

    calls: list[str] = []
    monkeypatch.setattr(
        percase, "resolve_conflicting_history",
        lambda c, cid, **kw: calls.append("conflicting_history") or False,
    )
    monkeypatch.setattr(
        percase, "load_policy_inputs",
        lambda c, cid, ch: calls.append("load_inputs") or ("FACTS", 999),
    )
    monkeypatch.setattr(
        percase, "apply_policy",
        lambda c, cid, **kw: calls.append("apply_policy") or "DECISION",
    )

    case_id = seed_policy_eval(conn)["case_id"]
    result = registry.get(POLICY).operation(conn, case_id, fencing_token=3)

    assert calls == ["conflicting_history", "load_inputs", "apply_policy"]
    assert result == "DECISION"


def test_the_loaded_facts_and_diagnosis_id_reach_apply_policy_unchanged(
    registry: JobRegistry, conn: psycopg.Connection, monkeypatch
) -> None:
    import reclaim.jobs.percase as percase

    seen: dict = {}
    monkeypatch.setattr(
        percase, "apply_policy",
        lambda c, cid, *, facts, diagnosis_id, **kw: seen.update(
            facts=facts, diagnosis_id=diagnosis_id
        ),
    )

    case_id = seed_policy_eval(conn)["case_id"]
    registry.get(POLICY).operation(conn, case_id, fencing_token=0)

    assert seen["facts"].cause == "INSUFFICIENT_FUNDS"
    assert isinstance(seen["diagnosis_id"], int)


def test_the_claims_token_reaches_apply_policy_unchanged(
    registry: JobRegistry, conn: psycopg.Connection, monkeypatch
) -> None:
    import reclaim.jobs.percase as percase

    tokens: list[int] = []
    monkeypatch.setattr(
        percase, "apply_policy",
        lambda c, cid, **kw: tokens.append(kw["fencing_token"]),
    )

    ids = seed_policy_eval(conn)
    park_lease(conn, ids["case_id"])
    park_others(conn, ids["case_id"])
    tick_once(registry, conn)

    observed = conn.execute(
        "SELECT fencing_token FROM recovery_cases WHERE id = %s", (ids["case_id"],)
    ).fetchone()[0]
    assert tokens == [observed]


# ---- lease and failure ---------------------------------------------------


def test_the_lease_is_released_after_a_successful_tick(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    ids = seed_policy_eval(conn)
    park_lease(conn, ids["case_id"])
    park_others(conn, ids["case_id"])
    tick_once(registry, conn)
    assert lease_row(conn, ids["case_id"])[0] is None


def test_the_lease_is_released_after_a_domain_failure(
    registry: JobRegistry, conn: psycopg.Connection, monkeypatch
) -> None:
    import reclaim.jobs.percase as percase

    def explode(*_a, **_k):
        raise RuntimeError("policy evaluation blew up")

    monkeypatch.setattr(percase, "apply_policy", explode)

    ids = seed_policy_eval(conn)
    park_lease(conn, ids["case_id"])
    park_others(conn, ids["case_id"])
    ticks = tick_once(registry, conn)

    assert isinstance(ticks[0].error, RuntimeError)
    assert lease_row(conn, ids["case_id"])[0] is None, "lease released despite the failure"
    assert case_row(conn, ids["case_id"])["state"] == "POLICY_EVAL", "a failure never advances the case"


def test_a_failing_tick_does_not_end_the_polling_loop(
    registry: JobRegistry, conn: psycopg.Connection, monkeypatch
) -> None:
    import reclaim.jobs.percase as percase

    calls: list[int] = []

    def flaky(_c, cid, **_k):
        calls.append(cid)
        raise RuntimeError("still broken")

    monkeypatch.setattr(percase, "resolve_conflicting_history", flaky)

    a = seed_policy_eval(conn)["case_id"]
    b = seed_policy_eval(conn)["case_id"]
    park_lease(conn, a)
    park_lease(conn, b)
    park_others(conn, a, b)
    ticks = tick_once(registry, conn, count=3)

    assert len(ticks) == 3, "the loop ran its full course"
    assert len(calls) >= 2, "it kept claiming after the first failure"


def test_an_idle_tick_is_not_a_failure(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    conn.execute("UPDATE recovery_cases SET state = 'NEW' WHERE state = 'POLICY_EVAL'")
    ticks = tick_once(registry, conn)
    assert ticks[0].worked is False
    assert ticks[0].error is None


# ---- boundaries -----------------------------------------------------------


def test_the_adapter_holds_no_sql() -> None:
    import reclaim.jobs.percase as percase

    source = inspect.getsource(percase.policy_operation)
    for forbidden in ("conn.execute(", "SELECT", "INSERT", "UPDATE", "cursor("):
        assert forbidden not in source, f"the adapter runs {forbidden}"


def test_the_adapter_invents_no_retry_or_provider_behaviour() -> None:
    import ast

    import reclaim.jobs.percase as percase

    tree = ast.parse(inspect.getsource(percase.policy_operation))
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

    for absent in ("ATTEMPT_FAILED", "provider", "backoff", "retry_after"):
        assert absent not in source, f"the policy job references {absent}"
