"""Execution dispatch and idempotency.

The one sequence this module exists to make crash-safe:

    TXN 1  (no network)  -> COMMIT -> provider call -> TXN 2 (no network)

The provider call is never inside a transaction. TXN 1 commits the idempotency
key before any byte reaches the provider (I2), and every row write rides inside
`transition()`'s `side_effects` so a stale fencing token writes nothing at all.

Out of scope: reconciliation, adoption after the fact, verification, revenue,
policy, diagnosis, human review. Breaker *state* changes belong to a separate
monitor job -- see `breaker.py`.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from reclaim.domain import breaker as breaker_mod
from reclaim.domain.leases import fenced_transition
from reclaim.domain.states import CaseState
from reclaim.domain.transitions import transition
from reclaim.provider.contract import (
    CreateLinkResult,
    Customer,
    PaymentProvider,
    ProviderOutcome,
    RetryChargeUnsupported,
)

KEY_PREFIX = "rcv_"
KEY_BODY_LENGTH = 26

# action_deadline_at = expire_by + 10 minutes, satisfying
# ck_deadline_after_provider. Bookkeeping only: a deadline that has passed is
# never evidence the customer failed to pay, so nothing may read it that way.
ACTION_DEADLINE_GRACE = timedelta(minutes=10)

CREATE_PAYMENT_LINK = "CREATE_PAYMENT_LINK"
# The enum value exists but the executor must never dispatch it.
ACTION_RETRY_CHARGE = "RETRY_CHARGE"
OPERATION_CREATE_LINK = "create_payment_link"

OPEN_ACTION_STATUSES = ("PROPOSED", "LIVE", "UNRESOLVED")

# ---------------------------------------------------------------------------
# Outcome mapping (TXN 2)
# ---------------------------------------------------------------------------

# One enum member per real provider outcome. The exact ProviderOutcome
# is still mirrored into response_body/audit detail for callers that want it
# without a column lookup.
_REQUEST_OUTCOME = {
    ProviderOutcome.ACCEPTED: "ACCEPTED",
    ProviderOutcome.DUPLICATE_REFERENCE: "DUPLICATE_REFERENCE",
    ProviderOutcome.REJECTED: "REJECTED",
    ProviderOutcome.TRANSPORT_ERROR: "TRANSPORT_ERROR",
    ProviderOutcome.TIMEOUT: "TIMEOUT",
    ProviderOutcome.PROVIDER_ERROR: "PROVIDER_ERROR",
    ProviderOutcome.RATE_LIMITED: "RATE_LIMITED",
    ProviderOutcome.UNPARSEABLE: "UNPARSEABLE",
    ProviderOutcome.AUTH_ERROR: "AUTH_ERROR",
    ProviderOutcome.UNKNOWN: "UNKNOWN",
}

_ATTEMPT_STATE = {
    ProviderOutcome.ACCEPTED: "ACCEPTED",
    ProviderOutcome.DUPLICATE_REFERENCE: "ACCEPTED",
    ProviderOutcome.REJECTED: "REJECTED",
    # Zero bytes written: the provider never saw the request, so nothing was
    # created. There is no ambiguity to preserve -- the absence of a request
    # is itself the evidence.
    ProviderOutcome.TRANSPORT_ERROR: "REJECTED",
}

_ACTION_STATUS = {
    ProviderOutcome.ACCEPTED: "LIVE",
    ProviderOutcome.DUPLICATE_REFERENCE: "LIVE",
    ProviderOutcome.REJECTED: "TERMINAL_FAILED",
    ProviderOutcome.TRANSPORT_ERROR: "TERMINAL_FAILED",
}

_CASE_TARGET = {
    ProviderOutcome.ACCEPTED: CaseState.AWAITING_CUSTOMER,
    ProviderOutcome.DUPLICATE_REFERENCE: CaseState.AWAITING_CUSTOMER,
    ProviderOutcome.REJECTED: CaseState.ATTEMPT_FAILED,
    ProviderOutcome.TRANSPORT_ERROR: CaseState.ATTEMPT_FAILED,
}


def request_outcome_for(outcome: ProviderOutcome) -> str:
    return _REQUEST_OUTCOME[outcome]


def attempt_state_for(outcome: ProviderOutcome) -> str:
    """Anything not positively resolved is UNKNOWN. Never inferred to failure."""
    return _ATTEMPT_STATE.get(outcome, "UNKNOWN")


def action_status_for(outcome: ProviderOutcome) -> str:
    """An unresolved mechanism stays open; it never becomes TERMINAL without evidence."""
    return _ACTION_STATUS.get(outcome, "UNRESOLVED")


def case_target_for(outcome: ProviderOutcome) -> CaseState:
    """Unknown outcomes go to AMBIGUOUS, never ATTEMPT_FAILED (I3)."""
    return _CASE_TARGET.get(outcome, CaseState.AMBIGUOUS)


def resolve_attempt_budget(
    conn: psycopg.Connection, case_id: int, fencing_token: int
) -> tuple[int, int] | None:
    """`(attempt_count, max_attempts)` for a case in ATTEMPT_FAILED, fenced.

    This is the one column pair the increment in `dispatch` (TXN 1, below)
    writes and never reads back for routing -- routing after a failed attempt
    is a separate concern from spending the budget. `attempt_count` can never
    exceed `max_attempts` (`ck_attempt_budget`), so a caller comparing the two
    sees a closed, exhaustive pair: strictly less, or exactly equal.

    Returns None when the case is no longer ATTEMPT_FAILED under this token --
    another worker already reclaimed it. The caller discards its work rather
    than routing on a read that is no longer current, the same contract every
    fenced write already honours; it does not retry under a fresh token.
    """
    row = conn.execute(
        """
        SELECT attempt_count, max_attempts
          FROM recovery_cases
         WHERE id = %s AND state = %s AND fencing_token = %s
         FOR UPDATE
        """,
        (case_id, CaseState.ATTEMPT_FAILED.value, fencing_token),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), int(row[1])


class BudgetExhausted(Exception):
    """No attempt row is created and no network call happens."""

    def __init__(self, case_id: int) -> None:
        self.case_id = case_id
        super().__init__(f"attempt budget exhausted or stale token for case {case_id}")


class DispatchAborted(Exception):
    """TXN 1 could not commit its transition (stale token / wrong state)."""

    def __init__(self, case_id: int) -> None:
        self.case_id = case_id
        super().__init__(f"dispatch aborted for case {case_id}")


class ActionTypeUnsupported(Exception):
    """The executor was handed an action it must not dispatch.

    RETRY_CHARGE raises RetryChargeUnsupported instead; this covers the rest of
    the `action_type` enum (ESCALATE), which is a routing verdict rather than a
    dispatchable financial action.
    """

    def __init__(self, case_id: int, action_type: str) -> None:
        self.case_id = case_id
        self.action_type = action_type
        super().__init__(
            f"action_type {action_type!r} is not dispatchable (case {case_id})"
        )


@dataclass(frozen=True)
class Prepared:
    """Committed state after TXN 1. Everything the provider call needs."""

    case_id: int
    action_id: int
    attempt_id: int
    request_id: int
    idempotency_key: str
    amount_minor: int
    currency: str
    customer_ref: str
    expire_by: int
    attempt_count: int


@dataclass(frozen=True)
class DispatchResult:
    prepared: Prepared
    outcome: ProviderOutcome
    case_state: CaseState
    applied: bool
    provider_correlation_id: str | None


def new_idempotency_key() -> str:
    """"rcv_" + base32(uuid4())[:26]. 30 chars, inside the provider's 40-char cap."""
    body = base64.b32encode(uuid.uuid4().bytes).decode("ascii").rstrip("=")
    return f"{KEY_PREFIX}{body[:KEY_BODY_LENGTH]}"


# ---------------------------------------------------------------------------
# TXN 1
# ---------------------------------------------------------------------------


def prepare_dispatch(
    conn: psycopg.Connection,
    case_id: int,
    *,
    fencing_token: int,
    policy_decision_id: int,
    link_ttl_seconds: int,
    worker_id: str | None = None,
    idempotency_key: str | None = None,
) -> Prepared:
    """TXN 1: commits before any network call. No provider access here.

    Entry states:
      ACTION_READY — automated path
      ESCALATED — only when an open PROPOSED action exists; never merely because
                  the case is ESCALATED.
    """
    key = idempotency_key or new_idempotency_key()

    halted = False

    with conn.transaction():
        case_row = conn.execute(
            """
            SELECT state FROM recovery_cases
             WHERE id = %s
             FOR UPDATE
            """,
            (case_id,),
        ).fetchone()
        if case_row is None:
            raise DispatchAborted(case_id)
        from_state = CaseState(str(case_row[0]))

        # An open PROPOSED action can exist on either entry path, and
        # _acquire_action would promote it in both. Read its type once, here.
        open_action = conn.execute(
            """
            SELECT id, action_type FROM recovery_actions
             WHERE case_id = %s AND status = 'PROPOSED'
             LIMIT 1
            """,
            (case_id,),
        ).fetchone()

        if from_state is CaseState.ESCALATED:
            if open_action is None:
                raise DispatchAborted(case_id)
        elif from_state is not CaseState.ACTION_READY:
            raise DispatchAborted(case_id)

        # RETRY_CHARGE must never dispatch. Checked here -- before the breaker
        # gate, before the budget claim, before any row is written -- so a
        # refused action promotes nothing, spends no budget, creates no attempt
        # or provider_request, and never reaches the network. Policy evaluation
        # remaps RETRY_CHARGE before it reaches review, and review itself
        # refuses it; this is the third and final guard, at the dispatch layer.
        if open_action is not None:
            proposed_type = str(open_action[1])
            if proposed_type != CREATE_PAYMENT_LINK:
                if proposed_type == ACTION_RETRY_CHARGE:
                    raise RetryChargeUnsupported(
                        f"case {case_id} holds a PROPOSED {proposed_type} action; "
                        "RETRY_CHARGE is not dispatchable: no safe provider implementation exists"
                    )
                raise ActionTypeUnsupported(case_id, proposed_type)

        # 1. Breaker gate first, under FOR UPDATE so concurrent workers serialise.
        #    The gate must precede the budget claim: an attempt that consumed
        #    budget and never dispatched would waste the case's limited retries
        #    on nothing. Ordering it after would also force a rollback that
        #    discards the HALTED transition itself, making the halt and the
        #    unspent budget mutually exclusive.
        #
        #    ESCALATED + OPEN aborts with no state change (no HALTED), so an
        #    approved case keeps its review; ACTION_READY + OPEN halts.
        breaker = breaker_mod.read_breaker(conn, for_update=True)
        if breaker.is_open:
            if from_state is CaseState.ACTION_READY:
                _halt_for_breaker(conn, case_id, fencing_token, worker_id)
                halted = True
            else:
                raise breaker_mod.BreakerOpen(case_id)

        if not halted:
            # 2. Claim the attempt budget: its row lock serialises concurrent
            #    dispatchers on this case, and zero rows means exhausted or stale.
            claimed = conn.execute(
                """
                UPDATE recovery_cases
                   SET attempt_count = attempt_count + 1,
                       updated_at = now()
                 WHERE id = %s
                   AND attempt_count < max_attempts
                   AND fencing_token = %s
                RETURNING attempt_count
                """,
                (case_id, fencing_token),
            ).fetchone()
            if claimed is None:
                raise BudgetExhausted(case_id)
            attempt_count = int(claimed[0])

            money = _obligation_money(conn, case_id)
            expire_by = int(
                (
                    datetime.now(timezone.utc) + timedelta(seconds=link_ttl_seconds)
                ).timestamp()
            )
            created: dict[str, int] = {}

            def _prepare(inner: psycopg.Connection) -> None:
                action_id = _acquire_action(
                    inner,
                    case_id=case_id,
                    policy_decision_id=policy_decision_id,
                    expire_by=expire_by,
                )
                attempt_id = _insert_attempt(
                    inner,
                    case_id=case_id,
                    action_id=action_id,
                    attempt_no=attempt_count,
                    key=key,
                    money=money,
                )
                request_id = _insert_request(
                    inner,
                    attempt_id=attempt_id,
                    key=key,
                    money=money,
                    expire_by=expire_by,
                )
                _audit(
                    inner,
                    event_type="provider_request_sent",
                    case_id=case_id,
                    action_id=action_id,
                    attempt_id=attempt_id,
                    provider_request_id=request_id,
                    worker_id=worker_id,
                    fencing_token=fencing_token,
                    reason_code="provider_request_sent",
                    detail={
                        "operation": OPERATION_CREATE_LINK,
                        "request_no": 1,
                        # The reference and action type must be reconstructable
                        # from the audit trail alone, without querying live
                        # production tables.
                        "provider_reference": key,
                        "idempotency_key": key,
                        "action_type": CREATE_PAYMENT_LINK,
                        "amount_minor": money["amount_minor"],
                        "currency": money["currency"],
                    },
                )
                created.update(
                    action_id=action_id, attempt_id=attempt_id, request_id=request_id
                )

            applied = transition(
                conn,
                case_id,
                from_state,
                CaseState.EXECUTING,
                fencing_token,
                "execution_dispatch",
                side_effects=_prepare,
            )
            if not applied:
                raise DispatchAborted(case_id)

    # Raised after COMMIT so the HALTED transition survives the abort.
    if halted:
        raise breaker_mod.BreakerOpen(case_id)

    return Prepared(
        case_id=case_id,
        action_id=created["action_id"],
        attempt_id=created["attempt_id"],
        request_id=created["request_id"],
        idempotency_key=key,
        amount_minor=money["amount_minor"],
        currency=money["currency"],
        customer_ref=money["customer_ref"],
        expire_by=expire_by,
        attempt_count=attempt_count,
    )


def _halt_for_breaker(
    conn: psycopg.Connection, case_id: int, fencing_token: int, worker_id: str | None
) -> bool:
    """ACTION_READY -> HALTED, fenced.

    Uses fenced_transition rather than bare transition so a stale token is
    *audited* as well as rejected, consistent with every other worker's
    stale-write handling. The caller's BreakerOpen behaviour is unchanged; only
    the audit trail gains the missing evidence.
    """
    return fenced_transition(
        conn,
        case_id,
        CaseState.ACTION_READY,
        CaseState.HALTED,
        fencing_token,
        "breaker_open",
        worker_id=worker_id,
    )


def _obligation_money(conn: psycopg.Connection, case_id: int) -> dict[str, Any]:
    """The amount is copied from the obligation. No caller may supply one."""
    row = conn.execute(
        """
        SELECT o.amount_minor, o.currency, o.customer_ref
          FROM recovery_cases c
          JOIN financial_obligations o ON o.id = c.obligation_id
         WHERE c.id = %s
        """,
        (case_id,),
    ).fetchone()
    assert row is not None, f"case {case_id} has no obligation"
    return {"amount_minor": int(row[0]), "currency": str(row[1]), "customer_ref": str(row[2])}


def _acquire_action(
    conn: psycopg.Connection, *, case_id: int, policy_decision_id: int, expire_by: int
) -> int:
    """Promote an open PROPOSED action, else create a LIVE one.

    Human review creates a PROPOSED action ahead of dispatch; a bare INSERT
    here would collide with that row under uq_case_one_open_action.
    Promote-or-create is the only resolution that keeps both paths and the
    index intact. I5 holds either way: exactly one open action per case.
    """
    provider_expires_at = datetime.fromtimestamp(expire_by, tz=timezone.utc)
    deadline = provider_expires_at + ACTION_DEADLINE_GRACE

    # The action_type predicate is defence in depth: prepare_dispatch already
    # refused a non-CREATE_PAYMENT_LINK action before any write. If that guard
    # were ever bypassed, promotion cannot select the row, the INSERT below runs
    # instead, and uq_case_one_open_action turns it into a database error rather
    # than a payment link created under a lying action_type.
    promoted = conn.execute(
        """
        UPDATE recovery_actions
           SET status = 'LIVE',
               provider_expires_at = %s,
               action_deadline_at = %s
         WHERE case_id = %s
           AND status = 'PROPOSED'
           AND action_type = %s
        RETURNING id
        """,
        (provider_expires_at, deadline, case_id, CREATE_PAYMENT_LINK),
    ).fetchone()
    if promoted is not None:
        return int(promoted[0])

    seq = conn.execute(
        "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM recovery_actions WHERE case_id = %s",
        (case_id,),
    ).fetchone()
    assert seq is not None
    inserted = conn.execute(
        """
        INSERT INTO recovery_actions (
            case_id, action_type, status, sequence_no, policy_decision_id,
            provider_expires_at, action_deadline_at
        ) VALUES (%s, %s, 'LIVE', %s, %s, %s, %s)
        RETURNING id
        """,
        (case_id, CREATE_PAYMENT_LINK, int(seq[0]), policy_decision_id,
         provider_expires_at, deadline),
    ).fetchone()
    assert inserted is not None
    return int(inserted[0])


def _insert_attempt(
    conn: psycopg.Connection,
    *,
    case_id: int,
    action_id: int,
    attempt_no: int,
    key: str,
    money: dict[str, Any],
) -> int:
    row = conn.execute(
        """
        INSERT INTO execution_attempts (
            action_id, case_id, attempt_no, idempotency_key, provider_reference,
            state, amount_minor, currency
        ) VALUES (%s, %s, %s, %s, %s, 'PREPARED', %s, %s)
        RETURNING id
        """,
        (
            action_id,
            case_id,
            attempt_no,
            key,
            key,  # provider_reference IS the idempotency key, deliberately.
            money["amount_minor"],
            money["currency"],
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _insert_request(
    conn: psycopg.Connection,
    *,
    attempt_id: int,
    key: str,
    money: dict[str, Any],
    expire_by: int,
) -> int:
    body = {
        "operation": OPERATION_CREATE_LINK,
        "reference_id": key,
        "amount": money["amount_minor"],
        "currency": money["currency"],
        "expire_by": expire_by,
    }
    row = conn.execute(
        """
        INSERT INTO provider_requests (
            attempt_id, operation, request_no, idempotency_key,
            request_body, outcome
        ) VALUES (%s, %s, 1, %s, %s, 'IN_FLIGHT')
        RETURNING id
        """,
        (attempt_id, OPERATION_CREATE_LINK, key, Jsonb(body)),
    ).fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# Network boundary
# ---------------------------------------------------------------------------


def call_provider(provider: PaymentProvider, prepared: Prepared) -> CreateLinkResult:
    """Outside every transaction. Never auto-retries a dispatch."""
    return provider.create_payment_link(
        reference_id=prepared.idempotency_key,
        amount_minor=prepared.amount_minor,
        currency=prepared.currency,
        customer=_customer_for(prepared.customer_ref),
        expire_by=prepared.expire_by,
    )


def _customer_for(customer_ref: str) -> Customer:
    """The adapter refuses to guess a customer, so the mapping happens here."""
    if "@" in customer_ref:
        return Customer(email=customer_ref)
    if customer_ref.startswith("+"):
        return Customer(contact=customer_ref)
    return Customer(name=customer_ref, email=f"{customer_ref}@invalid.example")


# ---------------------------------------------------------------------------
# TXN 2
# ---------------------------------------------------------------------------


def settle_dispatch(
    conn: psycopg.Connection,
    prepared: Prepared,
    result: CreateLinkResult,
    *,
    fencing_token: int,
    worker_id: str | None = None,
    expected_state: CaseState = CaseState.EXECUTING,
) -> DispatchResult:
    """TXN 2: no network access here.

    `expected_state` is EXECUTING for a normal dispatch. A bounded same-key
    re-POST during reconciliation settles from RECONCILING instead, reusing
    this mapping rather than duplicating it.
    """
    outcome = result.outcome
    target = case_target_for(outcome)

    with conn.transaction():
        breaker_mod.record_execution_outcome(conn, outcome)

        def _settle(inner: psycopg.Connection) -> None:
            _finish_request(inner, prepared, result)
            _finish_attempt(inner, prepared, outcome)
            _finish_action(inner, prepared, outcome)
            _audit(
                inner,
                event_type="provider_response_received",
                case_id=prepared.case_id,
                action_id=prepared.action_id,
                attempt_id=prepared.attempt_id,
                provider_request_id=prepared.request_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                reason_code=f"provider_{outcome.value.lower()}",
                provider_correlation_id=result.provider_correlation_id,
                detail={
                    "provider_outcome": outcome.value,
                    "http_status": result.http_status,
                    "error_class": result.error_class.value if result.error_class else None,
                    "provider_reference": prepared.idempotency_key,
                    "action_type": CREATE_PAYMENT_LINK,
                    "attempt_state": attempt_state_for(outcome),
                    "action_status": action_status_for(outcome),
                    "target_state": target.value,
                },
            )

        applied = fenced_transition(
            conn,
            prepared.case_id,
            expected_state,
            target,
            fencing_token,
            f"provider_{outcome.value.lower()}",
            worker_id=worker_id,
            side_effects=_settle,
        )
        if not applied:
            # A stale token correctly prevents the write-back, but the provider
            # DID answer and we DID read it. Without this the trail records
            # only stale_write_rejected and silently loses what the provider
            # actually said -- the one piece of evidence a forensic reader most
            # needs when two workers raced over money.
            #
            # This records evidence only. It applies no state, touches no
            # attempt, action or request row, and changes no outcome: `applied`
            # is still False and the caller's contract is unchanged.
            _audit(
                conn,
                event_type="provider_response_observed",
                case_id=prepared.case_id,
                action_id=prepared.action_id,
                attempt_id=prepared.attempt_id,
                provider_request_id=prepared.request_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                reason_code="provider_response_not_applied",
                provider_correlation_id=result.provider_correlation_id,
                detail={
                    "provider_outcome": outcome.value,
                    "http_status": result.http_status,
                    "provider_reference": prepared.idempotency_key,
                    "action_type": CREATE_PAYMENT_LINK,
                    "would_have_targeted": target.value,
                    "applied": False,
                    "discarded_because": "stale_fencing_token",
                },
            )

    return DispatchResult(
        prepared=prepared,
        outcome=outcome,
        case_state=target,
        applied=applied,
        provider_correlation_id=result.provider_correlation_id,
    )


def _finish_request(
    conn: psycopg.Connection, prepared: Prepared, result: CreateLinkResult
) -> None:
    # The exact ProviderOutcome is mirrored here as well as in the enum column,
    # so a reader never has to re-derive it from http_status.
    body: dict[str, Any] = {"provider_outcome": result.outcome.value}
    if result.response_body is not None:
        body["response"] = result.response_body
    if result.error_code:
        body["error_code"] = result.error_code
    if result.error_description:
        body["error_description"] = result.error_description

    conn.execute(
        """
        UPDATE provider_requests
           SET outcome = %s,
               http_status = %s,
               response_body = %s,
               provider_correlation_id = %s,
               completed_at = now()
         WHERE id = %s
        """,
        (
            request_outcome_for(result.outcome),
            result.http_status,
            Jsonb(body),
            result.provider_correlation_id,
            prepared.request_id,
        ),
    )


def _finish_attempt(
    conn: psycopg.Connection, prepared: Prepared, outcome: ProviderOutcome
) -> None:
    state = attempt_state_for(outcome)
    settled = state in {"ACCEPTED", "REJECTED"}
    conn.execute(
        """
        UPDATE execution_attempts
           SET state = %s,
               settled_at = CASE WHEN %s THEN now() ELSE settled_at END
         WHERE id = %s
        """,
        (state, settled, prepared.attempt_id),
    )


def _finish_action(
    conn: psycopg.Connection, prepared: Prepared, outcome: ProviderOutcome
) -> None:
    status = action_status_for(outcome)
    # ck_resolved_shape: resolved_at is set iff the status is terminal.
    resolved = status in {"TERMINAL_SUCCESS", "TERMINAL_FAILED", "SUPERSEDED"}
    conn.execute(
        """
        UPDATE recovery_actions
           SET status = %s,
               resolved_at = CASE WHEN %s THEN now() ELSE resolved_at END
         WHERE id = %s
        """,
        (status, resolved, prepared.action_id),
    )


# ---------------------------------------------------------------------------
# Full sequence
# ---------------------------------------------------------------------------


def dispatch(
    conn: psycopg.Connection,
    case_id: int,
    *,
    provider: PaymentProvider,
    fencing_token: int,
    policy_decision_id: int,
    link_ttl_seconds: int,
    worker_id: str | None = None,
) -> DispatchResult:
    """End to end: TXN 1, COMMIT, network, TXN 2.

    Raises BudgetExhausted / BreakerOpen / DispatchAborted before any network
    call. Once TXN 1 commits, every provider outcome is recorded -- there is no
    path that dispatches and then silently drops the result.
    """
    prepared = prepare_dispatch(
        conn,
        case_id,
        fencing_token=fencing_token,
        policy_decision_id=policy_decision_id,
        link_ttl_seconds=link_ttl_seconds,
        worker_id=worker_id,
    )
    result = call_provider(provider, prepared)
    return settle_dispatch(
        conn, prepared, result, fencing_token=fencing_token, worker_id=worker_id
    )


def _audit(
    conn: psycopg.Connection,
    *,
    event_type: str,
    case_id: int,
    action_id: int | None = None,
    attempt_id: int | None = None,
    provider_request_id: int | None = None,
    worker_id: str | None = None,
    fencing_token: int | None = None,
    reason_code: str,
    provider_correlation_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_events (
            event_type, obligation_id, case_id, action_id, attempt_id,
            provider_request_id, worker_id, fencing_token, reason_code,
            provider_correlation_id, detail
        )
        SELECT %s, c.obligation_id, %s, %s, %s, %s, %s, %s, %s, %s, %s
          FROM recovery_cases c WHERE c.id = %s
        """,
        (
            event_type,
            case_id,
            action_id,
            attempt_id,
            provider_request_id,
            worker_id,
            fencing_token,
            reason_code,
            provider_correlation_id,
            Jsonb(detail or {}),
            case_id,
        ),
    )
