"""Per-case jobs: claim one case, run one existing domain operation, release.

Each adapter here exists only to bind what the domain function needs but the
runner cannot know -- a provider handle -- and to name the worker. The domain
functions keep every decision: state transitions, provider calls, idempotency,
poll budgets and transaction boundaries are theirs, unchanged.

The provider is built lazily, on the tick that needs it. Constructing it at
registration would make the whole job table refuse to load wherever provider
credentials are absent, which would take the expiry jobs down with it.
"""

from __future__ import annotations

from typing import Any, Callable

import psycopg

from reclaim.domain.execution import dispatch
from reclaim.domain.policy import authorising_decision_id
from reclaim.domain.reconciliation import reconcile_case
from reclaim.domain.verification import verify_case
from reclaim.provider.config import load_provider_config
from reclaim.provider.contract import PaymentProvider
from reclaim.provider.razorpay import RazorpayAdapter

EXECUTOR_WORKER_ID = "executor"
RECONCILER_WORKER_ID = "reconciler"
VERIFIER_WORKER_ID = "verifier"

ProviderFactory = Callable[[], PaymentProvider]


def default_provider() -> PaymentProvider:
    """The configured Razorpay adapter, built from existing provider config."""
    return RazorpayAdapter(load_provider_config())


def reconciler_operation(provider: ProviderFactory = default_provider) -> Any:
    """Resolve one ambiguous case against the provider.

    `reconcile_case` reads its own poll and re-POST budgets from operational
    configuration and owns both transactions around the network call.
    """

    def operation(conn: psycopg.Connection, case_id: int, *, fencing_token: int) -> Any:
        return reconcile_case(
            conn,
            case_id,
            provider=provider(),
            fencing_token=fencing_token,
            worker_id=RECONCILER_WORKER_ID,
        )

    operation.__name__ = "reconciler_operation"
    return operation


def verifier_operation(provider: ProviderFactory = default_provider) -> Any:
    """Independently verify one case that is waiting on the customer.

    Runs on a `recovery_verifier` connection because recognising revenue
    requires that role; the connection is a property of the job's registration,
    not something this adapter arranges.

    A case with no correlated webhook yet is a no-op inside `verify_case`, so
    claiming on state alone is safe: the domain decides there is nothing to
    compare rather than the runtime pre-filtering the queue.
    """

    def operation(conn: psycopg.Connection, case_id: int, *, fencing_token: int) -> Any:
        return verify_case(
            conn,
            case_id,
            provider=provider(),
            fencing_token=fencing_token,
            worker_id=VERIFIER_WORKER_ID,
        )

    operation.__name__ = "verifier_operation"
    return operation


def executor_operation(
    link_ttl_seconds: int, provider: ProviderFactory = default_provider
) -> Any:
    """Dispatch the action a policy decision already authorised.

    The authorising decision is read through the domain's own accessor: which
    decision authorises an action is a policy question, and answering it with a
    query here would put that judgement in the runtime.

    `dispatch` owns everything after that -- the attempt row, the idempotency
    key persisted before any network call, the breaker gate, the provider call
    and the outcome mapping. This adapter never retries: a dispatch that fails
    is the domain's recorded outcome, not something to attempt again.
    """

    def operation(conn: psycopg.Connection, case_id: int, *, fencing_token: int) -> Any:
        decision_id = authorising_decision_id(conn, case_id)
        if decision_id is None:
            # A case cannot reach ACTION_READY without one, so this is a data
            # anomaly. Surfacing it releases the lease and leaves the case for
            # a human, rather than inventing an authorisation.
            raise RuntimeError(
                f"case {case_id} is ACTION_READY with no authorising policy decision"
            )
        return dispatch(
            conn,
            case_id,
            provider=provider(),
            fencing_token=fencing_token,
            policy_decision_id=decision_id,
            link_ttl_seconds=link_ttl_seconds,
            worker_id=EXECUTOR_WORKER_ID,
        )

    operation.__name__ = "executor_operation"
    return operation
