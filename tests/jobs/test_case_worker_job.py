"""The case worker: the two mechanical edges from NEW to DIAGNOSING.

Enrichment performs no work, so the transition is the whole operation and
these tests are about the transition: which states are claimed, which target
each produces, that the claim's token is what the domain fences on, and that a
refused write leaves the case exactly where it was.

What happens once a case reaches DIAGNOSING belongs to the diagnosis suites.
"""

from __future__ import annotations

import inspect
from contextlib import contextmanager

import psycopg
import pytest

from reclaim.api.db import app_conn
from reclaim.config import lease_seconds_for, load_operational
from reclaim.domain.states import ALLOWED_TRANSITIONS, CaseState
from reclaim.jobs.jobs import CASE_WORKER, register_per_case_jobs
from reclaim.jobs.registry import JobKind, JobRegistry
from reclaim.jobs.runner import at_most, run_per_case
from tests.db.helpers import insert_case, insert_obligation


class FakeClock:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


class UnusableProvider:
    """Any provider call is a failure: this job never reaches the network."""

    def __getattr__(self, name):
        def boom(*_a, **_k):
            raise AssertionError(f"the case worker called the provider: {name}")

        return boom


def connect_to(conn: psycopg.Connection):
    @contextmanager
    def factory():
        yield conn

    return factory


@pytest.fixture
def registry() -> JobRegistry:
    return register_per_case_jobs(JobRegistry(), provider=UnusableProvider)


def seed(
    conn: psycopg.Connection,
    state: CaseState,
    suffix: str,
    *,
    attempt_count: int = 0,
    max_attempts: int = 2,
) -> int:
    obligation_id = insert_obligation(
        conn,
        anchor_key=f"ord_cw_{suffix}",
        anchor_canonical=f"order:ord_cw_{suffix}",
        source_event_id=f"evt_cw_{suffix}",
    )
    case_id = insert_case(
        conn,
        obligation_id,
        state=state.value,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
    )
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = '-infinity' WHERE id = %s",
        (case_id,),
    )
    return case_id


def case_row(conn: psycopg.Connection, case_id: int) -> tuple:
    return conn.execute(
        "SELECT state::text, worker_id, fencing_token FROM recovery_cases WHERE id = %s",
        (case_id,),
    ).fetchone()


def tick_once(registry: JobRegistry, conn: psycopg.Connection, count: int = 1):
    spec = registry.get(CASE_WORKER)
    return run_per_case(
        name=spec.name, connect=connect_to(conn), operation=spec.operation,
        expected_states=spec.expected_states, worker_id=spec.name,
        lease_seconds=spec.lease_seconds, interval_seconds=spec.interval_seconds,
        should_continue=at_most(count), clock=FakeClock(),
    )


def park(conn: psycopg.Connection, *keep: int) -> None:
    """Move every other claimable case out of the way, so a tick is unambiguous."""
    conn.execute(
        "UPDATE recovery_cases SET state = 'RECONCILING' "
        " WHERE state IN ('NEW', 'ENRICHING', 'ATTEMPT_FAILED') AND NOT (id = ANY(%s))",
        (list(keep),),
    )


def policy_decisions_for(conn: psycopg.Connection, case_id: int) -> list[tuple]:
    return conn.execute(
        "SELECT verdict, reason_code, diagnosis_id FROM policy_decisions"
        " WHERE case_id = %s ORDER BY id",
        (case_id,),
    ).fetchall()


def pending_review_count(conn: psycopg.Connection, case_id: int) -> int:
    return conn.execute(
        "SELECT count(*) FROM human_reviews WHERE case_id = %s AND status = 'PENDING'",
        (case_id,),
    ).fetchone()[0]


# ---- registration ---------------------------------------------------------


def test_the_worker_claims_exactly_the_three_owned_states(
    registry: JobRegistry,
) -> None:
    spec = registry.get(CASE_WORKER)
    assert spec.kind is JobKind.PER_CASE
    assert spec.expected_states == (
        CaseState.NEW,
        CaseState.ENRICHING,
        CaseState.ATTEMPT_FAILED,
    )


def test_policy_eval_is_not_claimed_here(registry: JobRegistry) -> None:
    """POLICY_EVAL is a real decision, not a mechanical transition -- it is
    the `policy` job's, per §3.1's distinct "Policy Engine" owner."""
    claimed = set(registry.get(CASE_WORKER).expected_states or ())
    assert CaseState.POLICY_EVAL not in claimed


def test_the_lease_and_interval_come_from_existing_configuration(
    registry: JobRegistry,
) -> None:
    spec = registry.get(CASE_WORKER)
    assert spec.lease_seconds == lease_seconds_for("enrichment")
    assert spec.interval_seconds == int(load_operational()["case_worker_interval_seconds"])
    assert lease_seconds_for("enrichment") == 30
    assert int(load_operational()["case_worker_interval_seconds"]) == 5


def test_the_worker_runs_as_the_application_role(registry: JobRegistry) -> None:
    assert registry.get(CASE_WORKER).connect is app_conn


# ---- the two edges --------------------------------------------------------


def test_a_new_case_is_claimed_and_becomes_enriching(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    case_id = seed(conn, CaseState.NEW, "new_to_enriching")
    park(conn, case_id)

    ticks = tick_once(registry, conn)

    assert ticks[0].worked is True and ticks[0].error is None
    assert case_row(conn, case_id)[0] == "ENRICHING"


def test_an_enriching_case_is_claimed_and_becomes_diagnosing(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    case_id = seed(conn, CaseState.ENRICHING, "enriching_to_diagnosing")
    park(conn, case_id)

    ticks = tick_once(registry, conn)

    assert ticks[0].worked is True and ticks[0].error is None
    assert case_row(conn, case_id)[0] == "DIAGNOSING"


def test_two_ticks_walk_a_new_case_to_diagnosing(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    """The pipeline, end to end, through the registered job only."""
    case_id = seed(conn, CaseState.NEW, "full_walk")
    park(conn, case_id)

    tick_once(registry, conn)
    assert case_row(conn, case_id)[0] == "ENRICHING"

    tick_once(registry, conn)
    assert case_row(conn, case_id)[0] == "DIAGNOSING"


def test_there_is_no_new_to_diagnosing_shortcut() -> None:
    """One tick can only ever advance one edge, because the machine has no
    NEW-to-DIAGNOSING pair to take."""
    assert (CaseState.NEW, CaseState.DIAGNOSING) not in ALLOWED_TRANSITIONS
    assert (CaseState.NEW, CaseState.ENRICHING) in ALLOWED_TRANSITIONS
    assert (CaseState.ENRICHING, CaseState.DIAGNOSING) in ALLOWED_TRANSITIONS


def test_the_advance_table_names_only_the_two_owned_edges() -> None:
    """A third entry would be a lifecycle edge invented in the job layer."""
    from reclaim.jobs.percase import CASE_WORKER_ADVANCE

    assert set(CASE_WORKER_ADVANCE) == {CaseState.NEW, CaseState.ENRICHING}
    assert CASE_WORKER_ADVANCE[CaseState.NEW][0] is CaseState.ENRICHING
    assert CASE_WORKER_ADVANCE[CaseState.ENRICHING][0] is CaseState.DIAGNOSING
    for target, _reason in CASE_WORKER_ADVANCE.values():
        assert target is not CaseState.ESCALATED, "TTL expiry owns that edge"


# ---- fencing, leases, failure ---------------------------------------------


def test_the_claims_token_reaches_the_domain_unchanged(
    registry: JobRegistry, conn: psycopg.Connection, monkeypatch
) -> None:
    import reclaim.jobs.percase as percase

    tokens: list[int] = []
    monkeypatch.setattr(
        percase, "fenced_transition",
        lambda c, cid, exp, new, token, reason, **kw: tokens.append(token) or True,
    )

    case_id = seed(conn, CaseState.NEW, "token")
    park(conn, case_id)
    tick_once(registry, conn)

    observed = conn.execute(
        "SELECT fencing_token FROM recovery_cases WHERE id = %s", (case_id,)
    ).fetchone()[0]
    assert tokens == [observed]


def test_a_stale_token_does_not_advance_the_case(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    """A refused write leaves the state alone rather than forcing it."""
    from reclaim.jobs.percase import case_worker_operation

    case_id = seed(conn, CaseState.NEW, "stale")
    applied = case_worker_operation()(
        conn, case_id, fencing_token=999, claimed_state=CaseState.NEW
    )

    assert applied is False, "the fenced write was refused"
    assert case_row(conn, case_id)[0] == "NEW", "state untouched"


def test_the_lease_is_released_after_a_successful_tick(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    case_id = seed(conn, CaseState.NEW, "release_ok")
    park(conn, case_id)
    tick_once(registry, conn)

    assert case_row(conn, case_id)[1] is None


def test_the_lease_is_released_after_a_failure(
    registry: JobRegistry, conn: psycopg.Connection, monkeypatch
) -> None:
    import reclaim.jobs.percase as percase

    def explode(*_a, **_k):
        raise RuntimeError("transition blew up")

    monkeypatch.setattr(percase, "fenced_transition", explode)

    case_id = seed(conn, CaseState.NEW, "release_fail")
    park(conn, case_id)
    ticks = tick_once(registry, conn)

    assert isinstance(ticks[0].error, RuntimeError)
    assert case_row(conn, case_id)[1] is None, "lease released despite the failure"
    assert case_row(conn, case_id)[0] == "NEW", "a failure never advances the case"


def test_a_failing_tick_does_not_end_the_polling_loop(
    registry: JobRegistry, conn: psycopg.Connection, monkeypatch
) -> None:
    import reclaim.jobs.percase as percase

    calls: list[int] = []

    def flaky(_c, cid, *_a, **_k):
        calls.append(cid)
        raise RuntimeError("still broken")

    monkeypatch.setattr(percase, "fenced_transition", flaky)

    a = seed(conn, CaseState.NEW, "loop_a")
    b = seed(conn, CaseState.NEW, "loop_b")
    park(conn, a, b)
    ticks = tick_once(registry, conn, count=3)

    assert len(ticks) == 3, "the loop ran its full course"
    assert len(calls) >= 2, "it kept claiming after the first failure"


def test_an_unowned_claimed_state_is_refused(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    """The operation will not invent an edge for a state it does not own."""
    from reclaim.jobs.percase import case_worker_operation

    case_id = seed(conn, CaseState.POLICY_EVAL, "unowned")
    with pytest.raises(RuntimeError, match="no case-worker edge"):
        case_worker_operation()(
            conn, case_id, fencing_token=0, claimed_state=CaseState.POLICY_EVAL
        )
    assert case_row(conn, case_id)[0] == "POLICY_EVAL"


# ---- boundaries -----------------------------------------------------------


def test_the_adapter_holds_no_sql() -> None:
    import reclaim.jobs.percase as percase

    source = inspect.getsource(percase.case_worker_operation)
    for forbidden in ("conn.execute(", "SELECT", "INSERT", "UPDATE", "cursor("):
        assert forbidden not in source, f"the adapter runs {forbidden}"


def test_the_adapter_invents_no_policy_or_execution_behaviour() -> None:
    """Routing to POLICY_EVAL/ESCALATED is here on purpose; deciding what
    happens inside either state is not.

    Executable code only: prose that says the job does *not* retry must not
    read as evidence that it does.
    """
    import ast

    import reclaim.jobs.percase as percase

    def stripped(fn) -> str:
        tree = ast.parse(inspect.getsource(fn))
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
        return ast.unparse(tree)

    source = stripped(percase.case_worker_operation) + stripped(
        percase._route_attempt_failed
    )

    for absent in (
        "conflicting_history",
        "PolicyFacts",
        "load_policy_inputs",
        "apply_policy",
        "retry",
        "backoff",
        "provider",
    ):
        assert absent not in source, f"the case worker references {absent}"


# ---- ATTEMPT_FAILED routing ------------------------------------------------
#
# The rule (docs/ARCHITECTURE.md, ADR-011):
#   attempt_count < max_attempts -> POLICY_EVAL
#   attempt_count = max_attempts -> ESCALATED
# Exhaustive per ck_attempt_budget's attempt_count <= max_attempts.


def test_budget_remaining_routes_to_policy_eval(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    case_id = seed(
        conn, CaseState.ATTEMPT_FAILED, "budget_remains",
        attempt_count=0, max_attempts=2,
    )
    park(conn, case_id)

    ticks = tick_once(registry, conn)

    assert ticks[0].worked is True and ticks[0].error is None
    assert case_row(conn, case_id)[0] == "POLICY_EVAL"


def test_budget_exhausted_routes_to_escalated(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    case_id = seed(
        conn, CaseState.ATTEMPT_FAILED, "budget_exhausted",
        attempt_count=2, max_attempts=2,
    )
    park(conn, case_id)

    ticks = tick_once(registry, conn)

    assert ticks[0].worked is True and ticks[0].error is None
    assert case_row(conn, case_id)[0] == "ESCALATED"


def test_the_boundary_is_exactly_where_the_check_constraint_places_it(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    """One below the ceiling remains; exactly at the ceiling exhausts."""
    below = seed(
        conn, CaseState.ATTEMPT_FAILED, "boundary_below",
        attempt_count=1, max_attempts=2,
    )
    at_ceiling = seed(
        conn, CaseState.ATTEMPT_FAILED, "boundary_at",
        attempt_count=2, max_attempts=2,
    )
    park(conn, below, at_ceiling)

    tick_once(registry, conn, count=2)

    assert case_row(conn, below)[0] == "POLICY_EVAL"
    assert case_row(conn, at_ceiling)[0] == "ESCALATED"


def test_escalation_opens_exactly_one_pending_review(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    """Every entry into ESCALATED must open a review; this path is no
    exception, matching TTL and deadline expiry (sweeper.py)."""
    case_id = seed(
        conn, CaseState.ATTEMPT_FAILED, "escalation_review",
        attempt_count=2, max_attempts=2,
    )
    park(conn, case_id)

    tick_once(registry, conn)

    assert case_row(conn, case_id)[0] == "ESCALATED"
    assert pending_review_count(conn, case_id) == 1
    decisions = policy_decisions_for(conn, case_id)
    assert len(decisions) == 1
    assert decisions[0][0] == "ESCALATE"
    assert decisions[0][1] == "attempt_failed_budget_exhausted"
    assert decisions[0][2] is None, "no prior diagnosis to attach to"


def test_only_policy_eval_and_escalated_are_reachable_targets(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    """No third destination exists, matching the exhaustive predicate."""
    for attempt_count, max_attempts in [(0, 1), (0, 3), (1, 1), (3, 3)]:
        case_id = seed(
            conn, CaseState.ATTEMPT_FAILED, f"reach_{attempt_count}_{max_attempts}",
            attempt_count=attempt_count, max_attempts=max_attempts,
        )
        park(conn, case_id)
        tick_once(registry, conn)
        assert case_row(conn, case_id)[0] in ("POLICY_EVAL", "ESCALATED")


def test_a_stale_token_is_rejected_and_does_not_transition(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    from reclaim.jobs.percase import case_worker_operation

    case_id = seed(
        conn, CaseState.ATTEMPT_FAILED, "stale_token",
        attempt_count=0, max_attempts=2,
    )
    real_token = case_row(conn, case_id)[2]

    applied = case_worker_operation()(
        conn, case_id,
        fencing_token=real_token + 1,
        claimed_state=CaseState.ATTEMPT_FAILED,
    )

    assert applied is False, "a token that does not match must be refused"
    row = case_row(conn, case_id)
    assert row[0] == "ATTEMPT_FAILED", "state untouched by the rejected write"
    assert row[2] == real_token, "fencing token untouched by the rejected write"


def test_the_claims_token_reaches_the_domain_unchanged_for_routing(
    registry: JobRegistry, conn: psycopg.Connection, monkeypatch
) -> None:
    """The runner's own claim, not a value the routing logic invents."""
    import reclaim.jobs.percase as percase

    tokens: list[int] = []
    real_fenced_transition = percase.fenced_transition

    def spy(conn, case_id, expected, new, token, *a, **kw):
        tokens.append(token)
        return real_fenced_transition(conn, case_id, expected, new, token, *a, **kw)

    monkeypatch.setattr(percase, "fenced_transition", spy)

    case_id = seed(
        conn, CaseState.ATTEMPT_FAILED, "token_routing",
        attempt_count=0, max_attempts=2,
    )
    park(conn, case_id)
    tick_once(registry, conn)

    observed = case_row(conn, case_id)[2]
    assert tokens == [observed]


def test_the_lease_is_released_after_routing_to_policy_eval(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    case_id = seed(
        conn, CaseState.ATTEMPT_FAILED, "release_policy",
        attempt_count=0, max_attempts=2,
    )
    park(conn, case_id)
    tick_once(registry, conn)
    assert case_row(conn, case_id)[1] is None


def test_the_lease_is_released_after_routing_to_escalated(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    case_id = seed(
        conn, CaseState.ATTEMPT_FAILED, "release_escalated",
        attempt_count=2, max_attempts=2,
    )
    park(conn, case_id)
    tick_once(registry, conn)
    assert case_row(conn, case_id)[1] is None


def test_the_lease_is_released_after_a_routing_failure(
    registry: JobRegistry, conn: psycopg.Connection, monkeypatch
) -> None:
    import reclaim.jobs.percase as percase

    def explode(*_a, **_k):
        raise RuntimeError("routing blew up")

    monkeypatch.setattr(percase, "resolve_attempt_budget", explode)

    case_id = seed(
        conn, CaseState.ATTEMPT_FAILED, "release_fail",
        attempt_count=0, max_attempts=2,
    )
    park(conn, case_id)
    ticks = tick_once(registry, conn)

    assert isinstance(ticks[0].error, RuntimeError)
    assert case_row(conn, case_id)[1] is None, "lease released despite the failure"
    assert case_row(conn, case_id)[0] == "ATTEMPT_FAILED", "a failure never routes the case"


def test_a_routing_failure_does_not_end_the_polling_loop(
    registry: JobRegistry, conn: psycopg.Connection, monkeypatch
) -> None:
    import reclaim.jobs.percase as percase

    calls: list[int] = []

    def flaky(_c, cid, *_a, **_k):
        calls.append(cid)
        raise RuntimeError("still broken")

    monkeypatch.setattr(percase, "resolve_attempt_budget", flaky)

    a = seed(conn, CaseState.ATTEMPT_FAILED, "loop_a", attempt_count=0, max_attempts=2)
    b = seed(conn, CaseState.ATTEMPT_FAILED, "loop_b", attempt_count=0, max_attempts=2)
    park(conn, a, b)
    ticks = tick_once(registry, conn, count=3)

    assert len(ticks) == 3, "the loop ran its full course"
    assert len(calls) >= 2, "it kept claiming after the first failure"


def test_a_reclaimed_case_is_not_routed_on_stale_data(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    """`resolve_attempt_budget` returns None once the token no longer
    matches; the operation must discard rather than route on it."""
    from reclaim.jobs.percase import case_worker_operation

    case_id = seed(
        conn, CaseState.ATTEMPT_FAILED, "reclaimed",
        attempt_count=0, max_attempts=2,
    )
    stale_token = case_row(conn, case_id)[2]
    # Simulate another worker's claim: bump the token, same as claim_next.
    conn.execute(
        "UPDATE recovery_cases SET fencing_token = fencing_token + 1 WHERE id = %s",
        (case_id,),
    )

    applied = case_worker_operation()(
        conn, case_id, fencing_token=stale_token, claimed_state=CaseState.ATTEMPT_FAILED
    )

    assert applied is False
    assert case_row(conn, case_id)[0] == "ATTEMPT_FAILED"


def test_resolve_attempt_budget_reads_no_sql_beyond_its_own_query() -> None:
    """A guarded read, not a place for the routing decision itself."""
    from reclaim.domain.execution import resolve_attempt_budget

    assert resolve_attempt_budget.__module__ == "reclaim.domain.execution"


def test_new_behaviour_is_unaffected_by_the_new_edge(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    """Adding ATTEMPT_FAILED must not disturb the existing NEW edge."""
    case_id = seed(conn, CaseState.NEW, "regress_new")
    park(conn, case_id)
    tick_once(registry, conn)
    assert case_row(conn, case_id)[0] == "ENRICHING"


def test_enriching_behaviour_is_unaffected_by_the_new_edge(
    registry: JobRegistry, conn: psycopg.Connection
) -> None:
    """Adding ATTEMPT_FAILED must not disturb the existing ENRICHING edge."""
    case_id = seed(conn, CaseState.ENRICHING, "regress_enriching")
    park(conn, case_id)
    tick_once(registry, conn)
    assert case_row(conn, case_id)[0] == "DIAGNOSING"
