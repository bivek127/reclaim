"""HTTP adapter for the operations console.

Thin by construction. Every route does three things and nothing more: validate
input at the boundary, call one existing domain or read-model function, and
translate the outcome into a status code. No route decides business meaning,
recomputes money, or performs a state transition of its own.

Route -> operation it delegates to:

    GET  /api/health                 -- liveness only
    GET  /api/overview               -- readmodel.overview
    GET  /api/cases                  -- readmodel.list_cases
    GET  /api/cases/{id}             -- readmodel.get_case
    GET  /api/cases/{id}/timeline    -- audit.load_case_audit_trail
                                        + audit.reconstruct_case_history
    GET  /api/reviews                -- readmodel.list_reviews
    GET  /api/reviews/{id}           -- domain.load_review_evidence
    GET  /api/unmappable             -- readmodel.list_unmappable_webhooks
    POST /api/reviews/{id}/approve   -- domain.claim_case + domain.approve_review
    POST /api/reviews/{id}/reject    -- domain.claim_case + domain.reject_review
    GET  /api/system                 -- readmodel.system_status
    POST /api/webhooks/razorpay      -- ingest.ingest_webhook

The two write routes compose `claim_case` with the matching review primitive,
which is what `approve_once` already does internally. Fencing, expected-state
checks, the state transition, and the audit row all remain inside the domain.
"""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from reclaim import readmodel
from reclaim.api.db import app_conn, environment_label
from reclaim.api.serializers import case_history, plain
from reclaim.audit import load_case_audit_trail, reconstruct_case_history
from reclaim.config import lease_seconds_for
from reclaim.domain import (
    ReviewBlocked,
    approve_review,
    claim_case,
    load_review_evidence,
    reject_review,
)
from reclaim.domain.states import CASE_STATES, CaseState
from reclaim.ingest import ingest_webhook
from reclaim.provider.config import load_provider_config
from reclaim.provider.razorpay import verify_webhook_signature

# Only actions the executor can actually dispatch may be offered to a reviewer.
# RETRY_CHARGE has no safe provider implementation and the executor raises on
# it, so it is never presented as a choice.
REVIEWABLE_ACTIONS = ("CREATE_PAYMENT_LINK",)

# The partial unique index enforcing "at most one open action per case". A
# violation of it is a refusal the domain intends, not a fault, so this layer
# gives it the conflict status it deserves. Matched by index name rather than
# by message text, and never widened to unique violations in general: any other
# constraint failing here is an unexpected fault and must stay one.
ONE_OPEN_ACTION_INDEX = "uq_case_one_open_action"

app = FastAPI(title="Reclaim Operations API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:4000"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class ReviewDecision(BaseModel):
    reviewer_ref: str = Field(min_length=1, max_length=200)
    selected_action: str | None = None


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "environment": environment_label()}


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    """Vocabulary the console renders. Sourced from the domain, never retyped."""
    return {
        "environment": environment_label(),
        "case_states": [s.value for s in CASE_STATES],
        "attention_states": list(readmodel.ATTENTION_STATES),
        "in_flight_states": list(readmodel.IN_FLIGHT_STATES),
        "reviewable_actions": list(REVIEWABLE_ACTIONS),
    }


@app.get("/api/overview")
def get_overview() -> dict[str, Any]:
    with app_conn() as conn:
        return plain(readmodel.overview(conn))


@app.get("/api/cases")
def get_cases(
    state: list[str] | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    needs_attention: bool = False,
    has_pending_review: bool = False,
    sort: str = Query(default="updated_at"),
    direction: str = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    requested = tuple(state or ())
    unknown = [s for s in requested if s not in readmodel.VALID_STATES]
    if unknown:
        raise HTTPException(400, f"unknown case state(s): {', '.join(unknown)}")
    with app_conn() as conn:
        page = readmodel.list_cases(
            conn,
            states=requested,
            query=q.strip() if q and q.strip() else None,
            needs_attention=needs_attention,
            has_pending_review=has_pending_review,
            sort=sort,
            direction=direction,
            limit=limit,
            offset=offset,
        )
    return plain(page)


@app.get("/api/cases/{case_id}")
def get_case(case_id: int) -> dict[str, Any]:
    with app_conn() as conn:
        detail = readmodel.get_case(conn, case_id)
    if detail is None:
        raise HTTPException(404, f"case {case_id} not found")
    return plain(detail)


@app.get("/api/cases/{case_id}/timeline")
def get_timeline(case_id: int) -> dict[str, Any]:
    """Reconstruction from `audit_events` alone.

    No production table is joined in to fill a gap; where the trail cannot
    supply a fact, the response says so via `unreconstructable`.
    """
    with app_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM recovery_cases WHERE id = %s", (case_id,)
        ).fetchone()
        if exists is None:
            raise HTTPException(404, f"case {case_id} not found")
        history = reconstruct_case_history(load_case_audit_trail(conn, case_id))
    return case_history(history)


@app.get("/api/reviews")
def get_reviews(
    status: str = Query(default="PENDING"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    with app_conn() as conn:
        rows, total = readmodel.list_reviews(
            conn, status=status, limit=limit, offset=offset
        )
    return {"rows": plain(list(rows)), "total": total,
            "limit": limit, "offset": offset, "status": status.upper()}


@app.get("/api/reviews/{case_id}")
def get_review_evidence(case_id: int) -> dict[str, Any]:
    with app_conn() as conn:
        try:
            evidence = load_review_evidence(conn, case_id)
        except ReviewBlocked as exc:
            raise HTTPException(404, str(exc)) from exc
        detail = readmodel.get_case(conn, case_id)
    payload = plain(evidence)
    payload["case_state"] = detail.case.state if detail else None
    payload["reviewable_actions"] = list(REVIEWABLE_ACTIONS)
    return payload


def _decide(case_id: int, body: ReviewDecision, *, approve: bool) -> dict[str, Any]:
    """Claim the case, then hand off to the review primitive.

    A refusal is surfaced as the domain reported it. The API never retries with
    a fresh token and never reports success the domain did not grant.
    """
    if approve:
        action = (body.selected_action or "").strip()
        if action not in REVIEWABLE_ACTIONS:
            raise HTTPException(
                400,
                f"selected_action must be one of {', '.join(REVIEWABLE_ACTIONS)}",
            )

    with app_conn() as conn:
        claim = claim_case(
            conn, case_id, CaseState.ESCALATED, "console",
            lease_seconds_for("review"),
        )
        if claim is None:
            raise HTTPException(
                409,
                "This case is not awaiting review, or another worker holds it.",
            )
        try:
            if approve:
                result = approve_review(
                    conn, case_id,
                    selected_action=body.selected_action or "",
                    reviewer_ref=body.reviewer_ref,
                    fencing_token=claim.fencing_token,
                    worker_id="console",
                )
            else:
                result = reject_review(
                    conn, case_id,
                    reviewer_ref=body.reviewer_ref,
                    fencing_token=claim.fencing_token,
                    worker_id="console",
                )
        except ReviewBlocked as exc:
            raise HTTPException(409, str(exc)) from exc
        except psycopg.errors.UniqueViolation as exc:
            if exc.diag.constraint_name != ONE_OPEN_ACTION_INDEX:
                raise
            # The domain transaction rolled back: the review is still PENDING,
            # the existing action is untouched, and no second one was created.
            raise HTTPException(
                409,
                "This case already has an open recovery action. Only one action "
                "may be open at a time, so another cannot be proposed until the "
                "current one resolves.",
            ) from exc
        finally:
            _release_lease(conn, case_id, claim.fencing_token)

    if not result.applied:
        raise HTTPException(
            409,
            "This case changed while the decision was being made; "
            "reload to see its current state.",
        )
    return plain(result)


def _release_lease(conn: psycopg.Connection, case_id: int, token: int) -> None:
    """Drop the console's lease so background workers are not blocked.

    Fenced on the token this request claimed, so a lease taken by someone else
    in the meantime is left alone.
    """
    conn.execute(
        "UPDATE recovery_cases SET worker_id = NULL, lease_expires_at = '-infinity' "
        "WHERE id = %s AND worker_id = 'console' AND fencing_token = %s",
        (case_id, token),
    )


@app.post("/api/reviews/{case_id}/approve")
def post_approve(case_id: int, body: ReviewDecision) -> dict[str, Any]:
    return _decide(case_id, body, approve=True)


@app.post("/api/reviews/{case_id}/reject")
def post_reject(case_id: int, body: ReviewDecision) -> dict[str, Any]:
    return _decide(case_id, body, approve=False)


# Razorpay does not publish the event id in the webhook body, so the transport
# has to take it from a header. `X-Razorpay-Event-Id` is an UNVERIFIED provider
# assumption: it has not been observed against a live delivery in this project,
# unlike the signature header. It is named here rather than guessed at because
# `provider_event_id` is the outermost idempotency key -- `uq_provider_event`
# dedupes on it, and a synthesised value would silently change what "the same
# delivery" means.
WEBHOOK_EVENT_ID_HEADER = "X-Razorpay-Event-Id"


@app.post("/api/webhooks/razorpay")
async def post_razorpay_webhook(
    request: Request,
    x_razorpay_signature: str | None = Header(default=None),
    x_razorpay_event_id: str | None = Header(default=None),
) -> Response:
    """Verify the delivery, then hand the untrusted bytes to the domain.

    Ordering is the security property: the signature is checked over the exact
    received bytes before anything reads them. This route never parses the
    payload -- `ingest_webhook` does, after it has been told the signature held.

    An event id that is missing or blank is refused before any database work.
    Inventing one would let two unidentifiable deliveries create two cases for
    one obligation, which is precisely what the unique index prevents when a
    real id is present.
    """
    event_id = (x_razorpay_event_id or "").strip()
    if not event_id:
        raise HTTPException(400, f"{WEBHOOK_EVENT_ID_HEADER} is required")

    # The body is read as bytes and never decoded, re-encoded or parsed here:
    # the signature covers exactly what arrived, and any normalisation on the
    # way in would verify something the sender did not sign.
    body = await request.body()
    secret = load_provider_config().webhook_secret or ""
    signature_valid = verify_webhook_signature(body, x_razorpay_signature or "", secret)

    # psycopg is blocking, so the domain call runs off the event loop.
    result = await run_in_threadpool(_ingest, signature_valid, body, event_id)

    # The domain decides the status: 400 for a rejected signature, 200 for
    # everything it accepted, including a duplicate or a malformed payload it
    # chose to record. The transport does not second-guess that.
    return Response(status_code=result.http_status)


def _ingest(signature_valid: bool, body: bytes, event_id: str) -> Any:
    with app_conn() as conn:
        return ingest_webhook(
            conn,
            signature_valid=signature_valid,
            raw_body=body,
            provider_event_id=event_id,
        )


@app.get("/api/unmappable")
def get_unmappable(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    with app_conn() as conn:
        rows, total = readmodel.list_unmappable_webhooks(
            conn, limit=limit, offset=offset
        )
    return {"rows": plain(list(rows)), "total": total,
            "limit": limit, "offset": offset}


@app.get("/api/system")
def get_system() -> dict[str, Any]:
    with app_conn() as conn:
        return plain(readmodel.system_status(conn))
