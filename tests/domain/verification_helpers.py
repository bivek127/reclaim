"""Shared fixtures for verification tests."""

from __future__ import annotations

import itertools
from typing import Any, NoReturn

import psycopg
from psycopg.types.json import Jsonb

from reclaim.provider.contract import (
    FetchOutcome,
    FetchResult,
    LinkStatus,
    RequestRecord,
    RetryChargeUnsupported,
)
from tests.db.helpers import (
    insert_action,
    insert_attempt,
    insert_case,
    insert_obligation,
    insert_policy_decision,
)

AMOUNT = 10_000
CURRENCY = "INR"
CORRELATION_ID = "plink_Verified00001"

_SEQ = itertools.count(1)


class StubVerifyProvider:
    """Implements the PaymentProvider protocol. Read-only by construction."""

    def __init__(self, fetch: FetchResult) -> None:
        self._fetch = fetch
        self.fetch_calls: list[str] = []

    def fetch_by_reference(self, *, reference_id: str) -> FetchResult:
        self.fetch_calls.append(reference_id)
        return self._fetch

    def create_payment_link(self, **kwargs: Any) -> NoReturn:
        raise AssertionError("verification must never create a financial mechanism")

    def retry_charge(self, **kwargs: Any) -> NoReturn:
        raise RetryChargeUnsupported("§19.1a")

    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        return False


def fetch_paid(
    *,
    amount_paid_minor: int = AMOUNT,
    currency: str | None = CURRENCY,
    correlation_id: str = CORRELATION_ID,
) -> FetchResult:
    return _found(LinkStatus.PAID, amount_paid_minor, currency, correlation_id)


def fetch_status(
    status: LinkStatus, *, amount_paid_minor: int = 0, currency: str | None = CURRENCY
) -> FetchResult:
    return _found(status, amount_paid_minor, currency, CORRELATION_ID)


def _found(
    status: LinkStatus,
    amount_paid_minor: int | None,
    currency: str | None,
    correlation_id: str,
) -> FetchResult:
    return FetchResult(
        outcome=FetchOutcome.FOUND,
        provider_reference="ref",
        request=RequestRecord("fetch_by_reference", "GET", "/v1/payment_links", None),
        http_status=200,
        provider_correlation_id=correlation_id,
        link_status=status,
        amount_minor=AMOUNT,
        amount_paid_minor=amount_paid_minor,
        currency=currency,
    )


def fetch_not_found() -> FetchResult:
    return FetchResult(
        outcome=FetchOutcome.NOT_FOUND,
        provider_reference="ref",
        request=RequestRecord("fetch_by_reference", "GET", "/v1/payment_links", None),
        http_status=200,
        response_body={"payment_links": []},
    )


def fetch_no_evidence(error_class: Any = None) -> FetchResult:
    return FetchResult(
        outcome=FetchOutcome.NO_EVIDENCE,
        provider_reference="ref",
        request=RequestRecord("fetch_by_reference", "GET", "/v1/payment_links", None),
        error_class=error_class,
    )


def seed_awaiting_customer(
    conn: psycopg.Connection,
    *,
    amount_minor: int = AMOUNT,
    currency: str = CURRENCY,
    attempt_state: str = "ACCEPTED",
    suffix: str | None = None,
) -> dict[str, Any]:
    """A case in AWAITING_CUSTOMER with one ACCEPTED attempt, as a successful
    dispatch and reconciliation leave it."""
    n = suffix or str(next(_SEQ))
    obligation_id = insert_obligation(
        conn,
        anchor_key=f"ord_v{n}",
        anchor_canonical=f"order:ord_v{n}",
        amount_minor=amount_minor,
        currency=currency,
        source_event_id=f"evt_v{n}",
    )
    case_id = insert_case(conn, obligation_id, state="AWAITING_CUSTOMER")
    policy_id = insert_policy_decision(conn, case_id)
    action_id = insert_action(conn, case_id, policy_id, status="LIVE")
    reference = f"rcv_V{n}"
    attempt_id = insert_attempt(
        conn,
        action_id,
        case_id,
        idempotency_key=reference,
        provider_reference=reference,
        state=attempt_state,
        amount_minor=amount_minor,
        currency=currency,
    )
    return {
        "obligation_id": obligation_id,
        "case_id": case_id,
        "policy_id": policy_id,
        "action_id": action_id,
        "attempt_id": attempt_id,
        "reference": reference,
    }


def deliver_webhook(
    conn: psycopg.Connection,
    ids: dict[str, Any],
    *,
    event_type: str = "payment_link.paid",
    reference: str | None = None,
    signature_valid: bool = True,
    provider_event_id: str | None = None,
) -> int:
    """Insert a webhook_events row exactly as webhook ingestion would."""
    ref = reference if reference is not None else ids["reference"]
    payload = {
        "event": event_type,
        "payload": {
            "payment_link": {
                "entity": {
                    "id": CORRELATION_ID,
                    "reference_id": ref,
                    "status": event_type.split(".", 1)[-1],
                }
            }
        },
    }
    row = conn.execute(
        """
        INSERT INTO webhook_events (
            provider_event_id, event_type, signature_valid, resolution,
            anchor_canonical, payload
        ) VALUES (%s, %s, %s, 'IGNORED', NULL, %s)
        RETURNING id
        """,
        (
            provider_event_id or f"evt_wh_{next(_SEQ)}",
            event_type,
            signature_valid,
            Jsonb(payload),
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


def case_row(conn: psycopg.Connection, case_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT state, recovered_amount_minor, fencing_token FROM recovery_cases "
        "WHERE id = %s",
        (case_id,),
    ).fetchone()
    assert row is not None
    return {"state": row[0], "revenue": row[1], "fencing_token": row[2]}


def verifications_for(conn: psycopg.Connection, case_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, agrees, verified_amount_minor, webhook_status, query_status,
               webhook_event_id
          FROM verifications WHERE case_id = %s ORDER BY id
        """,
        (case_id,),
    ).fetchall()
    return [
        {
            "id": r[0],
            "agrees": r[1],
            "verified_amount_minor": r[2],
            "webhook_status": r[3],
            "query_status": r[4],
            "webhook_event_id": r[5],
        }
        for r in rows
    ]
