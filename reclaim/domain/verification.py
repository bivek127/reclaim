"""Verification and revenue recognition.

The one place in the system that writes `recovered_amount_minor`, and the only
module whose failure mode is inventing revenue that never arrived.

The evidence ladder is enforced literally: a webhook saying SUCCESS is *not*
payment verification. Two independently-sourced pieces of evidence must agree --

    evidence 1  a webhook correlated to the attempt by its provider_reference
    evidence 2  an independent provider GET by the persisted provider_reference

-- and the webhook payload is never reused as the second source.

Three things this module deliberately does NOT do:

  * resolve from a single source, however positive
  * manufacture a negative verification when the provider gave no evidence
  * let the provider's response determine the recognized amount

The amount comes from `execution_attempts.amount_minor`, copied at dispatch
from the obligation, and `guard_recovered_amount` independently re-checks it.

Runs as the `recovery_verifier` role. Out of scope: reconciliation, policy,
diagnosis, human review, and any financial action or attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from reclaim.config import lease_seconds_for
from reclaim.domain.leases import claim_next, fenced_transition
from reclaim.domain.states import CaseState
from reclaim.provider.contract import FetchOutcome, FetchResult, LinkStatus, PaymentProvider

# Attempt states that can still receive a payment. An attempt the provider
# accepted is ACCEPTED; PREPARED/IN_FLIGHT/UNKNOWN belong to reconciliation,
# not verification.
VERIFIABLE_ATTEMPT_STATES: tuple[str, ...] = ("ACCEPTED",)

# Correlation reads only events with this prefix; anything else is ignored.
PAYMENT_LINK_EVENT_PREFIX = "payment_link."

# Webhook event types that assert the payment succeeded / failed. Kept as
# provider-neutral suffixes -- no Razorpay branching lives here.
WEBHOOK_SUCCESS_SUFFIXES = ("paid",)
WEBHOOK_FAILURE_SUFFIXES = ("expired", "cancelled")

# The only provider status that corroborates a successful payment.
PROVIDER_PAID_STATUSES = frozenset({LinkStatus.PAID})


class VerificationBlocked(Exception):
    """The case cannot be verified as presented. Never a provider outcome."""


@dataclass(frozen=True)
class TrustedAttempt:
    """Financial facts from our own committed rows. The provider cannot alter these."""

    attempt_id: int
    action_id: int
    case_id: int
    provider_reference: str
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class WebhookEvidence:
    webhook_event_id: int
    event_type: str
    claims_success: bool

    @property
    def status_label(self) -> str:
        return "SUCCESS" if self.claims_success else "FAILED"


@dataclass(frozen=True)
class VerifyResult:
    case_id: int
    case_state: CaseState
    applied: bool
    agrees: bool | None = None
    verified_amount_minor: int = 0
    recovered: bool = False
    reason: str = ""
    had_webhook: bool = False
    fetch_outcome: FetchOutcome | None = None


# ---------------------------------------------------------------------------
# Trusted local facts
# ---------------------------------------------------------------------------


def trusted_attempt_for(conn: psycopg.Connection, case_id: int) -> TrustedAttempt:
    """The accepted attempt and its money. Amount/currency are never provider-sourced."""
    row = conn.execute(
        """
        SELECT ea.id, ea.action_id, ea.case_id, ea.provider_reference,
               ea.amount_minor, ea.currency
          FROM execution_attempts ea
         WHERE ea.case_id = %s
           AND ea.state = ANY(%s)
           AND ea.provider_reference IS NOT NULL
         ORDER BY ea.id DESC
         LIMIT 1
        """,
        (case_id, list(VERIFIABLE_ATTEMPT_STATES)),
    ).fetchone()
    if row is None:
        raise VerificationBlocked(f"case {case_id} has no accepted attempt to verify")
    return TrustedAttempt(
        attempt_id=int(row[0]),
        action_id=int(row[1]),
        case_id=int(row[2]),
        provider_reference=str(row[3]),
        amount_minor=int(row[4]),
        currency=str(row[5]),
    )


# ---------------------------------------------------------------------------
# Evidence 1 -- webhook correlation
# ---------------------------------------------------------------------------


def correlate_webhook(
    conn: psycopg.Connection, attempt: TrustedAttempt
) -> WebhookEvidence | None:
    """Try correlation by reference, then by correlation id. No match returns None.

    Correlation is a read over rows already stored durably; the webhook is
    untrusted input and contributes a status claim only. It can never supply
    the amount, the currency, or the verification verdict.
    """
    found = _correlate_by_reference(conn, attempt) or _correlate_by_correlation_id(
        conn, attempt
    )
    return found


def _correlate_by_reference(
    conn: psycopg.Connection, attempt: TrustedAttempt
) -> WebhookEvidence | None:
    """Rule 1: payload reference_id == execution_attempts.provider_reference."""
    row = conn.execute(
        """
        SELECT id, event_type
          FROM webhook_events
         WHERE signature_valid
           AND event_type LIKE %s
           AND payload #>> '{payload,payment_link,entity,reference_id}' = %s
         ORDER BY received_at DESC, id DESC
         LIMIT 1
        """,
        (f"{PAYMENT_LINK_EVENT_PREFIX}%", attempt.provider_reference),
    ).fetchone()
    return _as_evidence(row)


def _correlate_by_correlation_id(
    conn: psycopg.Connection, attempt: TrustedAttempt
) -> WebhookEvidence | None:
    """Correlate by provider_correlation_id, via a join.

    `provider_correlation_id` lives on `provider_requests`, not on the action,
    so matching "that action's correlation id" means reaching it through a join
    over the attempt rather than a direct column read.
    """
    row = conn.execute(
        """
        SELECT we.id, we.event_type
          FROM webhook_events we
          JOIN provider_requests pr
            ON pr.provider_correlation_id
                 = we.payload #>> '{payload,payment_link,entity,id}'
         WHERE we.signature_valid
           AND we.event_type LIKE %s
           AND pr.attempt_id = %s
           AND pr.provider_correlation_id IS NOT NULL
         ORDER BY we.received_at DESC, we.id DESC
         LIMIT 1
        """,
        (f"{PAYMENT_LINK_EVENT_PREFIX}%", attempt.attempt_id),
    ).fetchone()
    return _as_evidence(row)


def _as_evidence(row: Any) -> WebhookEvidence | None:
    if row is None:
        return None
    event_type = str(row[1])
    suffix = event_type.split(".", 1)[-1].lower()
    return WebhookEvidence(
        webhook_event_id=int(row[0]),
        event_type=event_type,
        claims_success=suffix in WEBHOOK_SUCCESS_SUFFIXES,
    )


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def compare(
    webhook: WebhookEvidence, fetch: FetchResult, attempt: TrustedAttempt
) -> tuple[bool | None, str]:
    """Return (agrees, reason). `agrees is None` means no evidence -- write nothing.

    Only one combination agrees: the webhook claims success, the provider
    independently reports PAID, and the paid amount and currency equal the
    trusted attempt's exactly.
    """
    if fetch.outcome is FetchOutcome.NO_EVIDENCE:
        # We do not know the provider truth; manufacturing a negative result
        # would be a claim we cannot support.
        return None, "verification_no_evidence"

    if fetch.outcome is FetchOutcome.NOT_FOUND:
        return False, "verification_provider_not_found"

    if not webhook.claims_success:
        # The provider fetch may say PAID, but our other source disagrees.
        return False, "verification_webhook_reports_failure"

    if fetch.link_status not in PROVIDER_PAID_STATUSES:
        # EXPIRED/CANCELLED: provider-side death is NOT terminal evidence of
        # non-payment (ADR-006). It merely disagrees with a successful verdict.
        return False, "verification_provider_not_paid"

    if fetch.amount_paid_minor is None:
        return False, "verification_amount_unknown"

    if fetch.amount_paid_minor != attempt.amount_minor:
        # An amount that disagrees with the attempt is a contradiction.
        return False, "verification_amount_mismatch"

    if fetch.currency != attempt.currency:
        # Currency is compared on every correlation. A missing currency is not
        # a match — it is unknown, and unknown does not verify.
        return False, "verification_currency_mismatch"

    return True, "verification_agreed"


# ---------------------------------------------------------------------------
# The verification transaction
# ---------------------------------------------------------------------------


def verify_case(
    conn: psycopg.Connection,
    case_id: int,
    *,
    provider: PaymentProvider,
    fencing_token: int,
    worker_id: str | None = None,
) -> VerifyResult:
    """One verification cycle. `conn` must be a recovery_verifier connection."""
    attempt = trusted_attempt_for(conn, case_id)

    webhook = correlate_webhook(conn, attempt)
    if webhook is None:
        # Row 9. No second-source comparison is possible, so no verification
        # row is written and the case stays put. A missing webhook is not a
        # disagreement.
        return VerifyResult(
            case_id=case_id,
            case_state=CaseState.AWAITING_CUSTOMER,
            applied=False,
            reason="verification_awaiting_webhook",
            had_webhook=False,
        )

    # --------------------- network boundary: read-only ----------------------
    fetch = provider.fetch_by_reference(reference_id=attempt.provider_reference)
    # ------------------------------------------------------------------------

    agrees, reason = compare(webhook, fetch, attempt)

    if agrees is None:
        # Retry next cycle. No row, no transition, no revenue.
        return VerifyResult(
            case_id=case_id,
            case_state=CaseState.AWAITING_CUSTOMER,
            applied=False,
            agrees=None,
            reason=reason,
            had_webhook=True,
            fetch_outcome=fetch.outcome,
        )

    target = (
        CaseState.VERIFIED_RECOVERED if agrees else CaseState.AMBIGUOUS
    )
    amount = attempt.amount_minor if agrees else 0

    with conn.transaction():
        # Lock the case and re-check the precondition BEFORE writing anything.
        #
        # fenced_transition returns False rather than raising, so without this
        # a rejected transition would still leave a committed verification row
        # claiming an agreement that was never applied -- falsifying the audit
        # trail and leaving an unapplied agreeing row that guard_recovered_amount
        # would happily accept later. The FOR UPDATE lock is held for the rest
        # of the transaction, so this is not check-then-act: nothing can change
        # the state or token between here and the transition below.
        if not _claimable(conn, case_id, fencing_token):
            # Reuse fenced_transition purely so the stale write is rejected and
            # audited exactly the way every other worker's is.
            fenced_transition(
                conn,
                case_id,
                CaseState.AWAITING_CUSTOMER,
                target,
                fencing_token,
                reason,
                worker_id=worker_id,
            )
            return VerifyResult(
                case_id=case_id,
                case_state=CaseState.AWAITING_CUSTOMER,
                applied=False,
                agrees=agrees,
                reason=reason,
                had_webhook=True,
                fetch_outcome=fetch.outcome,
            )

        # The verification row MUST precede the revenue UPDATE:
        # guard_recovered_amount fires BEFORE UPDATE and looks for an agreeing
        # row with an equal amount.
        _insert_verification(
            conn,
            attempt=attempt,
            webhook=webhook,
            fetch=fetch,
            agrees=agrees,
            amount=amount,
        )

        def _settle(inner: psycopg.Connection) -> None:
            if agrees:
                _write_revenue(inner, case_id=case_id, amount=amount)
            _audit(
                inner,
                case_id=case_id,
                attempt=attempt,
                webhook=webhook,
                fetch=fetch,
                agrees=agrees,
                amount=amount,
                reason=reason,
                worker_id=worker_id,
                fencing_token=fencing_token,
            )

        applied = fenced_transition(
            conn,
            case_id,
            CaseState.AWAITING_CUSTOMER,
            target,
            fencing_token,
            reason,
            worker_id=worker_id,
            side_effects=_settle,
        )

    return VerifyResult(
        case_id=case_id,
        case_state=target if applied else CaseState.AWAITING_CUSTOMER,
        applied=applied,
        agrees=agrees,
        verified_amount_minor=amount if applied else 0,
        recovered=bool(agrees and applied),
        reason=reason,
        had_webhook=True,
        fetch_outcome=fetch.outcome,
    )


def _claimable(conn: psycopg.Connection, case_id: int, fencing_token: int) -> bool:
    """Is this case still AWAITING_CUSTOMER under our token? Locks the row."""
    row = conn.execute(
        """
        SELECT 1 FROM recovery_cases
         WHERE id = %s AND state = %s AND fencing_token = %s
         FOR UPDATE
        """,
        (case_id, CaseState.AWAITING_CUSTOMER.value, fencing_token),
    ).fetchone()
    return row is not None


def _insert_verification(
    conn: psycopg.Connection,
    *,
    attempt: TrustedAttempt,
    webhook: WebhookEvidence,
    fetch: FetchResult,
    agrees: bool,
    amount: int,
) -> int:
    row = conn.execute(
        """
        INSERT INTO verifications (
            case_id, attempt_id, webhook_event_id, webhook_status,
            query_status, query_correlation_id, agrees, verified_amount_minor
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            attempt.case_id,
            attempt.attempt_id,
            webhook.webhook_event_id,
            webhook.status_label,
            fetch.link_status.value if fetch.link_status else fetch.outcome.value,
            fetch.provider_correlation_id,
            agrees,
            amount,
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _write_revenue(conn: psycopg.Connection, *, case_id: int, amount: int) -> None:
    """The only revenue write in the system. Amount comes from the attempt row."""
    conn.execute(
        "UPDATE recovery_cases SET recovered_amount_minor = %s WHERE id = %s",
        (amount, case_id),
    )


def _audit(
    conn: psycopg.Connection,
    *,
    case_id: int,
    attempt: TrustedAttempt,
    webhook: WebhookEvidence,
    fetch: FetchResult,
    agrees: bool,
    amount: int,
    reason: str,
    worker_id: str | None,
    fencing_token: int,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_events (
            event_type, obligation_id, case_id, action_id, attempt_id,
            worker_id, fencing_token, reason_code, provider_correlation_id, detail
        )
        SELECT 'verification_recorded', c.obligation_id, %s, %s, %s,
               %s, %s, %s, %s, %s
          FROM recovery_cases c WHERE c.id = %s
        """,
        (
            case_id,
            attempt.action_id,
            attempt.attempt_id,
            worker_id,
            fencing_token,
            reason,
            fetch.provider_correlation_id,
            Jsonb(
                {
                    "agrees": agrees,
                    "verified_amount_minor": amount,
                    "provider_reference": attempt.provider_reference,
                    "webhook_event_id": webhook.webhook_event_id,
                    "webhook_status": webhook.status_label,
                    "query_outcome": fetch.outcome.value,
                    "query_status": fetch.link_status.value if fetch.link_status else None,
                    "amount_paid_minor": fetch.amount_paid_minor,
                }
            ),
            case_id,
        ),
    )


# ---------------------------------------------------------------------------
# Job entry point: periodic poll on AWAITING_CUSTOMER, verifier role
# ---------------------------------------------------------------------------


def verify_once(
    conn: psycopg.Connection,
    *,
    provider: PaymentProvider,
    worker_id: str = "verifier",
    lease_seconds: int | None = None,
) -> VerifyResult | None:
    """Claim one AWAITING_CUSTOMER case and verify it. None when nothing claimable."""
    lease = lease_seconds or lease_seconds_for("verification")
    claim = claim_next(conn, CaseState.AWAITING_CUSTOMER, worker_id, lease)
    if claim is None:
        return None
    return verify_case(
        conn,
        claim.case_id,
        provider=provider,
        fencing_token=claim.fencing_token,
        worker_id=worker_id,
    )
