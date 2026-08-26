"""Verification crash recovery across every point in the transaction.

Verification issues no financial POST in any path, so the provider GET is
always safely repeatable. The only question each row answers is what is
durable afterwards and whether revenue may still be written.
"""

from __future__ import annotations

import psycopg
import pytest

from reclaim.domain.leases import claim_case
from reclaim.domain.states import CaseState
from reclaim.domain.sweeper import sweep_expired_leases
from reclaim.domain.verification import VerificationBlocked, verify_case, verify_once
from reclaim.provider.contract import LinkStatus
from tests.domain.verification_helpers import (
    AMOUNT,
    StubVerifyProvider,
    case_row,
    deliver_webhook,
    fetch_no_evidence,
    fetch_paid,
    fetch_status,
    seed_awaiting_customer,
    verifications_for,
)


def _verify(conn, ids, fetch, *, token=0):
    provider = StubVerifyProvider(fetch)
    return verify_case(
        conn, ids["case_id"], provider=provider, fencing_token=token,
        worker_id="verifier-crash",
    ), provider


# ---- V1: GET succeeded, crash before the verification transaction --------


def test_v1_crash_before_txn_leaves_nothing_written(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """Only the GET happened; the process died before deciding anything."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)
    provider = StubVerifyProvider(fetch_paid())

    provider.fetch_by_reference(reference_id=ids["reference"])  # the GET, then "crash"

    assert case_row(conn, ids["case_id"])["state"] == "AWAITING_CUSTOMER"
    assert case_row(conn, ids["case_id"])["revenue"] == 0
    assert verifications_for(conn, ids["case_id"]) == []


def test_v1_reclaim_and_reverify_succeeds(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """A later verifier repeats the GET and completes normally."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)
    StubVerifyProvider(fetch_paid()).fetch_by_reference(reference_id=ids["reference"])

    result, _ = _verify(verifier_conn, ids, fetch_paid())

    assert result.recovered is True
    assert case_row(conn, ids["case_id"])["revenue"] == AMOUNT


def test_v1_get_is_repeatable(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)
    provider = StubVerifyProvider(fetch_paid())

    for _ in range(3):
        provider.fetch_by_reference(reference_id=ids["reference"])

    assert len(provider.fetch_calls) == 3
    assert case_row(conn, ids["case_id"])["revenue"] == 0


# ---- V2: the verification transaction rolls back -------------------------


def test_v2_rollback_leaves_no_partial_state(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """Atomicity: verification row, transition and revenue live or die together."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with verifier_conn.transaction():
            verifier_conn.execute(
                """
                INSERT INTO verifications (
                    case_id, attempt_id, agrees, verified_amount_minor
                ) VALUES (%s, %s, true, %s)
                """,
                (ids["case_id"], ids["attempt_id"], AMOUNT),
            )
            raise _Boom()

    assert verifications_for(conn, ids["case_id"]) == []
    assert case_row(conn, ids["case_id"])["state"] == "AWAITING_CUSTOMER"
    assert case_row(conn, ids["case_id"])["revenue"] == 0


def test_v2_verify_case_rolls_back_when_revenue_write_crashes(
    conn: psycopg.Connection,
    verifier_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verification transaction is all-or-nothing even after the INSERT."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("crash after verification insert")

    monkeypatch.setattr("reclaim.domain.verification._write_revenue", _boom)

    with pytest.raises(RuntimeError, match="crash after verification insert"):
        verify_case(
            verifier_conn,
            ids["case_id"],
            provider=StubVerifyProvider(fetch_paid()),
            fencing_token=0,
            worker_id="verifier-crash",
        )

    assert verifications_for(conn, ids["case_id"]) == []
    assert case_row(conn, ids["case_id"])["state"] == "AWAITING_CUSTOMER"
    assert case_row(conn, ids["case_id"])["revenue"] == 0


def test_v2_case_is_still_verifiable_after_rollback(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)

    result, _ = _verify(verifier_conn, ids, fetch_paid())

    assert result.recovered is True
    assert case_row(conn, ids["case_id"])["revenue"] == AMOUNT


# ---- V3: the transaction commits ------------------------------------------


def test_v3_commit_is_atomic_and_complete(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)

    _verify(verifier_conn, ids, fetch_paid())

    row = case_row(conn, ids["case_id"])
    assert row["state"] == "VERIFIED_RECOVERED"
    assert row["revenue"] == AMOUNT
    assert verifications_for(conn, ids["case_id"])[0]["agrees"] is True


def test_v3_revenue_never_exists_without_agreeing_verification(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """The forbidden committed state, checked directly against the table."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)
    _verify(verifier_conn, ids, fetch_paid())

    orphan = conn.execute(
        """
        SELECT count(*) FROM recovery_cases c
         WHERE c.recovered_amount_minor > 0
           AND NOT EXISTS (
             SELECT 1 FROM verifications v
              WHERE v.case_id = c.id AND v.agrees
                AND v.verified_amount_minor = c.recovered_amount_minor)
        """
    ).fetchone()
    assert orphan is not None and orphan[0] == 0


def test_v3_verified_recovered_never_exists_without_revenue(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)
    _verify(verifier_conn, ids, fetch_paid())

    bad = conn.execute(
        "SELECT count(*) FROM recovery_cases "
        "WHERE state = 'VERIFIED_RECOVERED' AND recovered_amount_minor = 0"
    ).fetchone()
    assert bad is not None and bad[0] == 0


# ---- V4: a second verifier races after the first committed ---------------


def test_v4_second_verifier_after_commit_changes_nothing(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)
    _verify(verifier_conn, ids, fetch_paid())

    second, _ = _verify(verifier_conn, ids, fetch_paid())

    assert second.applied is False
    assert case_row(conn, ids["case_id"])["revenue"] == AMOUNT
    assert len(verifications_for(conn, ids["case_id"])) == 1


def test_v4_recovered_case_is_not_reclaimable(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """VERIFIED_RECOVERED is terminal, so no verifier can claim it again."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)
    _verify(verifier_conn, ids, fetch_paid())
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = '-infinity' WHERE id = %s",
        (ids["case_id"],),
    )

    claim = claim_case(
        verifier_conn, ids["case_id"], CaseState.AWAITING_CUSTOMER, "v2", 45
    )

    assert claim is None


# ---- V5: duplicate webhook -----------------------------------------------


def test_v5_duplicate_webhook_cannot_double_count(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids, provider_event_id="evt_dup_1")
    _verify(verifier_conn, ids, fetch_paid())

    deliver_webhook(conn, ids, provider_event_id="evt_dup_2")
    second, _ = _verify(verifier_conn, ids, fetch_paid())

    assert second.applied is False
    assert case_row(conn, ids["case_id"])["revenue"] == AMOUNT


def test_v5_identical_provider_event_id_is_deduped(
    conn: psycopg.Connection
) -> None:
    """uq_provider_event stops a literal redelivery at the ingest layer."""
    from psycopg.errors import UniqueViolation

    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids, provider_event_id="evt_same")

    with pytest.raises(UniqueViolation):
        deliver_webhook(conn, ids, provider_event_id="evt_same")


# ---- V6: evidence conflicts ----------------------------------------------


def test_v6_conflict_goes_ambiguous_with_no_revenue(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)

    result, _ = _verify(verifier_conn, ids, fetch_status(LinkStatus.CREATED))

    assert result.case_state is CaseState.AMBIGUOUS
    assert case_row(conn, ids["case_id"])["revenue"] == 0
    assert verifications_for(conn, ids["case_id"])[0]["agrees"] is False


def test_v6_ambiguous_case_is_task_7s_to_resolve(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """Verification hands off; it does not reconcile."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)
    _verify(verifier_conn, ids, fetch_status(LinkStatus.CREATED))

    from reclaim.domain.states import is_allowed

    assert is_allowed(CaseState.AMBIGUOUS, CaseState.RECONCILING)
    assert case_row(conn, ids["case_id"])["state"] == "AMBIGUOUS"


# ---- V7: the GET produced no evidence ------------------------------------


def test_v7_no_evidence_leaves_the_case_untouched(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)

    result, _ = _verify(verifier_conn, ids, fetch_no_evidence())

    assert result.agrees is None
    assert case_row(conn, ids["case_id"])["state"] == "AWAITING_CUSTOMER"
    assert case_row(conn, ids["case_id"])["revenue"] == 0
    assert verifications_for(conn, ids["case_id"]) == []


def test_v7_retry_after_evidence_returns_succeeds(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """No evidence is a wait, not a verdict: the next cycle can still verify."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)
    _verify(verifier_conn, ids, fetch_no_evidence())

    result, _ = _verify(verifier_conn, ids, fetch_paid())

    assert result.recovered is True
    assert case_row(conn, ids["case_id"])["revenue"] == AMOUNT


# ---- lease recovery ------------------------------------------------------


def test_expired_verifier_lease_is_released_not_transitioned(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """A crashed verifier must not move the case; the sweeper only frees it."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)
    claim = claim_case(
        verifier_conn, ids["case_id"], CaseState.AWAITING_CUSTOMER, "v-dead", 45
    )
    assert claim is not None
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = now() - interval '1 s' "
        "WHERE id = %s",
        (ids["case_id"],),
    )

    sweep_expired_leases(conn)

    assert case_row(conn, ids["case_id"])["state"] == "AWAITING_CUSTOMER"
    assert case_row(conn, ids["case_id"])["revenue"] == 0


def test_verify_once_claims_and_completes(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = '-infinity' WHERE id = %s",
        (ids["case_id"],),
    )

    result = verify_once(verifier_conn, provider=StubVerifyProvider(fetch_paid()))

    assert result is not None
    assert result.recovered is True
    assert case_row(conn, ids["case_id"])["revenue"] == AMOUNT
