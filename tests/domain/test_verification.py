"""Verification: the two-source evidence ladder, correlation, and I8."""

from __future__ import annotations

import psycopg
import pytest

from reclaim.domain.states import CaseState, is_allowed
from reclaim.domain.verification import (
    VerificationBlocked,
    compare,
    correlate_webhook,
    trusted_attempt_for,
    verify_case,
)
from reclaim.provider.contract import ErrorClass, FetchOutcome, LinkStatus
from tests.domain.verification_helpers import (
    AMOUNT,
    CORRELATION_ID,
    StubVerifyProvider,
    case_row,
    deliver_webhook,
    fetch_no_evidence,
    fetch_not_found,
    fetch_paid,
    fetch_status,
    seed_awaiting_customer,
    verifications_for,
)


def _verify(conn, ids, fetch, *, token=0, worker="verifier-1"):
    provider = StubVerifyProvider(fetch)
    result = verify_case(
        conn, ids["case_id"], provider=provider, fencing_token=token, worker_id=worker
    )
    return result, provider


# ---- ROW 8: delayed webhook still verifies -------------------------------


def test_delayed_webhook_still_verifies(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """The webhook arrives after a provider query already reported PAID."""
    ids = seed_awaiting_customer(conn)

    # First cycle: provider says PAID, webhook has not arrived -> row 9 shape.
    early, _ = _verify(verifier_conn, ids, fetch_paid())
    assert early.case_state is CaseState.AWAITING_CUSTOMER
    assert verifications_for(conn, ids["case_id"]) == []

    # Webhook arrives late; the next cycle verifies.
    deliver_webhook(conn, ids)
    late, _ = _verify(verifier_conn, ids, fetch_paid())

    assert late.case_state is CaseState.VERIFIED_RECOVERED
    assert case_row(conn, ids["case_id"])["state"] == "VERIFIED_RECOVERED"
    assert case_row(conn, ids["case_id"])["revenue"] == AMOUNT


def test_delayed_webhook_counts_revenue_exactly_once(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """Row 8 forbids double-counting."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)
    _verify(verifier_conn, ids, fetch_paid())

    # A second cycle is cleanly rejected: the case is no longer
    # AWAITING_CUSTOMER, so the precondition check refuses before any write.
    second, _ = _verify(verifier_conn, ids, fetch_paid())

    assert second.applied is False
    assert second.recovered is False
    assert case_row(conn, ids["case_id"])["revenue"] == AMOUNT
    agreeing = [v for v in verifications_for(conn, ids["case_id"]) if v["agrees"]]
    assert len(agreeing) == 1, "a rejected cycle must not leave an agreeing row"


def test_rejected_cycle_writes_no_verification_row(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """A transition that will not apply must not leave evidence behind.

    fenced_transition returns False rather than raising, so without the
    FOR UPDATE precondition check a rejected cycle would still commit a
    verification row claiming an agreement that was never applied.
    """
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)
    _verify(verifier_conn, ids, fetch_paid())
    before = len(verifications_for(conn, ids["case_id"]))

    for _ in range(3):
        _verify(verifier_conn, ids, fetch_paid())

    assert len(verifications_for(conn, ids["case_id"])) == before


def test_rejected_cycle_is_audited_as_a_stale_write(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """The rejection is recorded, not silently swallowed."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)
    _verify(verifier_conn, ids, fetch_paid())

    _verify(verifier_conn, ids, fetch_paid())

    row = conn.execute(
        "SELECT count(*) FROM audit_events WHERE case_id = %s "
        "AND reason_code = 'stale_write_rejected'",
        (ids["case_id"],),
    ).fetchone()
    assert row is not None and row[0] >= 1


def test_verified_recovered_is_terminal(conn: psycopg.Connection) -> None:
    """The structural reason revenue is exactly-once."""
    for state in CaseState:
        assert not is_allowed(CaseState.VERIFIED_RECOVERED, state)


def test_agreeing_verification_records_both_sources(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    ids = seed_awaiting_customer(conn)
    webhook_id = deliver_webhook(conn, ids)

    _verify(verifier_conn, ids, fetch_paid())

    row = verifications_for(conn, ids["case_id"])[0]
    assert row["agrees"] is True
    assert row["verified_amount_minor"] == AMOUNT
    assert row["webhook_status"] == "SUCCESS"
    assert row["query_status"] == "PAID"
    assert row["webhook_event_id"] == webhook_id


# ---- provider success + no webhook -> keep waiting -------------------------


def test_missing_webhook_verified_by_query(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """One source is never enough, even when that source says SUCCESS."""
    ids = seed_awaiting_customer(conn)

    result, provider = _verify(verifier_conn, ids, fetch_paid())

    assert result.case_state is CaseState.AWAITING_CUSTOMER
    assert case_row(conn, ids["case_id"])["state"] == "AWAITING_CUSTOMER"
    assert case_row(conn, ids["case_id"])["revenue"] == 0
    assert result.had_webhook is False


def test_missing_webhook_writes_no_verification_row(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """A missing second source is absence of evidence, not disagreement."""
    ids = seed_awaiting_customer(conn)

    _verify(verifier_conn, ids, fetch_paid())

    assert verifications_for(conn, ids["case_id"]) == []


def test_repeated_polls_without_webhook_do_not_accumulate_rows(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """Waiting on a second source must be a stable state, not an unbounded row generator."""
    ids = seed_awaiting_customer(conn)

    for _ in range(5):
        _verify(verifier_conn, ids, fetch_paid())

    assert verifications_for(conn, ids["case_id"]) == []
    assert case_row(conn, ids["case_id"])["state"] == "AWAITING_CUSTOMER"


def test_uncorrelated_webhook_is_not_evidence(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """A webhook for someone else's reference correlates to nothing."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids, reference="rcv_SOMEONE_ELSE")

    result, _ = _verify(verifier_conn, ids, fetch_paid())

    assert result.had_webhook is False
    assert case_row(conn, ids["case_id"])["revenue"] == 0


def test_invalid_signature_webhook_is_not_evidence(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """An unverified webhook must never count as a source."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids, signature_valid=False)

    result, _ = _verify(verifier_conn, ids, fetch_paid())

    assert result.had_webhook is False
    assert case_row(conn, ids["case_id"])["revenue"] == 0


# ---- ROW 10: webhook success + query pending -> AMBIGUOUS -----------------


def test_webhook_success_query_pending_is_ambiguous(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)

    result, _ = _verify(verifier_conn, ids, fetch_status(LinkStatus.CREATED))

    assert result.case_state is CaseState.AMBIGUOUS
    assert case_row(conn, ids["case_id"])["state"] == "AMBIGUOUS"
    assert case_row(conn, ids["case_id"])["revenue"] == 0


def test_row10_records_disagreement(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)

    _verify(verifier_conn, ids, fetch_status(LinkStatus.CREATED))

    row = verifications_for(conn, ids["case_id"])[0]
    assert row["agrees"] is False
    assert row["verified_amount_minor"] == 0


# ---- ROW 11: query success + webhook failed -> AMBIGUOUS ------------------


def test_query_success_webhook_failed_is_ambiguous(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """Do not pick the favourable source."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids, event_type="payment_link.expired")

    result, _ = _verify(verifier_conn, ids, fetch_paid())

    assert result.case_state is CaseState.AMBIGUOUS
    assert case_row(conn, ids["case_id"])["state"] == "AMBIGUOUS"
    assert case_row(conn, ids["case_id"])["revenue"] == 0


def test_row11_records_disagreement(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids, event_type="payment_link.cancelled")

    _verify(verifier_conn, ids, fetch_paid())

    row = verifications_for(conn, ids["case_id"])[0]
    assert row["agrees"] is False
    assert row["webhook_status"] == "FAILED"


# ---- amount and currency ---------------------------------------------------


def test_amount_mismatch_does_not_verify(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)

    result, _ = _verify(verifier_conn, ids, fetch_paid(amount_paid_minor=AMOUNT - 1))

    assert result.case_state is CaseState.AMBIGUOUS
    assert case_row(conn, ids["case_id"])["revenue"] == 0


def test_overpayment_does_not_verify(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """The provider must not be able to inflate the recognized amount."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)

    result, _ = _verify(verifier_conn, ids, fetch_paid(amount_paid_minor=AMOUNT * 100))

    assert result.case_state is CaseState.AMBIGUOUS
    assert case_row(conn, ids["case_id"])["revenue"] == 0


def test_null_amount_paid_does_not_verify(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)

    result, _ = _verify(verifier_conn, ids, fetch_paid(amount_paid_minor=None))

    assert result.case_state is CaseState.AMBIGUOUS
    assert case_row(conn, ids["case_id"])["revenue"] == 0


def test_currency_mismatch_does_not_verify(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)

    result, _ = _verify(verifier_conn, ids, fetch_paid(currency="USD"))

    assert result.case_state is CaseState.AMBIGUOUS
    assert case_row(conn, ids["case_id"])["revenue"] == 0


def test_missing_currency_does_not_verify(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """Currency is compared on every correlation; absence is not a match."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)

    result, _ = _verify(verifier_conn, ids, fetch_paid(currency=None))

    assert result.case_state is CaseState.AMBIGUOUS
    assert case_row(conn, ids["case_id"])["revenue"] == 0


def test_recognized_amount_comes_from_the_attempt_not_the_provider(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """The trusted amount is the attempt's; the provider only has to match it."""
    ids = seed_awaiting_customer(conn, amount_minor=25_000)
    deliver_webhook(conn, ids)

    _verify(verifier_conn, ids, fetch_paid(amount_paid_minor=25_000))

    assert case_row(conn, ids["case_id"])["revenue"] == 25_000
    assert verifications_for(conn, ids["case_id"])[0]["verified_amount_minor"] == 25_000


# ---- no evidence -----------------------------------------------------------


@pytest.mark.parametrize(
    "error_class",
    [
        ErrorClass.TIMEOUT,
        ErrorClass.TRANSIENT_PROVIDER,
        ErrorClass.RATE_LIMIT,
        ErrorClass.AUTHENTICATION,
        ErrorClass.MALFORMED_RESPONSE,
        ErrorClass.NETWORK,
    ],
)
def test_no_evidence_writes_nothing(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection, error_class
) -> None:
    """timeout / 5xx / 429 / auth / malformed are not a negative verification."""
    ids = seed_awaiting_customer(conn, suffix=f"ne{error_class.value}")
    deliver_webhook(conn, ids)

    result, _ = _verify(verifier_conn, ids, fetch_no_evidence(error_class))

    assert result.agrees is None
    assert verifications_for(conn, ids["case_id"]) == []
    assert case_row(conn, ids["case_id"])["state"] == "AWAITING_CUSTOMER"
    assert case_row(conn, ids["case_id"])["revenue"] == 0


def test_repeated_no_evidence_polls_accumulate_nothing(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)

    for _ in range(5):
        _verify(verifier_conn, ids, fetch_no_evidence())

    assert verifications_for(conn, ids["case_id"]) == []
    assert case_row(conn, ids["case_id"])["state"] == "AWAITING_CUSTOMER"


def test_not_found_is_a_disagreement_not_no_evidence(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """NOT_FOUND is a positive provider answer; NO_EVIDENCE is not."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)

    result, _ = _verify(verifier_conn, ids, fetch_not_found())

    assert result.agrees is False
    assert result.case_state is CaseState.AMBIGUOUS
    assert case_row(conn, ids["case_id"])["revenue"] == 0


# ---- provider-side death is not terminal ---------------------------------


@pytest.mark.parametrize("status", list(LinkStatus))
def test_no_link_status_other_than_paid_verifies(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection, status: LinkStatus
) -> None:
    ids = seed_awaiting_customer(conn, suffix=f"ls{status.value}")
    deliver_webhook(conn, ids)

    result, _ = _verify(
        verifier_conn, ids, fetch_status(status, amount_paid_minor=AMOUNT)
    )

    if status is LinkStatus.PAID:
        assert result.case_state is CaseState.VERIFIED_RECOVERED
    else:
        assert result.case_state is CaseState.AMBIGUOUS
        assert case_row(conn, ids["case_id"])["revenue"] == 0


def test_expired_never_becomes_verified_failed(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """Expiry is unverified as failure evidence: it disagrees, it does not confirm."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)

    _verify(verifier_conn, ids, fetch_status(LinkStatus.EXPIRED))

    assert case_row(conn, ids["case_id"])["state"] != "VERIFIED_FAILED"
    assert case_row(conn, ids["case_id"])["state"] == "AMBIGUOUS"


# ---- I8 at the application layer -----------------------------------------


def test_webhook_alone_does_not_recover(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """I8: a webhook claiming success, with no corroboration, mints nothing."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)

    result, _ = _verify(verifier_conn, ids, fetch_no_evidence())

    assert result.recovered is False
    assert case_row(conn, ids["case_id"])["revenue"] == 0


def test_forged_webhook_cannot_dictate_the_amount(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """The webhook is untrusted input; it supplies a status claim only."""
    ids = seed_awaiting_customer(conn, amount_minor=AMOUNT)
    conn.execute(
        """
        INSERT INTO webhook_events (
            provider_event_id, event_type, signature_valid, resolution, payload
        ) VALUES ('evt_forged', 'payment_link.paid', true, 'IGNORED', %s)
        """,
        (
            psycopg.types.json.Jsonb(
                {
                    "payload": {
                        "payment_link": {
                            "entity": {
                                "reference_id": ids["reference"],
                                "amount": 99_999_999,
                                "amount_paid": 99_999_999,
                            }
                        }
                    }
                }
            ),
        ),
    )

    _verify(verifier_conn, ids, fetch_paid(amount_paid_minor=AMOUNT))

    assert case_row(conn, ids["case_id"])["revenue"] == AMOUNT


def test_verification_never_creates_a_financial_mechanism(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """The stub raises if create_payment_link is ever called."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)

    _verify(verifier_conn, ids, fetch_paid())

    attempts = conn.execute(
        "SELECT count(*) FROM execution_attempts WHERE case_id = %s", (ids["case_id"],)
    ).fetchone()
    actions = conn.execute(
        "SELECT count(*) FROM recovery_actions WHERE case_id = %s", (ids["case_id"],)
    ).fetchone()
    assert attempts[0] == 1
    assert actions[0] == 1


# ---- correlation and pure functions --------------------------------------


def test_correlation_matches_by_reference_id(conn: psycopg.Connection) -> None:
    """Correlation by reference_id."""
    ids = seed_awaiting_customer(conn)
    webhook_id = deliver_webhook(conn, ids)
    attempt = trusted_attempt_for(conn, ids["case_id"])

    evidence = correlate_webhook(conn, attempt)

    assert evidence is not None
    assert evidence.webhook_event_id == webhook_id
    assert evidence.claims_success is True


def test_correlation_matches_by_provider_correlation_id(
    conn: psycopg.Connection,
) -> None:
    """Correlation by the link id on the request, not a matching reference_id."""
    ids = seed_awaiting_customer(conn)
    conn.execute(
        """
        INSERT INTO provider_requests (
            attempt_id, operation, request_no, idempotency_key, request_body,
            outcome, provider_correlation_id, completed_at
        ) VALUES (%s, 'create_payment_link', 1, %s, '{}'::jsonb, 'ACCEPTED', %s, now())
        """,
        (ids["attempt_id"], ids["reference"], CORRELATION_ID),
    )
    webhook_id = deliver_webhook(conn, ids, reference="rcv_NOT_THE_ATTEMPT")
    attempt = trusted_attempt_for(conn, ids["case_id"])

    evidence = correlate_webhook(conn, attempt)

    assert evidence is not None
    assert evidence.webhook_event_id == webhook_id


def test_compare_only_agrees_on_full_match() -> None:
    from reclaim.domain.verification import TrustedAttempt, WebhookEvidence

    attempt = TrustedAttempt(1, 1, 1, "rcv_x", AMOUNT, "INR")
    ok = WebhookEvidence(1, "payment_link.paid", True)
    bad = WebhookEvidence(1, "payment_link.expired", False)

    assert compare(ok, fetch_paid(), attempt) == (True, "verification_agreed")
    assert compare(bad, fetch_paid(), attempt)[0] is False
    assert compare(ok, fetch_status(LinkStatus.CREATED), attempt)[0] is False
    assert compare(ok, fetch_no_evidence(), attempt)[0] is None
    assert compare(ok, fetch_not_found(), attempt)[0] is False
    assert compare(ok, fetch_paid(currency=None), attempt)[0] is False


def test_case_without_accepted_attempt_is_blocked(conn: psycopg.Connection) -> None:
    ids = seed_awaiting_customer(conn, attempt_state="UNKNOWN")

    with pytest.raises(VerificationBlocked):
        trusted_attempt_for(conn, ids["case_id"])


def test_no_new_state_machine_edges_introduced() -> None:
    """Verification uses only edges that already existed."""
    assert is_allowed(CaseState.AWAITING_CUSTOMER, CaseState.VERIFIED_RECOVERED)
    assert is_allowed(CaseState.AWAITING_CUSTOMER, CaseState.AMBIGUOUS)
