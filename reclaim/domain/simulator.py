"""Simulator and evaluation.

The acceptance bar is narrow and exact: **I11, and a fixed seed reproduces
exactly.** Everything here serves those two properties.

Three structural boundaries, in order of how much they matter:

1. **Read-only over production.** The simulator selects an existing corpus of
   real `recovery_cases` and writes only `sim_runs` / `sim_outcomes`. It creates
   no case, mutates none, transitions none, takes no lease, and constructs no
   provider client. Safety is a property of construction rather than of query
   predicates: there is no write path to production state to guard.

2. **I11 by argument type.** `extract_features` takes a `CaseRecord` and never a
   database connection, so it *cannot* reach `diagnoses` even if someone tried.
   Confidence, reasoning, model version and diagnosed cause are unreachable from
   the probability path, not merely unused by it.

3. **Determinism by construction.** Every random draw is keyed on
   `(seed, case_id, arm)` through a fresh generator, so outcomes do not depend on
   iteration order, sampling order, or how many draws preceded them.

Out of scope: dashboard styling. This module computes arm rates and a lift
point estimate; it does not compute an interval, because no method for one
is specified.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from reclaim.config import (
    SIM_MODEL_BASELINE_PLUS_UPLIFT,
    SIM_MODEL_DIRECT_RATES,
    SimulatorConfig,
    SimulatorConfigError,
    load_simulator_config,
)

ARM_CONTROL = "CONTROL"
ARM_TREATMENT = "TREATMENT"

# CREATE_PAYMENT_LINK is the only dispatchable action, so it is the only
# action a treatment arm can fire.
TREATMENT_ACTION = "CREATE_PAYMENT_LINK"

# EXPIRED_UNRESOLVED is excluded from the lift calculation entirely. Counting
# it as a failure would flatter the intervention.
LIFT_EXCLUDED_STATES = frozenset({"EXPIRED_UNRESOLVED"})

UNKNOWN_CAUSE = "UNKNOWN"


class SimulationBlocked(Exception):
    """The run cannot proceed as configured. Never a simulated outcome."""


@dataclass(frozen=True)
class CaseRecord:
    """One real case, reduced to the fields the probability model may use.

    This is the I11 boundary made structural. Nothing agent-generated appears
    here, and `extract_features` accepts nothing else -- so a future change that
    wanted `diagnoses.cause` would have to widen this record visibly rather than
    reach for a connection.
    """

    case_id: int
    state: str
    amount_minor: int
    currency: str
    customer_ref: str
    first_seen_at: datetime
    failure_cause_code: str
    prior_case_count: int


@dataclass(frozen=True)
class SimOutcome:
    case_id: int
    arm: str
    action_type: str | None
    resolved: bool
    amount_minor: int
    probability: float
    features: dict[str, Any]
    # The case's state when it was SELECTED into this run, not its state now.
    # Metrics derive EXPIRED_UNRESOLVED exclusion from this, so a run's reported
    # numbers cannot be rewritten by the real case's later history.
    case_state_at_run: str


@dataclass(frozen=True)
class SimMetrics:
    """Arm rates and lift point estimate. No interval is computed."""

    control_n: int
    treatment_n: int
    control_resolved: int
    treatment_resolved: int
    control_rate: float
    treatment_rate: float
    lift: float
    excluded_from_lift: int
    # The unresolved bucket is reported as a count *and* a sum, kept apart from
    # both arms: an expired case is neither a win nor a confirmed loss, and
    # folding its money into either rate would misstate the result.
    unresolved_amount_minor: int


@dataclass(frozen=True)
class SimRunResult:
    run_id: int
    seed: int
    n_per_arm: int
    outcomes: tuple[SimOutcome, ...]
    metrics: SimMetrics


# ---------------------------------------------------------------------------
# Corpus selection (read-only)
# ---------------------------------------------------------------------------


def load_corpus(conn: psycopg.Connection, *, history_window_days: int) -> list[CaseRecord]:
    """Every real case, ordered deterministically. Reads only; writes nothing.

    `failure_cause_code` comes from the ORIGINAL `payment.failed` webhook
    payload -- provider-sourced and genuinely pre-decision. `diagnoses.cause` is
    deliberately NOT used: it is agent-generated whenever `source='LLM'`, and an
    agent-generated value must never feed the probability model it would be
    evaluated against.
    """
    rows = conn.execute(
        """
        SELECT c.id,
               c.state::text,
               o.amount_minor,
               o.currency,
               o.customer_ref,
               o.first_seen_at,
               COALESCE(
                   (SELECT we.payload #>> '{payload,payment,entity,error_code}'
                      FROM webhook_events we
                     WHERE we.anchor_canonical = o.anchor_canonical
                       AND we.signature_valid
                       AND we.payload #>> '{payload,payment,entity,error_code}'
                           IS NOT NULL
                     ORDER BY we.received_at, we.id
                     LIMIT 1),
                   %s
               ) AS failure_cause_code,
               (SELECT count(*)
                  FROM recovery_cases prior
                  JOIN financial_obligations po ON po.id = prior.obligation_id
                 WHERE po.customer_ref = o.customer_ref
                   AND prior.created_at < c.created_at
                   AND prior.created_at >= c.created_at
                       - (%s || ' days')::interval) AS prior_case_count
          FROM recovery_cases c
          JOIN financial_obligations o ON o.id = c.obligation_id
         ORDER BY c.id
        """,
        (UNKNOWN_CAUSE, str(history_window_days)),
    ).fetchall()

    return [
        CaseRecord(
            case_id=int(r[0]),
            state=str(r[1]),
            amount_minor=int(r[2]),
            currency=str(r[3]),
            customer_ref=str(r[4]),
            first_seen_at=r[5],
            failure_cause_code=str(r[6]),
            prior_case_count=int(r[7]),
        )
        for r in rows
    ]


def select_cases(
    corpus: list[CaseRecord], *, seed: int, n_per_arm: int
) -> list[CaseRecord]:
    """Deterministic sample of n_per_arm cases from a deterministically ordered corpus.

    Ranks by a hash of (seed, case_id) rather than shuffling, so the selection
    depends only on the seed and the case ids present -- never on corpus order,
    row count, or how the rows arrived.

    A fixed seed reproduces a run exactly **for a fixed corpus**. It does not
    guarantee the same sample once the corpus changes; that is inherent to
    sampling anything live, and is why `sim_outcomes.case_id` persists the
    selection per row.
    """
    if n_per_arm <= 0:
        raise SimulationBlocked("n_per_arm must be positive")
    if len(corpus) < n_per_arm:
        raise SimulationBlocked(
            f"corpus holds {len(corpus)} cases; n_per_arm={n_per_arm} requires at "
            "least that many. Seed real cases through the ingest path first."
        )
    ranked = sorted(corpus, key=lambda case: (_rank(seed, case.case_id), case.case_id))
    return ranked[:n_per_arm]


def _rank(seed: int, case_id: int) -> str:
    return hashlib.sha256(f"{seed}:{case_id}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Feature extraction -- I11's structural boundary (no connection parameter)
# ---------------------------------------------------------------------------


def extract_features(case: CaseRecord, config: SimulatorConfig) -> dict[str, Any]:
    """The four pre-decision features. Pure; no database access.

    Recorded, **not weighted**. No defensible empirical weight exists for any of
    them, so assigning coefficients would fabricate a finding. They are written
    to `sim_outcomes.pre_decision_features` as an audit record and have no
    influence on probability.
    """
    hour = case.first_seen_at.astimezone(_zone(config.feature_timezone)).hour
    return {
        "failure_cause_code": case.failure_cause_code,
        "amount_band": _amount_band(case.amount_minor, config.amount_band_bounds),
        "customer_payment_history": case.prior_case_count,
        "hour_of_day": hour,
    }


def _zone(name: str) -> Any:
    if name.upper() == "UTC":
        return timezone.utc
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)


def _amount_band(amount_minor: int, bounds: tuple[int, ...]) -> str:
    lower = 0
    for bound in bounds:
        if amount_minor < bound:
            return f"{lower}-{bound}"
        lower = bound
    return f"{lower}+"


# ---------------------------------------------------------------------------
# Probability -- pure, explicit, configurable
# ---------------------------------------------------------------------------


def probability_for(arm: str, config: SimulatorConfig) -> float:
    """A combination function that is nowhere authoritatively specified.

    The permitted *inputs* are fixed, but the arithmetic that turns a baseline
    and a per-action parameter into a probability is nowhere defined, so it is
    a named configuration choice rather than a silent assumption.
    """
    baseline = config.organic_baseline_rate
    if arm == ARM_CONTROL:
        return baseline

    param = config.action_params[TREATMENT_ACTION]
    if config.model == SIM_MODEL_BASELINE_PLUS_UPLIFT:
        return _clamp(baseline + param)
    if config.model == SIM_MODEL_DIRECT_RATES:
        return _clamp(param)
    raise SimulatorConfigError(f"unknown model {config.model!r}")


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def resolve(seed: int, case_id: int, arm: str, probability: float) -> bool:
    """Seeded Bernoulli draw, keyed on (seed, case_id, arm).

    A fresh digest per draw rather than a shared stream: the outcome for a case
    is then independent of how many draws preceded it, so adding, removing or
    reordering cases cannot change any other case's result.
    """
    digest = hashlib.sha256(f"{seed}:{case_id}:{arm}".encode("utf-8")).digest()
    draw = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return draw < probability


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def build_outcomes(
    cases: list[CaseRecord], config: SimulatorConfig
) -> list[SimOutcome]:
    """Both arms over the same cases -- a paired design, which the schema allows.

    `sim_outcomes` carries no UNIQUE(run_id, case_id), so one case may appear in
    both arms. Pairing removes between-arm confounding entirely, which matters
    at a small sample size.
    """
    outcomes: list[SimOutcome] = []
    for arm in (ARM_CONTROL, ARM_TREATMENT):
        action = None if arm == ARM_CONTROL else TREATMENT_ACTION
        probability = probability_for(arm, config)
        for case in cases:
            outcomes.append(
                SimOutcome(
                    case_id=case.case_id,
                    arm=arm,
                    action_type=action,
                    resolved=resolve(config.seed, case.case_id, arm, probability),
                    amount_minor=case.amount_minor,
                    probability=probability,
                    features=extract_features(case, config),
                    case_state_at_run=case.state,
                )
            )
    return outcomes


def compute_metrics(outcomes: list[SimOutcome]) -> SimMetrics:
    """Arm rates and the lift point estimate.

    A pure function of the outcome rows -- it takes no CaseRecord and reads no
    live state. Exclusion comes from `case_state_at_run`, frozen when the case
    was selected, so recomputing a run's metrics later cannot produce a
    different answer because a real case moved on.

    EXPIRED_UNRESOLVED is excluded from the lift calculation entirely, so those
    cases are dropped from BOTH arms before any rate is computed -- dropping
    them from one arm only would bias the difference.
    """
    excluded = {
        o.case_id for o in outcomes if o.case_state_at_run in LIFT_EXCLUDED_STATES
    }
    eligible = [o for o in outcomes if o.case_id not in excluded]

    # One amount per excluded case, not one per outcome row: a case appears in
    # both arms carrying the same obligation amount, so summing rows would
    # double every unresolved figure. Integer minor units throughout.
    unresolved_amount_minor = sum(
        amount
        for amount in {
            o.case_id: o.amount_minor for o in outcomes if o.case_id in excluded
        }.values()
    )

    control = [o for o in eligible if o.arm == ARM_CONTROL]
    treatment = [o for o in eligible if o.arm == ARM_TREATMENT]
    control_resolved = sum(1 for o in control if o.resolved)
    treatment_resolved = sum(1 for o in treatment if o.resolved)

    control_rate = control_resolved / len(control) if control else 0.0
    treatment_rate = treatment_resolved / len(treatment) if treatment else 0.0

    return SimMetrics(
        control_n=len(control),
        treatment_n=len(treatment),
        control_resolved=control_resolved,
        treatment_resolved=treatment_resolved,
        control_rate=control_rate,
        treatment_rate=treatment_rate,
        lift=treatment_rate - control_rate,
        excluded_from_lift=len(excluded),
        unresolved_amount_minor=unresolved_amount_minor,
    )


def run_simulation(
    conn: psycopg.Connection, *, config: SimulatorConfig | None = None
) -> SimRunResult:
    """One complete run. Reads production, writes only sim_runs / sim_outcomes.

    Single transaction: `sim_runs` has no `completed_at` or `status` column, so a
    partially-populated run would be indistinguishable from a whole one. Atomicity
    is what keeps "a run in the database is a complete run" true without a
    migration. A failure leaves no trace to clean up.
    """
    cfg = config or load_simulator_config()

    corpus = load_corpus(conn, history_window_days=cfg.history_window_days)
    cases = select_cases(corpus, seed=cfg.seed, n_per_arm=cfg.n_per_arm)
    outcomes = build_outcomes(cases, cfg)
    metrics = compute_metrics(outcomes)

    with conn.transaction():
        row = conn.execute(
            """
            INSERT INTO sim_runs (seed, n_per_arm, params)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (cfg.seed, cfg.n_per_arm, Jsonb(cfg.params_for_run())),
        ).fetchone()
        assert row is not None
        run_id = int(row[0])

        for outcome in outcomes:
            conn.execute(
                """
                INSERT INTO sim_outcomes (
                    run_id, arm, case_id, pre_decision_features,
                    action_type, resolved, amount_minor, case_state_at_run
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id,
                    outcome.arm,
                    outcome.case_id,
                    Jsonb(outcome.features),
                    outcome.action_type,
                    outcome.resolved,
                    outcome.amount_minor,
                    outcome.case_state_at_run,
                ),
            )

    return SimRunResult(
        run_id=run_id,
        seed=cfg.seed,
        n_per_arm=cfg.n_per_arm,
        outcomes=tuple(outcomes),
        metrics=metrics,
    )


def load_run_outcomes(conn: psycopg.Connection, run_id: int) -> list[SimOutcome]:
    """Rehydrate a run's outcomes from `sim_outcomes` alone.

    Deliberately queries exactly one table. It never joins `recovery_cases`, so
    a run's persisted result cannot be re-coloured by what happened to those
    cases afterwards -- that independence is the whole point.
    """
    rows = conn.execute(
        """
        SELECT case_id, arm::text, action_type::text, resolved, amount_minor,
               pre_decision_features, case_state_at_run::text
          FROM sim_outcomes
         WHERE run_id = %s
         ORDER BY id
        """,
        (run_id,),
    ).fetchall()
    if not rows:
        raise SimulationBlocked(f"no simulation outcomes for run {run_id}")

    return [
        SimOutcome(
            case_id=int(r[0]),
            arm=str(r[1]),
            action_type=r[2],
            resolved=bool(r[3]),
            amount_minor=int(r[4]),
            probability=float("nan"),  # not persisted; never an input to metrics
            features=r[5],
            case_state_at_run=str(r[6]),
        )
        for r in rows
    ]


def metrics_for_run(conn: psycopg.Connection, run_id: int) -> SimMetrics:
    """Recompute a completed run's metrics from durable simulation data only.

    Returns exactly what `run_simulation` reported at the time, however much the
    underlying real cases have changed since: a seed makes every reported number
    reproducible, and that now covers the reported numbers, not merely the
    outcome rows.
    """
    return compute_metrics(load_run_outcomes(conn, run_id))


def fingerprint(outcomes: tuple[SimOutcome, ...] | list[SimOutcome]) -> str:
    """Stable digest of a run's outcomes, for reproducibility assertions."""
    payload = json.dumps(
        [
            [o.case_id, o.arm, o.action_type, o.resolved, o.amount_minor, o.features]
            for o in outcomes
        ],
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
