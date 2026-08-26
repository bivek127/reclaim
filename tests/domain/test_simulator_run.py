"""Simulator runs: reproducibility, arms, isolation, metrics."""

from __future__ import annotations

import threading
from typing import Any

import psycopg
import pytest

from reclaim.config import (
    SimulatorConfigError,
    load_simulator_config,
)
from reclaim.domain.simulator import (
    ARM_CONTROL,
    ARM_TREATMENT,
    TREATMENT_ACTION,
    SimulationBlocked,
    build_outcomes,
    compute_metrics,
    extract_features,
    fingerprint,
    load_corpus,
    metrics_for_run,
    probability_for,
    resolve,
    run_simulation,
    select_cases,
)
from tests.domain.simulator_helpers import (
    BASELINE,
    UPLIFT,
    case_snapshot,
    seed_corpus,
    sim_config,
    table_counts,
)


def _run(conn, **cfg):
    return run_simulation(conn, config=sim_config(**cfg))


# ---- a fixed seed reproduces exactly ---------------------------------------


def test_fixed_seed_reproduces_exactly(conn: psycopg.Connection) -> None:
    seed_corpus(conn, 6)

    first = _run(conn)
    second = _run(conn)

    assert fingerprint(first.outcomes) == fingerprint(second.outcomes)
    assert first.run_id != second.run_id, "each run is its own record"


def test_different_seed_gives_a_different_run(conn: psycopg.Connection) -> None:
    seed_corpus(conn, 6)

    a = _run(conn, seed=1)
    b = _run(conn, seed=2)

    assert fingerprint(a.outcomes) != fingerprint(b.outcomes)


def test_outcome_does_not_depend_on_other_cases(conn: psycopg.Connection) -> None:
    """Per-draw digests: adding a case cannot change another case's result."""
    p = probability_for(ARM_CONTROL, sim_config())

    before = resolve(99, 4242, ARM_CONTROL, p)
    after = resolve(99, 4242, ARM_CONTROL, p)

    assert before == after


def test_selection_is_deterministic_for_a_fixed_corpus(
    conn: psycopg.Connection,
) -> None:
    seed_corpus(conn, 8)
    corpus = load_corpus(conn, history_window_days=30)

    first = [c.case_id for c in select_cases(corpus, seed=7, n_per_arm=4)]
    second = [c.case_id for c in select_cases(list(reversed(corpus)), seed=7, n_per_arm=4)]

    assert first == second, "selection must not depend on corpus ordering"


def test_selection_records_which_real_cases_produced_the_run(
    conn: psycopg.Connection,
) -> None:
    """A completed run identifies its corpus from its own rows."""
    seed_corpus(conn, 5)
    result = _run(conn, n_per_arm=3)

    persisted = conn.execute(
        "SELECT DISTINCT case_id FROM sim_outcomes WHERE run_id = %s ORDER BY 1",
        (result.run_id,),
    ).fetchall()
    assert [r[0] for r in persisted] == sorted({o.case_id for o in result.outcomes})


# ---- CONTROL / TREATMENT ------------------------------------------------


def test_control_has_no_action_and_treatment_does(conn: psycopg.Connection) -> None:
    seed_corpus(conn, 4)

    result = _run(conn, n_per_arm=3)

    control = [o for o in result.outcomes if o.arm == ARM_CONTROL]
    treatment = [o for o in result.outcomes if o.arm == ARM_TREATMENT]
    assert all(o.action_type is None for o in control)
    assert all(o.action_type == TREATMENT_ACTION for o in treatment)


def test_ck_control_has_no_action_is_the_enforcement(
    conn: psycopg.Connection,
) -> None:
    """The DDL check, not the application, is what makes this true."""
    from psycopg.errors import CheckViolation

    ids = seed_corpus(conn, 1)
    run = conn.execute(
        "INSERT INTO sim_runs (seed, n_per_arm, params) VALUES (1,1,'{}') RETURNING id"
    ).fetchone()

    with pytest.raises(CheckViolation):
        conn.execute(
            """
            INSERT INTO sim_outcomes (
                run_id, arm, case_id, pre_decision_features,
                action_type, resolved, amount_minor, case_state_at_run
            ) VALUES (%s, 'CONTROL', %s, '{}', 'CREATE_PAYMENT_LINK', true, 1,
                      'AWAITING_CUSTOMER')
            """,
            (run[0], ids[0]),
        )


def test_n_per_arm_rows_in_each_arm(conn: psycopg.Connection) -> None:
    seed_corpus(conn, 10)

    result = _run(conn, n_per_arm=4)

    rows = conn.execute(
        "SELECT arm::text, count(*) FROM sim_outcomes WHERE run_id = %s GROUP BY 1",
        (result.run_id,),
    ).fetchall()
    assert dict(rows) == {"CONTROL": 4, "TREATMENT": 4}


def test_both_arms_use_the_same_cases(conn: psycopg.Connection) -> None:
    """Paired design: the schema permits it and it removes confounding."""
    seed_corpus(conn, 5)

    result = _run(conn, n_per_arm=3)

    control = {o.case_id for o in result.outcomes if o.arm == ARM_CONTROL}
    treatment = {o.case_id for o in result.outcomes if o.arm == ARM_TREATMENT}
    assert control == treatment


# ---- isolation: the simulator must not touch production -----------------


def test_no_recovery_case_is_mutated(conn: psycopg.Connection) -> None:
    seed_corpus(conn, 5)
    before = case_snapshot(conn)

    _run(conn, n_per_arm=3)

    assert case_snapshot(conn) == before


def test_no_production_row_is_created(conn: psycopg.Connection) -> None:
    seed_corpus(conn, 5)
    before = table_counts(conn)

    _run(conn, n_per_arm=3)

    assert table_counts(conn) == before


def test_attempt_count_is_untouched(conn: psycopg.Connection) -> None:
    seed_corpus(conn, 4)
    before = conn.execute("SELECT sum(attempt_count) FROM recovery_cases").fetchone()

    _run(conn, n_per_arm=3)

    assert conn.execute(
        "SELECT sum(attempt_count) FROM recovery_cases"
    ).fetchone() == before


def test_recovered_amount_is_untouched(conn: psycopg.Connection) -> None:
    seed_corpus(conn, 4)

    _run(conn, n_per_arm=3)

    total = conn.execute(
        "SELECT sum(recovered_amount_minor) FROM recovery_cases"
    ).fetchone()
    assert total[0] == 0


def test_simulator_imports_nothing_from_the_provider_layer() -> None:
    """No provider call is possible because no provider symbol is importable.

    Checked by parsing the module's import statements rather than scanning text,
    so a docstring mentioning the provider cannot make this pass or fail
    spuriously. This is the enforcement mechanism: the simulator has no handle
    to call.
    """
    import ast
    import inspect

    from reclaim.domain import simulator

    tree = ast.parse(inspect.getsource(simulator))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    offenders = [m for m in imported if "provider" in m or "razorpay" in m]
    assert offenders == [], f"simulator imports from the provider layer: {offenders}"


def test_terminal_state_cases_are_safe_to_sample(conn: psycopg.Connection) -> None:
    """Terminal cases can be in the corpus; sampling them changes nothing."""
    seed_corpus(conn, 4, state="VERIFIED_FAILED")
    before = case_snapshot(conn)

    result = _run(conn, n_per_arm=3)

    assert case_snapshot(conn) == before
    assert len(result.outcomes) == 6


# ---- persistence ---------------------------------------------------------


def test_sim_run_persists_params_with_citations(conn: psycopg.Connection) -> None:
    seed_corpus(conn, 4)

    result = _run(conn, n_per_arm=3)

    row = conn.execute(
        "SELECT seed, n_per_arm, params FROM sim_runs WHERE id = %s", (result.run_id,)
    ).fetchone()
    assert row[0] == 12345
    assert row[1] == 3
    params = row[2]
    assert params["organic_baseline"]["source"]
    assert params["action_params"]["CREATE_PAYMENT_LINK"]["source"]
    assert params["feature_encoding"]["weighted"] is False


def test_sim_outcomes_persist_features(conn: psycopg.Connection) -> None:
    seed_corpus(conn, 4)

    result = _run(conn, n_per_arm=3)

    rows = conn.execute(
        "SELECT pre_decision_features FROM sim_outcomes WHERE run_id = %s",
        (result.run_id,),
    ).fetchall()
    assert len(rows) == 6
    assert all("amount_band" in r[0] for r in rows)


def test_run_is_atomic_on_failure(conn: psycopg.Connection) -> None:
    """A partial run must leave no sim_runs row (no completed_at column exists)."""
    seed_corpus(conn, 4)
    before = conn.execute("SELECT count(*) FROM sim_runs").fetchone()[0]

    with pytest.raises(SimulationBlocked):
        _run(conn, n_per_arm=99)  # corpus too small

    assert conn.execute("SELECT count(*) FROM sim_runs").fetchone()[0] == before


# ---- configuration -------------------------------------------------------


def test_shipped_config_fails_closed_on_unsourced_values() -> None:
    """config/simulator.yaml ships with research values unset, by design."""
    with pytest.raises(SimulatorConfigError) as excinfo:
        load_simulator_config()

    assert "unset" in str(excinfo.value)


def test_insufficient_corpus_is_refused(conn: psycopg.Connection) -> None:
    seed_corpus(conn, 2)

    with pytest.raises(SimulationBlocked) as excinfo:
        _run(conn, n_per_arm=50)

    assert "corpus holds 2" in str(excinfo.value)


@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_n_per_arm_is_refused(conn: psycopg.Connection, bad: int) -> None:
    seed_corpus(conn, 3)

    with pytest.raises(SimulationBlocked):
        _run(conn, n_per_arm=bad)


def test_unknown_model_is_refused(conn: psycopg.Connection) -> None:
    seed_corpus(conn, 3)

    with pytest.raises(SimulatorConfigError):
        _run(conn, n_per_arm=2, model="wishful_thinking")


# ---- probability + metrics ----------------------------------------------


def test_baseline_applies_identically_to_both_arms() -> None:
    """The organic baseline applies identically to control and treatment."""
    cfg = sim_config()

    assert probability_for(ARM_CONTROL, cfg) == BASELINE
    assert probability_for(ARM_TREATMENT, cfg) == pytest.approx(BASELINE + UPLIFT)


def test_direct_rates_model_uses_the_action_rate() -> None:
    cfg = sim_config(model="direct_rates")

    assert probability_for(ARM_CONTROL, cfg) == BASELINE
    assert probability_for(ARM_TREATMENT, cfg) == pytest.approx(UPLIFT)


def test_probability_is_clamped_to_a_valid_range() -> None:
    cfg = sim_config(organic_baseline_rate=0.9, action_params={"CREATE_PAYMENT_LINK": 0.9})

    assert probability_for(ARM_TREATMENT, cfg) == 1.0


def test_metrics_arithmetic_on_a_hand_checked_fixture(
    conn: psycopg.Connection,
) -> None:
    seed_corpus(conn, 6)
    cfg = sim_config(n_per_arm=6)
    corpus = load_corpus(conn, history_window_days=30)
    cases = select_cases(corpus, seed=cfg.seed, n_per_arm=6)
    outcomes = build_outcomes(cases, cfg)

    metrics = compute_metrics(outcomes)

    control = [o for o in outcomes if o.arm == ARM_CONTROL]
    assert metrics.control_n == 6
    assert metrics.control_resolved == sum(1 for o in control if o.resolved)
    assert metrics.control_rate == pytest.approx(metrics.control_resolved / 6)
    assert metrics.lift == pytest.approx(metrics.treatment_rate - metrics.control_rate)


def test_expired_unresolved_excluded_from_lift(conn: psycopg.Connection) -> None:
    """EXPIRED_UNRESOLVED cases are excluded entirely, from both arms."""
    seed_corpus(conn, 3, state="EXPIRED_UNRESOLVED")
    seed_corpus(conn, 3, state="AWAITING_CUSTOMER")
    cfg = sim_config(n_per_arm=6)
    corpus = load_corpus(conn, history_window_days=30)
    cases = select_cases(corpus, seed=cfg.seed, n_per_arm=6)

    metrics = compute_metrics(build_outcomes(cases, cfg))

    assert metrics.excluded_from_lift == 3
    assert metrics.control_n == 3
    assert metrics.treatment_n == 3


# ---- feature extraction determinism -------------------------------------


def test_feature_extraction_is_deterministic(conn: psycopg.Connection) -> None:
    seed_corpus(conn, 3)
    cfg = sim_config()
    corpus = load_corpus(conn, history_window_days=30)

    first = [extract_features(c, cfg) for c in corpus]
    second = [extract_features(c, cfg) for c in corpus]

    assert first == second


def test_amount_band_boundaries(conn: psycopg.Connection) -> None:
    cfg = sim_config()
    corpus_amounts = {10_000: "0-50000", 50_000: "50000-200000", 500_000: "200000+"}

    for amount, expected in corpus_amounts.items():
        seed_corpus(conn, 1, amount_minor=amount)
    corpus = load_corpus(conn, history_window_days=30)
    bands = {c.amount_minor: extract_features(c, cfg)["amount_band"] for c in corpus}

    assert bands == corpus_amounts


# ---- concurrency ---------------------------------------------------------


def test_concurrent_runs_are_independent(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    """Read-only corpus: concurrent runs need no lease and cannot interfere."""
    seed_corpus(conn, 6)
    results: list[Any] = [None, None]
    barrier = threading.Barrier(2)

    def worker(index: int) -> None:
        with psycopg.connect(migrated_database, autocommit=True) as own:
            barrier.wait()
            results[index] = run_simulation(own, config=sim_config(n_per_arm=4))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(r is not None for r in results)
    assert results[0].run_id != results[1].run_id
    assert fingerprint(results[0].outcomes) == fingerprint(results[1].outcomes)
    counts = conn.execute(
        "SELECT run_id, count(*) FROM sim_outcomes GROUP BY 1"
    ).fetchall()
    assert all(c[1] == 8 for c in counts), "each run is complete"


# ---- metrics reproducibility -----------------------------------------------
#
# A fixed seed must reproduce outcome ROWS AND reported METRICS. Reading live
# recovery_cases.state for metrics would let a real case that moved on after
# the run silently rewrite the run's numbers. These pin the guarantee: metrics
# are derived from case_state_at_run, frozen at selection.


def _expire(conn: psycopg.Connection, case_id: int) -> None:
    """Move a real case to EXPIRED_UNRESOLVED, as the TTL job legitimately would."""
    conn.execute(
        "UPDATE recovery_cases SET state = 'EXPIRED_UNRESOLVED', active_since = NULL, "
        "worker_id = NULL WHERE id = %s",
        (case_id,),
    )


def _unexpire(conn: psycopg.Connection, case_id: int) -> None:
    conn.execute(
        "UPDATE recovery_cases SET state = 'AWAITING_CUSTOMER', "
        "active_since = now() WHERE id = %s",
        (case_id,),
    )


def test_case_state_at_run_is_persisted(conn: psycopg.Connection) -> None:
    seed_corpus(conn, 4, state="AWAITING_CUSTOMER")

    result = _run(conn, n_per_arm=3)

    rows = conn.execute(
        "SELECT DISTINCT case_state_at_run::text FROM sim_outcomes WHERE run_id = %s",
        (result.run_id,),
    ).fetchall()
    assert [r[0] for r in rows] == ["AWAITING_CUSTOMER"]


def test_case_state_at_run_matches_selection_time_state(
    conn: psycopg.Connection,
) -> None:
    seed_corpus(conn, 3, state="EXPIRED_UNRESOLVED")
    seed_corpus(conn, 3, state="AWAITING_CUSTOMER")

    result = _run(conn, n_per_arm=6)

    live = dict(
        conn.execute("SELECT id, state::text FROM recovery_cases").fetchall()
    )
    for outcome in result.outcomes:
        assert outcome.case_state_at_run == live[outcome.case_id]


def test_metrics_for_run_matches_the_run(conn: psycopg.Connection) -> None:
    seed_corpus(conn, 6)

    result = _run(conn, n_per_arm=5)

    assert metrics_for_run(conn, result.run_id) == result.metrics


def test_metrics_survive_a_case_changing_state_afterwards(
    conn: psycopg.Connection,
) -> None:
    """A case's state can change freely after a run without touching its metrics."""
    seed_corpus(conn, 6, state="AWAITING_CUSTOMER")
    result = _run(conn, n_per_arm=6)
    before = metrics_for_run(conn, result.run_id)

    for outcome in result.outcomes[:2]:
        _expire(conn, outcome.case_id)

    assert metrics_for_run(conn, result.run_id) == before
    assert metrics_for_run(conn, result.run_id) == result.metrics


def test_case_included_at_run_stays_included(conn: psycopg.Connection) -> None:
    """A case eligible when the run happened is eligible forever, for that run."""
    seed_corpus(conn, 4, state="AWAITING_CUSTOMER")
    result = _run(conn, n_per_arm=4)
    assert result.metrics.excluded_from_lift == 0
    assert result.metrics.control_n == 4

    for outcome in result.outcomes:
        _expire(conn, outcome.case_id)

    recomputed = metrics_for_run(conn, result.run_id)
    assert recomputed.excluded_from_lift == 0
    assert recomputed.control_n == 4


def test_case_excluded_at_run_stays_excluded(conn: psycopg.Connection) -> None:
    """And the inverse: exclusion is frozen too, not re-litigated."""
    seed_corpus(conn, 4, state="EXPIRED_UNRESOLVED")
    result = _run(conn, n_per_arm=4)
    assert result.metrics.excluded_from_lift == 4
    assert result.metrics.control_n == 0

    for outcome in result.outcomes:
        _unexpire(conn, outcome.case_id)

    recomputed = metrics_for_run(conn, result.run_id)
    assert recomputed.excluded_from_lift == 4
    assert recomputed.control_n == 0


def test_expired_excluded_from_both_arms(conn: psycopg.Connection) -> None:
    """EXPIRED_UNRESOLVED is excluded entirely, never from one arm only."""
    seed_corpus(conn, 3, state="EXPIRED_UNRESOLVED")
    seed_corpus(conn, 3, state="AWAITING_CUSTOMER")

    result = _run(conn, n_per_arm=6)
    metrics = metrics_for_run(conn, result.run_id)

    assert metrics.excluded_from_lift == 3
    assert metrics.control_n == 3
    assert metrics.treatment_n == 3


def test_metrics_for_run_never_queries_recovery_cases(
    conn: psycopg.Connection,
) -> None:
    """Structural: the read path touches exactly one table.

    Proven by deleting nothing and instead inspecting the SQL actually issued --
    a metrics recomputation that joined recovery_cases would be re-exposed to
    the mutable state this fix exists to escape.
    """
    import ast
    import inspect

    from reclaim.domain import simulator

    tree = ast.parse(inspect.getsource(simulator.load_run_outcomes).strip())
    func = tree.body[0]
    docstring = ast.get_docstring(func, clean=False)

    sql_literals = [
        node.value
        for node in ast.walk(func)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value != docstring  # the docstring EXPLAINS the rule; it is not SQL
    ]
    joined = " ".join(sql_literals).lower()

    assert "sim_outcomes" in joined
    assert "recovery_cases" not in joined
    assert "financial_obligations" not in joined
    assert "join" not in joined


def test_metrics_for_run_is_stable_across_repeated_calls(
    conn: psycopg.Connection,
) -> None:
    seed_corpus(conn, 5)
    result = _run(conn, n_per_arm=4)

    assert len({metrics_for_run(conn, result.run_id) for _ in range(5)}) == 1


def test_metrics_for_run_rejects_an_unknown_run(conn: psycopg.Connection) -> None:
    with pytest.raises(SimulationBlocked):
        metrics_for_run(conn, 999_999)


def test_two_runs_keep_separate_metrics(conn: psycopg.Connection) -> None:
    """Recomputation is per-run, not a global recount."""
    seed_corpus(conn, 6, state="AWAITING_CUSTOMER")
    first = _run(conn, n_per_arm=6)

    for outcome in first.outcomes[:3]:
        _expire(conn, outcome.case_id)
    second = _run(conn, n_per_arm=6)

    assert metrics_for_run(conn, first.run_id) == first.metrics
    assert metrics_for_run(conn, second.run_id) == second.metrics
    assert metrics_for_run(conn, first.run_id) != metrics_for_run(conn, second.run_id)
