"""Shared fixtures for execution tests."""

from __future__ import annotations

from typing import Any, NoReturn

import psycopg

from reclaim.provider.contract import (
    CreateLinkResult,
    Customer,
    ErrorClass,
    FetchOutcome,
    FetchResult,
    LinkStatus,
    ProviderOutcome,
    RequestRecord,
    RetryChargeUnsupported,
)
from tests.db.helpers import insert_case, insert_obligation, insert_policy_decision

LINK_TTL_SECONDS = 3600


class StubProvider:
    """Implements the PaymentProvider protocol. No Razorpay details here."""

    def __init__(
        self,
        outcome: ProviderOutcome = ProviderOutcome.ACCEPTED,
        *,
        correlation_id: str | None = "plink_stub0000001",
        http_status: int | None = 200,
        on_call: Any = None,
    ) -> None:
        self.outcome = outcome
        self.correlation_id = correlation_id
        self.http_status = http_status
        self.calls: list[dict[str, Any]] = []
        self._on_call = on_call

    def create_payment_link(
        self,
        *,
        reference_id: str,
        amount_minor: int,
        currency: str,
        customer: Customer,
        expire_by: int | None = None,
        description: str | None = None,
    ) -> CreateLinkResult:
        self.calls.append(
            {
                "reference_id": reference_id,
                "amount_minor": amount_minor,
                "currency": currency,
                "expire_by": expire_by,
            }
        )
        if self._on_call is not None:
            self._on_call(reference_id)

        resolved = self.outcome in {
            ProviderOutcome.ACCEPTED,
            ProviderOutcome.DUPLICATE_REFERENCE,
        }
        return CreateLinkResult(
            outcome=self.outcome,
            provider_reference=reference_id,
            request=RequestRecord(
                operation="create_payment_link",
                method="POST",
                url="/v1/payment_links",
                body={"reference_id": reference_id, "amount": amount_minor},
            ),
            error_class=None if resolved else ErrorClass.UNKNOWN,
            http_status=self.http_status,
            provider_correlation_id=self.correlation_id if resolved else None,
            link_status=LinkStatus.CREATED if resolved else None,
            expire_by=expire_by,
            response_body={"id": self.correlation_id} if resolved else None,
        )

    def fetch_by_reference(self, *, reference_id: str) -> NoReturn:
        raise AssertionError("Task 6 must never call fetch_by_reference (that is Task 7)")


class StubReconcileProvider:
    """Provider stub for reconciliation tests. Serves scripted fetch results,
    then POSTs.

    Deliberately separate from StubProvider so an execution test can still
    assert that dispatch never reads, while reconciliation tests exercise both
    operations.
    """

    def __init__(
        self,
        *fetches: FetchResult,
        create_outcome: ProviderOutcome = ProviderOutcome.ACCEPTED,
        create_correlation_id: str | None = "plink_repost00001",
    ) -> None:
        self._fetches = list(fetches)
        self._create = StubProvider(
            create_outcome, correlation_id=create_correlation_id
        )
        self.fetch_calls: list[str] = []

    @property
    def create_calls(self) -> list[dict[str, Any]]:
        return self._create.calls

    def fetch_by_reference(self, *, reference_id: str) -> FetchResult:
        self.fetch_calls.append(reference_id)
        if not self._fetches:
            raise AssertionError(f"unscripted fetch for {reference_id}")
        nxt = self._fetches.pop(0)
        if len(self._fetches) == 0:
            self._fetches.append(nxt)  # repeat the last one for re-queries
        return nxt

    def create_payment_link(self, **kwargs: Any) -> CreateLinkResult:
        return self._create.create_payment_link(**kwargs)

    def retry_charge(self, **kwargs: Any) -> NoReturn:
        raise RetryChargeUnsupported("§19.1a")

    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        return False


def fetch_found(
    *,
    correlation_id: str = "plink_found0000001",
    link_status: LinkStatus = LinkStatus.CREATED,
    amount_minor: int | None = 10_000,
    amount_paid_minor: int = 0,
) -> FetchResult:
    return FetchResult(
        outcome=FetchOutcome.FOUND,
        provider_reference="ref",
        request=RequestRecord("fetch_by_reference", "GET", "/v1/payment_links", None),
        http_status=200,
        provider_correlation_id=correlation_id,
        link_status=link_status,
        amount_minor=amount_minor,
        amount_paid_minor=amount_paid_minor,
        currency="INR",
        response_body={"payment_links": [{"id": correlation_id}]},
    )


def fetch_not_found() -> FetchResult:
    return FetchResult(
        outcome=FetchOutcome.NOT_FOUND,
        provider_reference="ref",
        request=RequestRecord("fetch_by_reference", "GET", "/v1/payment_links", None),
        http_status=200,
        response_body={"payment_links": []},
    )


def fetch_no_evidence(error_class: ErrorClass = ErrorClass.TIMEOUT,
                      http_status: int | None = None) -> FetchResult:
    return FetchResult(
        outcome=FetchOutcome.NO_EVIDENCE,
        provider_reference="ref",
        request=RequestRecord("fetch_by_reference", "GET", "/v1/payment_links", None),
        error_class=error_class,
        http_status=http_status,
    )


def seed_dispatchable(
    conn: psycopg.Connection,
    *,
    state: str = "ACTION_READY",
    max_attempts: int = 2,
    attempt_count: int = 0,
    amount_minor: int = 10_000,
    customer_ref: str = "cust@example.com",
    suffix: str = "1",
) -> dict[str, int]:
    """A case sitting in ACTION_READY with a policy decision already recorded."""
    obligation_id = insert_obligation(
        conn,
        anchor_key=f"ord_{suffix}",
        anchor_canonical=f"order:ord_{suffix}",
        amount_minor=amount_minor,
        customer_ref=customer_ref,
        source_event_id=f"evt_{suffix}",
    )
    case_id = insert_case(
        conn,
        obligation_id,
        state=state,
        max_attempts=max_attempts,
        attempt_count=attempt_count,
    )
    policy_id = insert_policy_decision(conn, case_id)
    return {"obligation_id": obligation_id, "case_id": case_id, "policy_id": policy_id}


def case_row(conn: psycopg.Connection, case_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT state, attempt_count, fencing_token FROM recovery_cases WHERE id = %s",
        (case_id,),
    ).fetchone()
    assert row is not None
    return {"state": row[0], "attempt_count": row[1], "fencing_token": row[2]}


def attempts_for(conn: psycopg.Connection, case_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, state, idempotency_key, provider_reference, attempt_no, amount_minor
          FROM execution_attempts WHERE case_id = %s ORDER BY id
        """,
        (case_id,),
    ).fetchall()
    return [
        {
            "id": r[0],
            "state": r[1],
            "idempotency_key": r[2],
            "provider_reference": r[3],
            "attempt_no": r[4],
            "amount_minor": r[5],
        }
        for r in rows
    ]


def actions_for(conn: psycopg.Connection, case_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, status, sequence_no, resolved_at, provider_expires_at, action_deadline_at
          FROM recovery_actions WHERE case_id = %s ORDER BY id
        """,
        (case_id,),
    ).fetchall()
    return [
        {
            "id": r[0],
            "status": r[1],
            "sequence_no": r[2],
            "resolved_at": r[3],
            "provider_expires_at": r[4],
            "action_deadline_at": r[5],
        }
        for r in rows
    ]


def requests_for(conn: psycopg.Connection, case_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT pr.id, pr.outcome, pr.request_no, pr.completed_at,
               pr.response_body, pr.provider_correlation_id, pr.idempotency_key
          FROM provider_requests pr
          JOIN execution_attempts ea ON ea.id = pr.attempt_id
         WHERE ea.case_id = %s ORDER BY pr.id
        """,
        (case_id,),
    ).fetchall()
    return [
        {
            "id": r[0],
            "outcome": r[1],
            "request_no": r[2],
            "completed_at": r[3],
            "response_body": r[4],
            "provider_correlation_id": r[5],
            "idempotency_key": r[6],
        }
        for r in rows
    ]


def breaker_row(conn: psycopg.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT state, consecutive_failures, opened_at FROM circuit_breaker WHERE id = 1"
    ).fetchone()
    assert row is not None
    return {"state": row[0], "consecutive_failures": row[1], "opened_at": row[2]}
