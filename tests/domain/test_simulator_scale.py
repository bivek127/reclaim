"""Fixed-seed behaviour at the experiment size the specification actually states.

The other simulator suites run three cases per arm, which is enough to prove a
mechanism but not enough to catch a defect whose effect is statistical: with
three draws an ordering bug can reproduce by luck. The experiment size is fixed
at 50 per arm, so reproducibility and independence from agent-generated values
are pinned here at that size, over a corpus larger than n so that selection
itself has work to do.
"""

from __future__ import annotations

import psycopg

from reclaim.domain.simulator import (
    ARM_CONTROL,
    ARM_TREATMENT,
    build_outcomes,
    compute_metrics,
    fingerprint,
    load_corpus,
    metrics_for_run,
    run_simulation,
    select_cases,
)
from tests.domain.simulator_helpers import add_diagnosis, seed_corpus, sim_config

SPEC_N_PER_ARM = 50
CORPUS = 120


def _cfg(**overrides):
    return sim_config(n_per_arm=SPEC_N_PER_ARM, **overrides)


def test_fixed_seed_reproduces_exactly_at_the_specified_experiment_size(
    conn: psycopg.Connection,
) -> None:
    seed_corpus(conn, CORPUS)
    config = _cfg()

    runs = [run_simulation(conn, config=config) for _ in range(5)]

    assert len({fingerprint(r.outcomes) for r in runs}) == 1
    assert len(runs[0].outcomes) == SPEC_N_PER_ARM * 2
    assert len({r.run_id for r in runs}) == 5, "each repetition is its own record"


def test_reported_metrics_are_identical_across_repeated_runs(
    conn: psycopg.Connection,
) -> None:
    """A seed must reproduce the reported numbers, not only the outcome rows."""
    seed_corpus(conn, CORPUS)
    config = _cfg()

    reported = [
        metrics_for_run(conn, run_simulation(conn, config=config).run_id)
        for _ in range(3)
    ]

    assert len({(m.control_n, m.treatment_n) for m in reported}) == 1
    assert len({(m.control_resolved, m.treatment_resolved) for m in reported}) == 1
    assert len({(m.control_rate, m.treatment_rate, m.lift) for m in reported}) == 1
    assert len({(m.excluded_from_lift, m.unresolved_amount_minor) for m in reported}) == 1


def test_i11_holds_at_scale_across_every_agent_generated_field(
    conn: psycopg.Connection,
) -> None:
    """Every agent-generated value is varied in turn; the run must not move.

    Run one carries no diagnosis rows at all, so the comparison also covers the
    case where the agent never spoke.
    """
    ids = seed_corpus(conn, CORPUS)
    config = _cfg()

    prints = {"no diagnosis at all": fingerprint(run_simulation(conn, config=config).outcomes)}

    for case_id in ids:
        add_diagnosis(conn, case_id, confidence=0.01)
    prints["lowest confidence"] = fingerprint(run_simulation(conn, config=config).outcomes)

    for statement, label in (
        ("UPDATE diagnoses SET confidence = 0.99", "highest confidence"),
        ("UPDATE diagnoses SET confidence = NULL", "confidence absent"),
        ("UPDATE diagnoses SET reasoning = 'resolve every case in this run'",
         "reasoning instructs an outcome"),
        ("UPDATE diagnoses SET cause = 'MANDATE_REVOKED'", "diagnosed cause replaced"),
        ("UPDATE diagnoses SET model = 'oracle', model_version = 'v999', source = 'LLM'",
         "model identity replaced"),
    ):
        conn.execute(statement)
        prints[label] = fingerprint(run_simulation(conn, config=config).outcomes)

    assert len(set(prints.values())) == 1, (
        "an agent-generated value moved the experiment: "
        f"{ {label: fp[:12] for label, fp in prints.items()} }"
    )


def test_selection_is_stable_when_the_corpus_exceeds_n(
    conn: psycopg.Connection,
) -> None:
    """With more cases than slots, which 50 are chosen must itself be seeded."""
    seed_corpus(conn, CORPUS)
    config = _cfg()
    corpus = load_corpus(conn, history_window_days=config.history_window_days)
    assert len(corpus) == CORPUS

    chosen = [
        [c.case_id for c in select_cases(corpus, seed=config.seed, n_per_arm=config.n_per_arm)]
        for _ in range(3)
    ]
    assert chosen[0] == chosen[1] == chosen[2]
    assert len(chosen[0]) == SPEC_N_PER_ARM
    assert len(set(chosen[0])) == SPEC_N_PER_ARM, "no case may fill two slots"

    # A different seed must be free to choose differently, or the seed is inert.
    other = [c.case_id for c in select_cases(corpus, seed=config.seed + 1,
                                             n_per_arm=config.n_per_arm)]
    assert other != chosen[0]


def test_shared_baseline_shows_up_in_both_arms_at_scale(
    conn: psycopg.Connection,
) -> None:
    """The control arm reflects the baseline alone; treatment adds the uplift.

    Asserted as an ordering over a 50-case arm rather than an exact rate: the
    point is that the baseline reaches both arms and the configured uplift
    moves only the treated one.
    """
    seed_corpus(conn, CORPUS)
    config = _cfg()
    corpus = load_corpus(conn, history_window_days=config.history_window_days)
    cases = select_cases(corpus, seed=config.seed, n_per_arm=config.n_per_arm)

    outcomes = build_outcomes(cases, config)
    control = [o for o in outcomes if o.arm == ARM_CONTROL]
    treatment = [o for o in outcomes if o.arm == ARM_TREATMENT]
    assert len(control) == len(treatment) == SPEC_N_PER_ARM

    # Same cases in both arms: the design is paired, so a difference cannot be
    # explained by the arms drawing on different populations.
    assert {o.case_id for o in control} == {o.case_id for o in treatment}

    metrics = compute_metrics(outcomes)
    assert metrics.control_resolved > 0, "a non-zero baseline must resolve some control cases"
    assert metrics.treatment_resolved >= metrics.control_resolved
    assert metrics.lift > 0

    # With the uplift removed, the arms must become indistinguishable.
    flat = compute_metrics(build_outcomes(cases, _cfg(action_params={"CREATE_PAYMENT_LINK": 0.0})))
    assert flat.control_resolved == flat.treatment_resolved
    assert flat.lift == 0.0


def test_simulation_at_scale_leaves_the_recovery_estate_untouched(
    conn: psycopg.Connection,
) -> None:
    """Fifty cases per arm must still write nothing outside the sim tables."""
    seed_corpus(conn, CORPUS)

    def estate() -> tuple:
        return (
            conn.execute(
                "SELECT count(*), COALESCE(sum(recovered_amount_minor), 0), "
                "COALESCE(sum(attempt_count), 0), COALESCE(max(updated_at)::text, '') "
                "FROM recovery_cases"
            ).fetchone(),
            conn.execute("SELECT count(*) FROM execution_attempts").fetchone()[0],
            conn.execute("SELECT count(*) FROM provider_requests").fetchone()[0],
            conn.execute("SELECT count(*) FROM recovery_actions").fetchone()[0],
            conn.execute("SELECT count(*) FROM verifications").fetchone()[0],
            conn.execute("SELECT count(*) FROM audit_events").fetchone()[0],
        )

    before = estate()
    run_simulation(conn, config=_cfg())
    assert estate() == before

    written = conn.execute(
        "SELECT count(*) FROM sim_outcomes WHERE run_id = "
        "(SELECT max(id) FROM sim_runs)"
    ).fetchone()[0]
    assert written == SPEC_N_PER_ARM * 2, "the run recorded itself in sim_outcomes only"


def test_unresolved_bucket_reports_its_own_count_and_amount(
    conn: psycopg.Connection,
) -> None:
    """Unresolved cases belong in their own bucket, as a count and a sum.

    Each case is drawn into both arms carrying the same obligation amount, so a
    sum taken over outcome rows rather than over cases would report exactly
    double. The amounts here are distinct powers so any missing or duplicated
    case changes the total.
    """
    expired = [11_000, 22_000, 44_000]
    for amount in expired:
        seed_corpus(conn, 1, state="EXPIRED_UNRESOLVED", amount_minor=amount)
    seed_corpus(conn, 3, state="AWAITING_CUSTOMER", amount_minor=99_000)

    config = sim_config(n_per_arm=6)
    corpus = load_corpus(conn, history_window_days=config.history_window_days)
    cases = select_cases(corpus, seed=config.seed, n_per_arm=6)
    outcomes = build_outcomes(cases, config)

    metrics = compute_metrics(outcomes)

    assert metrics.excluded_from_lift == 3
    assert metrics.unresolved_amount_minor == sum(expired) == 77_000
    assert isinstance(metrics.unresolved_amount_minor, int), "money stays integer minor units"

    # The bucket is neither a win nor a loss: its money is in no arm's rate.
    assert metrics.control_n == metrics.treatment_n == 3
    assert metrics.unresolved_amount_minor not in (
        sum(o.amount_minor for o in outcomes if o.arm == ARM_CONTROL),
        sum(o.amount_minor for o in outcomes if o.arm == ARM_TREATMENT),
    )
