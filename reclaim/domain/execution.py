"""Execution dispatch and idempotency.

The one sequence this module exists to make crash-safe:

    TXN 1  (no network)  -> COMMIT -> provider call -> TXN 2 (no network)

The provider call is never inside a transaction. TXN 1 commits the idempotency
key before any byte reaches the provider (I2), and every row write rides inside
`transition()`'s `side_effects` so a stale fencing token writes nothing at all.

Out of scope: reconciliation, adoption after the fact, verification, revenue,
policy, diagnosis, human review. Breaker *state* changes belong to a separate
monitor job -- see `breaker.py` and ADR-011.
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
)

KEY_PREFIX = "rcv_"
KEY_BODY_LENGTH = 26

# action_deadline_at = expire_by + 10 minutes, satisfying
# ck_deadline_after_provider. Bookkeeping only -- per ADR-006 nothing may read
# the deadline's passing as terminal-failure evidence.
ACTION_DEADLINE_GRACE = timedelta(minutes=10)

CREATE_PAYMENT_LINK = "CREATE_PAYMENT_LINK"
OPERATION_CREATE_LINK = "create_payment_link"

OPEN_ACTION_STATUSES = ("PROPOSED", "LIVE", "UNRESOLVED")

# ---------------------------------------------------------------------------
# Outcome mapping (TXN 2)
# ---------------------------------------------------------------------------

# `request_outcome` has no member for several real provider outcomes. Per
# ADR-010 the enum extension is deferred until the read side needs additional
# values anyway. Until then these collapse onto TIMEOUT in the enum
# column while the exact ProviderOutcome is written losslessly to
# provider_requests.response_body and audit_events.detail.
_ENUM_FALLBACK = "TIMEOUT"

_REQUEST_OUTCOME = {
    ProviderOutcome.ACCEPTED: "ACCEPTED",
    ProviderOutcome.DUPLICATE_REFERENCE: "DUPLICATE_REFERENCE",
    ProviderOutcome.REJECTED: "REJECTED",
    ProviderOutcome.TRANSPORT_ERROR: "TRANSPORT_ERROR",
    ProviderOutcome.TIMEOUT: "TIMEOUT",
    ProviderOutcome.PROVIDER_ERROR: _ENUM_FALLBACK,
    ProviderOutcome.RATE_LIMITED: _ENUM_FALLBACK,
    ProviderOutcome.UNPARSEABLE: _ENUM_FALLBACK,
    ProviderOutcome.AUTH_ERROR: _ENUM_FALLBACK,
    ProviderOutcome.UNKNOWN: _ENUM_FALLBACK,
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
    """TXN 1: commits before any network call. No provider access here."""
    key = idempotency_key or new_idempotency_key()

    halted = False

    with conn.transaction():
        # 1. Breaker gate first, under FOR UPDATE so concurrent workers serialise.
        #    The gate must precede the budget claim: an attempt that consumed
        #    budget and never dispatched would waste the case's limited retries
        #    on nothing. Ordering it after would also force a rollback that
        #    discards the HALTED transition itself, making the halt and the
        #    unspent budget mutually exclusive.
        state = breaker_mod.read_breaker(conn, for_update=True)
        if state.is_open:
            _halt_for_breaker(conn, case_id, fencing_token, worker_id)
            halted = True

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
                    detail={"operation": OPERATION_CREATE_LINK, "request_no": 1},
                )
                created.update(
                    action_id=action_id, attempt_id=attempt_id, request_id=request_id
                )

            applied = transition(
                conn,
                case_id,
                CaseState.ACTION_READY,
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
) -> None:
    transition(
        conn,
        case_id,
        CaseState.ACTION_READY,
        CaseState.HALTED,
        fencing_token,
        "breaker_open",
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
    """Promote an open PROPOSED action, else create a LIVE one (ADR-011).

    Human review creates a PROPOSED action ahead of dispatch; a bare INSERT
    here would collide with that row under uq_case_one_open_action.
    Promote-or-create is the only resolution that keeps both paths and the
    index intact. I5 holds either way: exactly one open action per case.
    """
    provider_expires_at = datetime.fromtimestamp(expire_by, tz=timezone.utc)
    deadline = provider_expires_at + ACTION_DEADLINE_GRACE

    promoted = conn.execute(
        """
        UPDATE recovery_actions
           SET status = 'LIVE',
               provider_expires_at = %s,
               action_deadline_at = %s
         WHERE case_id = %s
           AND status = 'PROPOSED'
        RETURNING id
        """,
        (provider_expires_at, deadline, case_id),
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
            key,  # SPEC-2 / ADR-003: provider_reference IS the key.
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
) -> DispatchResult:
    """TXN 2: no network access here."""
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
                },
            )

        applied = fenced_transition(
            conn,
            prepared.case_id,
            CaseState.EXECUTING,
            target,
            fencing_token,
            f"provider_{outcome.value.lower()}",
            worker_id=worker_id,
            side_effects=_settle,
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
    # The exact ProviderOutcome is preserved here even when the enum column
    # collapses it (ADR-010). Nothing downstream needs to re-derive it.
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
