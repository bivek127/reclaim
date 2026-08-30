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

from reclaim.config import load_ollama_config
from reclaim.domain.diagnosis import diagnose_case
from reclaim.domain.execution import dispatch, resolve_attempt_budget
from reclaim.domain.leases import fenced_transition
from reclaim.domain.policy import (
    apply_policy,
    authorising_decision_id,
    load_policy_inputs,
    resolve_conflicting_history,
)
from reclaim.domain.reconciliation import reconcile_case
from reclaim.domain.states import CaseState
from reclaim.domain.verification import verify_case
from reclaim.llm.client import LlmClient, OllamaClient
from reclaim.provider.config import load_provider_config
from reclaim.provider.contract import PaymentProvider
from reclaim.provider.razorpay import RazorpayAdapter

CASE_WORKER_WORKER_ID = "case-worker"
DIAGNOSIS_WORKER_ID = "diagnosis"
POLICY_WORKER_ID = "policy"
EXECUTOR_WORKER_ID = "executor"
RECONCILER_WORKER_ID = "reconciler"
VERIFIER_WORKER_ID = "verifier"

# The forward edge out of each state the case worker owns, and why the case
# moved. ENRICHING also has a legal edge to ESCALATED, but that one belongs to
# TTL expiry, so the forward target is named rather than derived from the legal
# set. Legality is still the state machine's to enforce: `fenced_transition`
# raises on a pair it does not allow, so a wrong target here cannot write.
#
# `enrichment_pass_through` records in the audit trail that no enrichment was
# performed, which is the current contract rather than a missing step.
#
# ATTEMPT_FAILED is deliberately absent from this map: its target depends on
# the case's own attempt budget, not on the state alone, so it is routed by
# the operation function below rather than a fixed lookup.
CASE_WORKER_ADVANCE: dict[CaseState, tuple[CaseState, str]] = {
    CaseState.NEW: (CaseState.ENRICHING, "case_worker_started"),
    CaseState.ENRICHING: (CaseState.DIAGNOSING, "enrichment_pass_through"),
}

REASON_ATTEMPT_FAILED_BUDGET_REMAINS = "attempt_failed_budget_remains"
REASON_ATTEMPT_FAILED_BUDGET_EXHAUSTED = "attempt_failed_budget_exhausted"

ProviderFactory = Callable[[], PaymentProvider]
LlmFactory = Callable[[], LlmClient]


def default_provider() -> PaymentProvider:
    """The configured Razorpay adapter, built from existing provider config."""
    return RazorpayAdapter(load_provider_config())


def default_llm() -> LlmClient:
    """The configured Ollama client, built from existing operational config."""
    return OllamaClient(load_ollama_config())


def case_worker_operation() -> Any:
    """Advance one case along the lifecycle edges the case worker owns.

    NEW and ENRICHING are mechanical: entry into ENRICHING, and ENRICHING to
    DIAGNOSING. Enrichment performs no work of its own -- diagnosis reads only
    obligation and case fields that exist as soon as the case does -- so the
    transition is the whole operation.

    ATTEMPT_FAILED is routing, not a transition to a fixed target: policy gets
    to decide a fresh action if the case's attempt budget remains, or the case
    is escalated if it doesn't. This is not a retry of the failed attempt --
    no dispatch happens here, and no attempt budget is spent by routing alone.

    A rejected write is not an error. `fenced_transition` returns False when
    another worker holds a newer token, and the contract for that is to discard
    the work rather than retry: there is no work to discard here, and the case
    stays where it was for whoever holds the newer claim.
    """

    def operation(
        conn: psycopg.Connection,
        case_id: int,
        *,
        fencing_token: int,
        claimed_state: CaseState | None = None,
        **_: Any,
    ) -> Any:
        if claimed_state is CaseState.ATTEMPT_FAILED:
            return _route_attempt_failed(conn, case_id, fencing_token)

        advance = CASE_WORKER_ADVANCE.get(claimed_state)  # type: ignore[arg-type]
        if advance is None:
            # The runner claimed a state this job does not own. Refusing keeps
            # an undefined edge from being invented at runtime.
            raise RuntimeError(
                f"case {case_id} claimed in {claimed_state} has no case-worker edge"
            )
        target, reason = advance
        return fenced_transition(
            conn,
            case_id,
            claimed_state,  # type: ignore[arg-type]
            target,
            fencing_token,
            reason,
            worker_id=CASE_WORKER_WORKER_ID,
        )

    operation.__name__ = "case_worker_operation"
    return operation


def _route_attempt_failed(
    conn: psycopg.Connection, case_id: int, fencing_token: int
) -> bool:
    budget = resolve_attempt_budget(conn, case_id, fencing_token)
    if budget is None:
        # Another worker already reclaimed the case under a newer token;
        # there is nothing current left to route.
        return False
    attempt_count, max_attempts = budget

    if attempt_count < max_attempts:
        return fenced_transition(
            conn,
            case_id,
            CaseState.ATTEMPT_FAILED,
            CaseState.POLICY_EVAL,
            fencing_token,
            REASON_ATTEMPT_FAILED_BUDGET_REMAINS,
            worker_id=CASE_WORKER_WORKER_ID,
        )

    def _escalate(inner: psycopg.Connection) -> None:
        from reclaim.domain.review import on_entered_escalated

        # No prior policy decision to attach to -- this escalation is caused
        # by the attempt budget, not by a policy verdict -- so a fresh
        # provenance row is inserted, the same way TTL and deadline expiry
        # already escalate with no decision to reuse.
        on_entered_escalated(
            inner,
            case_id,
            reason_code=REASON_ATTEMPT_FAILED_BUDGET_EXHAUSTED,
            policy_decision_id=None,
        )

    return fenced_transition(
        conn,
        case_id,
        CaseState.ATTEMPT_FAILED,
        CaseState.ESCALATED,
        fencing_token,
        REASON_ATTEMPT_FAILED_BUDGET_EXHAUSTED,
        worker_id=CASE_WORKER_WORKER_ID,
        side_effects=_escalate,
    )


def diagnosis_operation(llm: LlmFactory = default_llm) -> Any:
    """Classify one claimed case's failure and hand it to policy.

    `diagnose_case` owns the retry ladder, the deterministic fallback for an
    unreachable or malformed model, the `diagnoses` row and the fenced
    transition to POLICY_EVAL. It never touches `attempt_count`: a model that
    cannot answer must not spend a case's financial budget.

    The case-level function is the one wired here rather than `diagnose_once`.
    The runner already holds the claim, and `diagnose_once` claims a case of
    its own -- against a lease that has not expired it would find nothing and
    the job would silently never diagnose.

    The client is built on the tick that needs it, for the same reason the
    provider is: a model that is down must not stop the job table from loading.
    """

    def operation(
        conn: psycopg.Connection, case_id: int, *, fencing_token: int, **_: Any
    ) -> Any:
        return diagnose_case(
            conn,
            case_id,
            llm=llm(),
            fencing_token=fencing_token,
            worker_id=DIAGNOSIS_WORKER_ID,
        )

    operation.__name__ = "diagnosis_operation"
    return operation


def policy_operation() -> Any:
    """Evaluate one claimed case's diagnosis and act on the verdict.

    Three domain calls, composed in the order the verdict depends on:
    `resolve_conflicting_history` first, because `load_policy_inputs` needs it
    as an input; then `load_policy_inputs`, which reads the case's facts and
    its most recent diagnosis; then `apply_policy`, which evaluates those
    facts, writes the `policy_decisions` row, and performs the fenced
    transition to ACTION_READY, ESCALATED, or VERIFIED_FAILED -- all inside
    its own transaction.

    `apply_policy` runs `evaluate()` itself as part of that atomic write,
    because the transition target depends on the verdict. Calling it here
    first would only compute the same pure function twice.

    No SQL of its own: every read and write belongs to the three domain calls.
    """

    def operation(
        conn: psycopg.Connection, case_id: int, *, fencing_token: int, **_: Any
    ) -> Any:
        conflicting_history = resolve_conflicting_history(conn, case_id)
        facts, diagnosis_id = load_policy_inputs(conn, case_id, conflicting_history)
        return apply_policy(
            conn,
            case_id,
            facts=facts,
            diagnosis_id=diagnosis_id,
            fencing_token=fencing_token,
            worker_id=POLICY_WORKER_ID,
        )

    operation.__name__ = "policy_operation"
    return operation


def reconciler_operation(provider: ProviderFactory = default_provider) -> Any:
    """Resolve one ambiguous case against the provider.

    `reconcile_case` reads its own poll and re-POST budgets from operational
    configuration and owns both transactions around the network call.
    """

    def operation(
        conn: psycopg.Connection, case_id: int, *, fencing_token: int, **_: Any
    ) -> Any:
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

    def operation(
        conn: psycopg.Connection, case_id: int, *, fencing_token: int, **_: Any
    ) -> Any:
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
    """Dispatch an action, whether policy or a human authorised it.

    Two entry paths, which `prepare_dispatch` already distinguishes:

    * ACTION_READY -- policy authorised the action, and the decision that did
      so is read through the domain's own accessor. Which decision authorises
      an action is a policy question; answering it with a query here would put
      that judgement in the runtime.
    * ESCALATED -- a reviewer approved it, and the PROPOSED action they created
      already carries its own decision. `_acquire_action` promotes that row and
      keeps its `policy_decision_id`, so no decision is looked up and none is
      passed: `None` says "not applicable" where a number would be invented.

    `dispatch` owns everything after that -- the attempt row, the idempotency
    key persisted before any network call, the breaker gate, the provider call
    and the outcome mapping. This adapter never retries: a dispatch that fails
    is the domain's recorded outcome, not something to attempt again.
    """

    def operation(
        conn: psycopg.Connection,
        case_id: int,
        *,
        fencing_token: int,
        claimed_state: CaseState | None = None,
        **_: Any,
    ) -> Any:
        decision_id: int | None = None
        if claimed_state is not CaseState.ESCALATED:
            decision_id = authorising_decision_id(conn, case_id)
            if decision_id is None:
                # A case cannot reach ACTION_READY without one, so this is a
                # data anomaly. Surfacing it releases the lease and leaves the
                # case for a human, rather than inventing an authorisation.
                raise RuntimeError(
                    f"case {case_id} is ACTION_READY with no authorising "
                    "policy decision"
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
