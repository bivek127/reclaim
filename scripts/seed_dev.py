"""Populate a development database by driving the real domain paths.

Every case below is produced by the same functions the production jobs call:
ingest, diagnosis, policy, execution, reconciliation, verification, review.
Nothing is inserted straight into a domain table, so the resulting states and
audit trail are genuine evidence rather than fixtures shaped to look like one.

Setup-only concerns live here and nowhere else: a scripted provider stub that
never touches the network, and webhook bodies that stand in for real provider
deliveries. Neither is importable by application code.

Refuses to run against a database whose name does not look like a development
database unless --force is given.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, NoReturn

import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reclaim.config import lease_seconds_for, load_operational
from reclaim.domain import (
    PolicyFacts,
    apply_policy,
    approve_review,
    claim_case,
    diagnose_case,
    dispatch,
    expire_action_deadlines,
    reconcile_case,
    reject_review,
    set_breaker_state,
    transition,
    verify_case,
)
from reclaim.domain.execution import BudgetExhausted, DispatchAborted
from reclaim.domain.breaker import BreakerOpen
from reclaim.domain.states import CaseState
from reclaim.ingest.webhook import ingest_webhook
from reclaim.llm.client import UnreachableLlm
from reclaim.provider.contract import (
    CreateLinkResult,
    Customer,
    ErrorClass,
    FetchOutcome,
    FetchResult,
    LinkStatus,
    ProviderOutcome,
    RequestRecord,
)

LINK_TTL_SECONDS = 3600
BASE_TIME = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Scripted provider. Setup-only: no network, no credentials, no adapter reuse.
# --------------------------------------------------------------------------


class SeedProvider:
    """Returns a scripted outcome so execution paths run without a network."""

    def __init__(
        self,
        outcome: ProviderOutcome = ProviderOutcome.ACCEPTED,
        *,
        correlation_id: str | None = None,
        fetch: FetchResult | None = None,
    ) -> None:
        self.outcome = outcome
        self.correlation_id = correlation_id
        self._fetch = fetch
        self.create_calls: list[str] = []
        self.fetch_calls: list[str] = []

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
        self.create_calls.append(reference_id)
        resolved = self.outcome in {
            ProviderOutcome.ACCEPTED,
            ProviderOutcome.DUPLICATE_REFERENCE,
        }
        cid = self.correlation_id or f"plink_{reference_id[4:18]}"
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
            http_status=200 if resolved else None,
            provider_correlation_id=cid if resolved else None,
            link_status=LinkStatus.CREATED if resolved else None,
            expire_by=expire_by,
            response_body={"id": cid} if resolved else None,
        )

    def fetch_by_reference(self, *, reference_id: str) -> FetchResult:
        self.fetch_calls.append(reference_id)
        if self._fetch is None:
            return FetchResult(
                outcome=FetchOutcome.NO_EVIDENCE,
                provider_reference=reference_id,
                request=RequestRecord(
                    operation="fetch_by_reference", method="GET",
                    url="/v1/payment_links", body=None,
                ),
                error_class=ErrorClass.TIMEOUT,
            )
        return self._fetch

    def retry_charge(self, **kwargs: Any) -> NoReturn:
        from reclaim.provider.contract import RetryChargeUnsupported

        raise RetryChargeUnsupported("not dispatchable")

    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        return True


def _found(reference_id: str, *, amount_minor: int, status: LinkStatus,
           correlation_id: str) -> FetchResult:
    return FetchResult(
        outcome=FetchOutcome.FOUND,
        provider_reference=reference_id,
        request=RequestRecord(
            operation="fetch_by_reference", method="GET",
            url="/v1/payment_links", body=None,
        ),
        http_status=200,
        provider_correlation_id=correlation_id,
        link_status=status,
        amount_minor=amount_minor,
        amount_paid_minor=amount_minor if status is LinkStatus.PAID else None,
        currency="INR",
    )


def _not_found(reference_id: str) -> FetchResult:
    return FetchResult(
        outcome=FetchOutcome.NOT_FOUND,
        provider_reference=reference_id,
        request=RequestRecord(
            operation="fetch_by_reference", method="GET",
            url="/v1/payment_links", body=None,
        ),
        http_status=200,
    )


# --------------------------------------------------------------------------
# Webhook bodies. Stand-ins for provider deliveries; ingested through the
# real ingest path so resolution, dedup, and anchoring all actually run.
# --------------------------------------------------------------------------


def payment_failed_body(*, order_id: str, amount_minor: int, customer: str,
                        error_code: str) -> str:
    return json.dumps({
        "event": "payment.failed",
        "payload": {"payment": {"entity": {
            "id": f"pay_{order_id}",
            "order_id": order_id,
            "amount": amount_minor,
            "currency": "INR",
            "customer_id": customer,
            "error_code": error_code,
        }}},
    })


def link_paid_body(*, reference_id: str, amount_minor: int) -> str:
    return json.dumps({
        "event": "payment_link.paid",
        "payload": {"payment_link": {"entity": {
            "id": f"plink_{reference_id[4:18]}",
            "reference_id": reference_id,
            "amount": amount_minor,
            "amount_paid": amount_minor,
            "currency": "INR",
            "status": "paid",
        }}},
    })


# --------------------------------------------------------------------------
# Pipeline steps. Each one calls the domain function a production job calls,
# claiming a lease first so fencing is exercised exactly as it is in service.
# --------------------------------------------------------------------------


@dataclass
class Seeded:
    case_id: int
    obligation_id: int
    label: str


def _claim(conn: psycopg.Connection, case_id: int, state: CaseState,
           worker: str) -> int:
    claim = claim_case(conn, case_id, state, worker, lease_seconds_for("policy"))
    if claim is None:
        raise RuntimeError(f"could not claim case {case_id} in {state.value}")
    return claim.fencing_token


def _release(conn: psycopg.Connection, case_id: int) -> None:
    """Drop the lease so the next step can claim. Mirrors a worker finishing."""
    conn.execute(
        "UPDATE recovery_cases SET worker_id = NULL, "
        "lease_expires_at = '-infinity' WHERE id = %s",
        (case_id,),
    )


def ingest(conn: psycopg.Connection, *, order_id: str, amount_minor: int,
           customer: str, error_code: str) -> Seeded:
    result = ingest_webhook(
        conn,
        signature_valid=True,
        raw_body=payment_failed_body(
            order_id=order_id, amount_minor=amount_minor,
            customer=customer, error_code=error_code,
        ),
        provider_event_id=f"evt_{order_id}",
    )
    if result.case_id is None or result.obligation_id is None:
        raise RuntimeError(f"ingest did not create a case for {order_id}")
    return Seeded(result.case_id, result.obligation_id, order_id)


def advance_to_diagnosing(conn: psycopg.Connection, case_id: int) -> None:
    """NEW -> ENRICHING -> DIAGNOSING through the real state machine."""
    for src, dst, reason in (
        (CaseState.NEW, CaseState.ENRICHING, "enrichment_started"),
        (CaseState.ENRICHING, CaseState.DIAGNOSING, "enrichment_complete"),
    ):
        token = _claim(conn, case_id, src, "enrichment")
        if not transition(conn, case_id, src, dst, token, reason):
            raise RuntimeError(f"transition {src.value}->{dst.value} refused")
        _release(conn, case_id)


def diagnose(conn: psycopg.Connection, case_id: int) -> int:
    """Real diagnosis. Ollama is not assumed: the deterministic fallback runs."""
    token = _claim(conn, case_id, CaseState.DIAGNOSING, "diagnosis")
    result = diagnose_case(
        conn, case_id, llm=UnreachableLlm(), fencing_token=token,
        worker_id="diagnosis",
    )
    _release(conn, case_id)
    if result.diagnosis_id is None:
        raise RuntimeError(f"diagnosis produced no row for case {case_id}")
    return result.diagnosis_id


def evaluate(conn: psycopg.Connection, case_id: int, diagnosis_id: int, *,
             conflicting_history: bool = False) -> str:
    token = _claim(conn, case_id, CaseState.POLICY_EVAL, "policy")
    row = conn.execute(
        "SELECT d.cause, c.attempt_count, c.max_attempts "
        "FROM diagnoses d JOIN recovery_cases c ON c.id = d.case_id "
        "WHERE d.id = %s",
        (diagnosis_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("diagnosis row vanished")
    cause, attempts, max_attempts = row
    result = apply_policy(
        conn, case_id,
        facts=PolicyFacts(
            cause=cause,
            attempt_count=attempts,
            max_attempts=max_attempts,
            conflicting_history=conflicting_history,
        ),
        diagnosis_id=diagnosis_id,
        fencing_token=token,
        worker_id="policy",
    )
    _release(conn, case_id)
    return result.case_state.value if result.case_state else "UNKNOWN"


def execute(conn: psycopg.Connection, case_id: int, provider: SeedProvider) -> str:
    """Real dispatch: TXN 1 -> provider call -> TXN 2, with real idempotency."""
    policy_id = conn.execute(
        "SELECT id FROM policy_decisions WHERE case_id = %s "
        "ORDER BY id DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    if policy_id is None:
        raise RuntimeError(f"case {case_id} has no policy decision")
    token = _claim(conn, case_id, CaseState.ACTION_READY, "executor")
    try:
        result = dispatch(
            conn, case_id, provider=provider, fencing_token=token,
            policy_decision_id=int(policy_id[0]),
            link_ttl_seconds=LINK_TTL_SECONDS, worker_id="executor",
        )
        state = result.case_state.value
    except BreakerOpen:
        state = "HALTED"
    except (BudgetExhausted, DispatchAborted) as exc:
        state = f"aborted:{type(exc).__name__}"
    _release(conn, case_id)
    return state


def deliver_paid_webhook(conn: psycopg.Connection, case_id: int) -> str:
    """Ingest a provider 'link paid' delivery through the real ingest path."""
    row = conn.execute(
        "SELECT provider_reference, amount_minor FROM execution_attempts "
        "WHERE case_id = %s ORDER BY id DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"case {case_id} has no attempt to correlate")
    reference, amount = str(row[0]), int(row[1])
    ingest_webhook(
        conn,
        signature_valid=True,
        raw_body=link_paid_body(reference_id=reference, amount_minor=amount),
        provider_event_id=f"evt_paid_{reference}",
    )
    return reference


def verify(verifier_conn: psycopg.Connection, app_conn: psycopg.Connection,
           case_id: int, *, status: LinkStatus = LinkStatus.PAID) -> str:
    """Independent verification, run as the verifier role that owns revenue."""
    row = app_conn.execute(
        "SELECT provider_reference, amount_minor FROM execution_attempts "
        "WHERE case_id = %s ORDER BY id DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"case {case_id} has no attempt to verify")
    reference, amount = str(row[0]), int(row[1])
    provider = SeedProvider(fetch=_found(
        reference, amount_minor=amount, status=status,
        correlation_id=f"plink_{reference[4:18]}",
    ))
    token = _claim(verifier_conn, case_id, CaseState.AWAITING_CUSTOMER, "verifier")
    result = verify_case(
        verifier_conn, case_id, provider=provider, fencing_token=token,
        worker_id="verifier",
    )
    _release(app_conn, case_id)
    return result.case_state.value if result.case_state else "UNKNOWN"


def reconcile(conn: psycopg.Connection, case_id: int, *,
              fetch: FetchResult | None, max_polls: int = 20) -> str:
    token = _claim(conn, case_id, CaseState.AMBIGUOUS, "reconciler")
    result = reconcile_case(
        conn, case_id, provider=SeedProvider(fetch=fetch), fencing_token=token,
        source_state=CaseState.AMBIGUOUS, worker_id="reconciler",
        max_polls=max_polls,
    )
    _release(conn, case_id)
    return result.case_state.value


def expire_deadline(conn: psycopg.Connection, case_id: int) -> str:
    """Age the action window, then let the real sweeper escalate it."""
    conn.execute(
        "UPDATE recovery_actions SET provider_expires_at = now() - interval '70 minutes', "
        "action_deadline_at = now() - interval '60 minutes' "
        "WHERE case_id = %s AND status = 'LIVE'",
        (case_id,),
    )
    expire_action_deadlines(conn)
    row = conn.execute(
        "SELECT state::text FROM recovery_cases WHERE id = %s", (case_id,)
    ).fetchone()
    return str(row[0]) if row else "UNKNOWN"


def review_approve(conn: psycopg.Connection, case_id: int, *,
                   reviewer: str) -> str:
    token = _claim(conn, case_id, CaseState.ESCALATED, "review")
    result = approve_review(
        conn, case_id, selected_action="CREATE_PAYMENT_LINK",
        reviewer_ref=reviewer, fencing_token=token, worker_id="review",
    )
    _release(conn, case_id)
    return result.reason


def review_reject(conn: psycopg.Connection, case_id: int, *,
                  reviewer: str) -> str:
    """Composes the two existing primitives, exactly as approve_once does."""
    token = _claim(conn, case_id, CaseState.ESCALATED, "review")
    result = reject_review(
        conn, case_id, reviewer_ref=reviewer, fencing_token=token,
        worker_id="review",
    )
    _release(conn, case_id)
    return result.reason


def exhaust_attempt_budget(app: psycopg.Connection, case_id: int,
                           diagnosis_id: int) -> str:
    """Drive real failed dispatches until policy escalates on spent budget.

    Diagnosis reads failure codes from prior provider requests on the case, so
    a first-pass case has none and resolves to UNKNOWN. Budget exhaustion is
    therefore the escalation the policy table actually reaches here, and it
    leaves both actions TERMINAL_FAILED -- no open action, so an approving
    reviewer can still propose one.
    """
    state = execute(app, case_id, SeedProvider(ProviderOutcome.REJECTED))
    for _ in range(3):
        if state != CaseState.ATTEMPT_FAILED.value:
            break
        token = _claim(app, case_id, CaseState.ATTEMPT_FAILED, "policy")
        if not transition(app, case_id, CaseState.ATTEMPT_FAILED,
                          CaseState.POLICY_EVAL, token, "retry_evaluation"):
            break
        _release(app, case_id)
        state = evaluate(app, case_id, diagnosis_id)
        if state == CaseState.ACTION_READY.value:
            state = execute(app, case_id, SeedProvider(ProviderOutcome.REJECTED))
    return state



# --------------------------------------------------------------------------
# Scenarios. Deterministic: fixed order, fixed identifiers, fixed amounts.
# Each one exists to make a specific operator situation demonstrable.
# --------------------------------------------------------------------------

SCENARIOS: list[dict[str, Any]] = [
    {"key": "recovered", "order": "ord_4101", "amount": 425000,
     "customer": "cust_ravi_8812", "error": "INSUFFICIENT_FUNDS",
     "note": "full happy path through independent verification"},
    {"key": "recovered", "order": "ord_4102", "amount": 129900,
     "customer": "cust_meera_2277", "error": "GATEWAY_ERROR",
     "note": "second recovered case, different failure cause"},
    {"key": "awaiting", "order": "ord_4103", "amount": 78500,
     "customer": "cust_arjun_5510", "error": "EXPIRED_CARD",
     "note": "link live, customer has not paid yet"},
    {"key": "awaiting", "order": "ord_4104", "amount": 1650000,
     "customer": "cust_devi_9001", "error": "INSUFFICIENT_FUNDS",
     "note": "high-value case still waiting"},
    {"key": "deadline_escalated", "order": "ord_4105", "amount": 249000,
     "customer": "cust_sana_3140", "error": "BANK_DOWNTIME",
     "note": "payment window closed without payment"},
    {"key": "budget_escalated", "order": "ord_4106", "amount": 512000,
     "customer": "cust_karan_7788", "error": "MANDATE_REVOKED",
     "note": "both attempts failed, policy escalated on spent budget"},
    {"key": "ambiguous", "order": "ord_4107", "amount": 96000,
     "customer": "cust_priya_4423", "error": "SERVER_ERROR",
     "note": "provider timed out, outcome unknown"},
    {"key": "reconciled_adopted", "order": "ord_4108", "amount": 310000,
     "customer": "cust_imran_6612", "error": "NETWORK_ERROR",
     "note": "ambiguous, then reconciliation adopted the existing link"},
    {"key": "attempt_failed", "order": "ord_4109", "amount": 55000,
     "customer": "cust_nisha_1907", "error": "PAYMENT_DECLINED",
     "note": "provider rejected the link outright"},
    {"key": "review_rejected", "order": "ord_4110", "amount": 187500,
     "customer": "cust_vikram_5023", "error": "RISK_BLOCKED",
     "note": "escalated, then a reviewer rejected it"},
    {"key": "review_approved", "order": "ord_4111", "amount": 220000,
     "customer": "cust_leela_8890", "error": "AUTHENTICATION_FAILED",
     "note": "budget escalated, reviewer approved a proposed action"},
    {"key": "expired_unresolved", "order": "ord_4112", "amount": 143000,
     "customer": "cust_tara_3311", "error": "SERVER_ERROR",
     "note": "reconciliation exhausted its poll budget"},
    {"key": "halted", "order": "ord_4113", "amount": 67000,
     "customer": "cust_omar_2244", "error": "GATEWAY_ERROR",
     "note": "breaker open, dispatch halted before any provider call"},
]


def run_scenario(app: psycopg.Connection, verifier: psycopg.Connection,
                 spec: dict[str, Any]) -> tuple[str, str]:
    key = spec["key"]
    seeded = ingest(
        app, order_id=spec["order"], amount_minor=spec["amount"],
        customer=spec["customer"], error_code=spec["error"],
    )
    cid = seeded.case_id
    advance_to_diagnosing(app, cid)
    diagnosis_id = diagnose(app, cid)

    # An unmapped cause plus conflicting history is what the policy table
    # turns into an escalation; nothing here forces the verdict directly.
    state = evaluate(app, cid, diagnosis_id)
    if state != CaseState.ACTION_READY.value:
        return cid, state

    if key in {"budget_escalated", "review_approved"}:
        state = exhaust_attempt_budget(app, cid, diagnosis_id)
        if key == "review_approved" and state == CaseState.ESCALATED.value:
            review_approve(app, cid, reviewer="ops.reviewer@reclaim.local")
        row = app.execute(
            "SELECT state::text FROM recovery_cases WHERE id = %s", (cid,)
        ).fetchone()
        return cid, str(row[0]) if row else state

    if key == "halted":
        set_breaker_state(
            app, open_breaker=True, reason_code="seed_demonstration",
            trip_cause={"reason": "development seed"}, reset_seconds=120,
            worker_id="seed",
        )
        state = execute(app, cid, SeedProvider())
        set_breaker_state(
            app, open_breaker=False, reason_code="seed_reset", worker_id="seed"
        )
        return cid, state

    if key == "attempt_failed":
        return cid, execute(app, cid, SeedProvider(ProviderOutcome.REJECTED))

    if key in {"ambiguous", "reconciled_adopted", "expired_unresolved"}:
        state = execute(app, cid, SeedProvider(ProviderOutcome.TIMEOUT))
        if key == "ambiguous":
            return cid, state
        reference = app.execute(
            "SELECT provider_reference FROM execution_attempts "
            "WHERE case_id = %s ORDER BY id DESC LIMIT 1",
            (cid,),
        ).fetchone()
        ref = str(reference[0]) if reference else ""
        if key == "reconciled_adopted":
            return cid, reconcile(app, cid, fetch=_found(
                ref, amount_minor=spec["amount"], status=LinkStatus.CREATED,
                correlation_id=f"plink_{ref[4:18]}",
            ))
        last = state
        for _ in range(4):
            last = reconcile(app, cid, fetch=None, max_polls=3)
            if last == CaseState.EXPIRED_UNRESOLVED.value:
                break
        return cid, last

    state = execute(app, cid, SeedProvider())
    if state != CaseState.AWAITING_CUSTOMER.value:
        return cid, state

    if key == "awaiting":
        return cid, state
    if key == "recovered":
        deliver_paid_webhook(app, cid)
        return cid, verify(verifier, app, cid)
    if key in {"deadline_escalated", "review_rejected"}:
        state = expire_deadline(app, cid)
        if key == "review_rejected":
            review_reject(app, cid, reviewer="ops.reviewer@reclaim.local")
        row = app.execute(
            "SELECT state::text FROM recovery_cases WHERE id = %s", (cid,)
        ).fetchone()
        return cid, str(row[0]) if row else state
    return cid, state


def looks_like_dev(url: str) -> bool:
    tail = url.rsplit("/", 1)[-1].split("?")[0].lower()
    return "dev" in tail or "local" in tail or "sandbox" in tail


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--app-url", default=os.environ.get(
        "DB_APP_URL",
        "postgresql://recovery_app:recovery_app_test@localhost:5432/reclaim_dev"))
    ap.add_argument("--verifier-url", default=os.environ.get(
        "DB_VERIFIER_URL",
        "postgresql://recovery_verifier:recovery_verifier_test@localhost:5432/reclaim_dev"))
    ap.add_argument("--admin-url", default=os.environ.get(
        "DATABASE_URL", "postgresql://postgres@localhost:5432/reclaim_dev"),
        help="owner connection, used only to clear tables before seeding")
    ap.add_argument("--truncate", action="store_true",
                    help="clear domain tables before seeding")
    ap.add_argument("--force", action="store_true",
                    help="allow a database whose name does not look like dev")
    args = ap.parse_args()

    if not looks_like_dev(args.app_url) and not args.force:
        print("refusing: target does not look like a development database.\n"
              "          pass --force only if you are certain.", file=sys.stderr)
        return 2

    with psycopg.connect(args.app_url, autocommit=True) as app, \
            psycopg.connect(args.verifier_url, autocommit=True) as verifier:
        if args.truncate:
            # Clearing tables is a setup concern and needs the owner role:
            # recovery_app is deliberately denied write access to verifications.
            with psycopg.connect(args.admin_url, autocommit=True) as admin:
                admin.execute(
                    "TRUNCATE verifications, provider_requests, execution_attempts, "
                    "recovery_actions, human_reviews, policy_decisions, diagnoses, "
                    "audit_events, webhook_events, recovery_cases, "
                    "financial_obligations RESTART IDENTITY CASCADE"
                )
                admin.execute("UPDATE circuit_breaker SET state='CLOSED', "
                              "consecutive_failures=0, opened_at=NULL, "
                              "reset_after=NULL, trip_cause=NULL WHERE id=1")

        print(f"seeding {len(SCENARIOS)} cases through real domain paths\n")
        results: list[tuple[str, int, str, str]] = []
        for spec in SCENARIOS:
            cid, state = run_scenario(app, verifier, spec)
            results.append((spec["order"], cid, state, spec["note"]))
            print(f"  case {cid:>3}  {spec['order']:<10} {state:<20} {spec['note']}")

        events = app.execute("SELECT count(*) FROM audit_events").fetchone()
        print(f"\n{len(results)} cases, {events[0] if events else 0} audit events")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
