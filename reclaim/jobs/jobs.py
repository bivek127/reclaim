"""Registration of the background jobs, as data.

Each entry names an existing domain callable and the configuration that decides
how often it runs. Nothing here implements recovery behaviour: the domain
functions already select their own rows, open their own transactions, and hold
whatever locks they need. Registration only says *when*.

Intervals and batch sizes are read from `config/operational.yaml` at import.
A literal here would be a second schedule competing with the file.
"""

from __future__ import annotations

from typing import Any

from reclaim.api.db import app_conn, verifier_conn
from reclaim.config import lease_seconds_for, load_operational, load_policy
from reclaim.domain import (
    expire_action_deadlines,
    expire_reviews,
    expire_ttl,
    sweep_expired_leases,
)
from reclaim.domain.states import CaseState
from reclaim.jobs.breaker import breaker_monitor_operation
from reclaim.jobs.percase import (
    case_worker_operation,
    diagnosis_operation,
    executor_operation,
    policy_operation,
    reconciler_operation,
    verifier_operation,
)
from reclaim.jobs.registry import JOBS, JobKind, JobRegistry, JobSpec

SWEEPER = "sweeper"
TTL_EXPIRY = "ttl-expiry"
REVIEW_EXPIRY = "review-expiry"
ACTION_DEADLINE_EXPIRY = "action-deadline-expiry"
BREAKER_MONITOR = "breaker-monitor"
CASE_WORKER = "case-worker"
DIAGNOSIS = "diagnosis"
POLICY = "policy"
EXECUTOR = "executor"
RECONCILER = "reconciler"
VERIFIER = "verifier"


def register_batch_jobs(
    registry: JobRegistry = JOBS,
    config: dict | None = None,
    policy: dict | None = None,
) -> JobRegistry:
    """Register the batch jobs whose trigger the job contract states.

    Batch jobs need no lease held by the runtime: each function already claims
    or locks the rows it touches for the duration of its own transaction.
    """
    values = config if config is not None else load_operational()
    limits = policy if policy is not None else load_policy()
    batch_size = int(values["sweeper_batch_size"])

    registry.register(
        JobSpec(
            name=SWEEPER,
            kind=JobKind.BATCH,
            interval_seconds=int(values["sweeper_interval_seconds"]),
            operation=sweep_expired_leases,
            connect=app_conn,
            limit=batch_size,
        )
    )
    registry.register(
        JobSpec(
            name=TTL_EXPIRY,
            kind=JobKind.BATCH,
            interval_seconds=int(values["ttl_expiry_interval_seconds"]),
            operation=expire_ttl,
            connect=app_conn,
            limit=batch_size,
        )
    )
    # The job contract states no trigger for this sweep, so its cadence is an
    # implementation decision recorded in configuration. It is tuned separately
    # from ttl-expiry on purpose: a closed payment window is not TTL
    # exhaustion, and the two sweeps must be able to diverge.
    registry.register(
        JobSpec(
            name=ACTION_DEADLINE_EXPIRY,
            kind=JobKind.BATCH,
            interval_seconds=int(values["action_deadline_expiry_interval_seconds"]),
            operation=expire_action_deadlines,
            connect=app_conn,
            limit=batch_size,
        )
    )
    registry.register(
        JobSpec(
            name=REVIEW_EXPIRY,
            kind=JobKind.BATCH,
            interval_seconds=int(values["review_expiry_interval_seconds"]),
            operation=expire_reviews,
            connect=app_conn,
            limit=batch_size,
        )
    )
    # The breaker is a singleton row, not a queue of cases, so it runs on the
    # batch loop: a timer with no lease. Its threshold and reset window are
    # business policy and come from the policy configuration, not from here.
    registry.register(
        JobSpec(
            name=BREAKER_MONITOR,
            kind=JobKind.BATCH,
            interval_seconds=int(values["breaker_monitor_interval_seconds"]),
            operation=breaker_monitor_operation(
                failure_threshold=int(limits["breaker_failure_threshold"]),
                reset_seconds=int(limits["breaker_reset_seconds"]),
            ),
            connect=app_conn,
            limit=batch_size,
        )
    )
    return registry


def register_per_case_jobs(
    registry: JobRegistry = JOBS,
    config: dict | None = None,
    provider: Any = None,
    llm: Any = None,
) -> JobRegistry:
    """Register the per-case jobs whose contract the job table fully states.

    Each claims one state, holds the lease that state's work is sized for, and
    hands the claim's fencing token straight to the domain.

    ATTEMPT_FAILED is not claimed here. Its routing rule is settled --
    docs/ARCHITECTURE.md gives it explicitly -- but no domain accessor yet
    reads `(attempt_count, max_attempts)` for a case outside POLICY_EVAL, which
    every existing reader assumes. That is a missing accessor, not an
    undefined contract, and it is not this registration's job to invent one.
    """
    values = config if config is not None else load_operational()
    kwargs = {} if provider is None else {"provider": provider}
    llm_kwargs = {} if llm is None else {"llm": llm}

    # Both edges are mechanical, so one job walks them on one cadence. The
    # enrichment lease covers the pair: neither transition does work beyond the
    # write itself.
    registry.register(
        JobSpec(
            name=CASE_WORKER,
            kind=JobKind.PER_CASE,
            interval_seconds=int(values["case_worker_interval_seconds"]),
            operation=case_worker_operation(),
            connect=app_conn,
            expected_states=(CaseState.NEW, CaseState.ENRICHING),
            lease_seconds=lease_seconds_for("enrichment"),
        )
    )
    registry.register(
        JobSpec(
            name=DIAGNOSIS,
            kind=JobKind.PER_CASE,
            interval_seconds=int(values["case_worker_interval_seconds"]),
            operation=diagnosis_operation(**llm_kwargs),
            connect=app_conn,
            expected_states=(CaseState.DIAGNOSING,),
            lease_seconds=lease_seconds_for("diagnosis"),
        )
    )
    # §3.1 names this state's owner "Policy Engine", distinct from the "Case
    # Worker" label the surrounding states share -- a real decision, not a
    # mechanical transition, so it gets its own job rather than folding into
    # case-worker's advance map.
    registry.register(
        JobSpec(
            name=POLICY,
            kind=JobKind.PER_CASE,
            interval_seconds=int(values["case_worker_interval_seconds"]),
            operation=policy_operation(),
            connect=app_conn,
            expected_states=(CaseState.POLICY_EVAL,),
            lease_seconds=lease_seconds_for("policy"),
        )
    )
    registry.register(
        JobSpec(
            name=EXECUTOR,
            kind=JobKind.PER_CASE,
            interval_seconds=int(values["executor_interval_seconds"]),
            operation=executor_operation(
                link_ttl_seconds=int(values["payment_link_ttl_seconds"]), **kwargs
            ),
            connect=app_conn,
            # Human approval executes through this same job: review leaves the
            # case ESCALATED with an open PROPOSED action, and the executor is
            # the component that performs the move to EXECUTING.
            expected_states=(CaseState.ACTION_READY, CaseState.ESCALATED),
            lease_seconds=lease_seconds_for("execution"),
        )
    )
    registry.register(
        JobSpec(
            name=RECONCILER,
            kind=JobKind.PER_CASE,
            interval_seconds=int(values["reconciliation_interval_seconds"]),
            operation=reconciler_operation(**kwargs),
            connect=app_conn,
            expected_states=(CaseState.AMBIGUOUS,),
            lease_seconds=lease_seconds_for("reconciliation"),
        )
    )
    # The verifier is the only job that runs as `recovery_verifier`: recognising
    # revenue needs a role the application role deliberately does not have.
    registry.register(
        JobSpec(
            name=VERIFIER,
            kind=JobKind.PER_CASE,
            interval_seconds=int(values["verifier_interval_seconds"]),
            operation=verifier_operation(**kwargs),
            connect=verifier_conn,
            expected_states=(CaseState.AWAITING_CUSTOMER,),
            lease_seconds=lease_seconds_for("verification"),
        )
    )
    return registry


def register_all_jobs(registry: JobRegistry = JOBS) -> JobRegistry:
    """Every job the runtime can currently run."""
    register_batch_jobs(registry)
    register_per_case_jobs(registry)
    return registry
