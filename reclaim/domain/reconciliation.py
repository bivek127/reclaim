"""Reconciliation: converting unknown provider outcomes into known ones.

A case reaches AMBIGUOUS holding a persisted idempotency key and no knowledge
of whether money moved. This module resolves that, and only that.

Two network rounds, each `prepare -> COMMIT -> network -> settle`. No provider
call ever happens inside a transaction.

    ROUND 1 (always)   read-only GET by the persisted reference
    ROUND 2 (rare)     same-key re-POST, permitted ONLY when durable local
                       state proves the original POST never went out (ADR-013)

The pivotal judgement is NOT made from the provider's answer alone. `NOT_FOUND`
means different things depending on what our own committed rows say happened:

    attempt PREPARED  -> TXN 2 never ran -> the POST may never have gone out
    attempt UNKNOWN   -> TXN 2 ran       -> the POST definitely went out

Out of scope: verification, revenue, webhook correlation, policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from reclaim.config import load_operational
from reclaim.domain.execution import (
    Prepared,
    call_provider,
    settle_dispatch,
)
from reclaim.domain.leases import claim_next, fenced_transition
from reclaim.domain.states import CaseState
from reclaim.domain.transitions import transition
from reclaim.provider.contract import FetchOutcome, FetchResult, PaymentProvider

OPERATION_FETCH = "fetch_by_reference"
OPERATION_CREATE = "create_payment_link"

# Attempt states that mean "this attempt is still open" -- the same set as
# uq_action_one_open_attempt, so a reconcilable case always has exactly one.
OPEN_ATTEMPT_STATES = ("PREPARED", "IN_FLIGHT", "UNKNOWN")

# States a reconciler may claim. AMBIGUOUS is the normal entry.
# RECONCILING is an orphan left by a crashed predecessor: a GET is safely
# repeatable, so re-claiming and re-querying is correct and adds no new edge.
CLAIMABLE_STATES = (CaseState.AMBIGUOUS, CaseState.RECONCILING)


class ReconciliationBlocked(Exception):
    """The case cannot be reconciled as presented. Never a provider outcome."""


@dataclass(frozen=True)
class OpenAttempt:
    """The single open financial mechanism this case is waiting on."""

    attempt_id: int
    action_id: int
    case_id: int
    idempotency_key: str
    state: str
    amount_minor: int
    currency: str
    customer_ref: str
    expire_by: int | None

    @property
    def post_provably_sent(self) -> bool:
        """True when TXN 2 recorded an outcome, so the POST reached the wire.

        PREPARED means TXN 2 never ran: the request row is still IN_FLIGHT and
        the POST may never have left the process (a mid-dispatch crash).
        """
        return self.state != "PREPARED"


@dataclass(frozen=True)
class ReconcileResult:
    case_id: int
    fetch_outcome: FetchOutcome | None
    case_state: CaseState
    applied: bool
    adopted_correlation_id: str | None = None
    reposted: bool = False
    poll_count: int = 0
    expired: bool = False


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------


def claim_for_reconciliation(
    conn: psycopg.Connection, *, worker_id: str, lease_seconds: int
):
    """Claim an AMBIGUOUS case, or re-claim a RECONCILING orphan.

    Returns (Claim, source_state) or None. AMBIGUOUS is tried first so normal
    work is not starved by orphans.
    """
    for state in CLAIMABLE_STATES:
        claim = claim_next(conn, state, worker_id, lease_seconds)
        if claim is not None:
            return claim, state
    return None


def _enter_reconciling(
    conn: psycopg.Connection,
    *,
    case_id: int,
    source_state: CaseState,
    fencing_token: int,
    worker_id: str | None,
    side_effects: Any,
) -> bool:
    """AMBIGUOUS -> RECONCILING, or stay put if already RECONCILING.

    An orphan re-claim needs no transition: the case is already in the right
    state, and inventing an edge for it would be a second state machine. It is
    still fenced -- a stale token must not open a query row on a case another
    worker now owns.

    Uses fenced_transition so a stale token is *audited* as well as rejected,
    leaving a stale-write-rejected row in the trail.
    """
    if source_state is CaseState.RECONCILING:
        if not _token_is_current(conn, case_id, fencing_token, CaseState.RECONCILING):
            _record_stale_entry(conn, case_id, fencing_token, worker_id, source_state)
            return False
        side_effects(conn)
        return True
    return fenced_transition(
        conn,
        case_id,
        CaseState.AMBIGUOUS,
        CaseState.RECONCILING,
        fencing_token,
        "reconciliation_claimed",
        worker_id=worker_id,
        side_effects=side_effects,
    )


def _token_is_current(
    conn: psycopg.Connection, case_id: int, fencing_token: int, expected: CaseState
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM recovery_cases
         WHERE id = %s AND state = %s AND fencing_token = %s
        """,
        (case_id, expected.value, fencing_token),
    ).fetchone()
    return row is not None


def _record_stale_entry(
    conn: psycopg.Connection,
    case_id: int,
    fencing_token: int,
    worker_id: str | None,
    expected: CaseState,
) -> None:
    """Mirror leases._record_stale_write for the orphan-reclaim path."""
    conn.execute(
        """
        INSERT INTO audit_events (
            event_type, obligation_id, case_id, worker_id, fencing_token,
            prev_state, reason_code, detail
        )
        SELECT 'stale_write_rejected', c.obligation_id, %s, %s, %s, %s,
               'stale_write_rejected',
               jsonb_build_object('observed_token', %s::bigint,
                                  'current_token', c.fencing_token)
          FROM recovery_cases c WHERE c.id = %s
        """,
        (case_id, worker_id, fencing_token, expected.value, fencing_token, case_id),
    )


# ---------------------------------------------------------------------------
# Reading local truth
# ---------------------------------------------------------------------------


def open_attempt_for(conn: psycopg.Connection, case_id: int) -> OpenAttempt:
    row = conn.execute(
        f"""
        SELECT ea.id, ea.action_id, ea.case_id, ea.idempotency_key, ea.state,
               ea.amount_minor, ea.currency, o.customer_ref,
               EXTRACT(EPOCH FROM ra.provider_expires_at)::bigint
          FROM execution_attempts ea
          JOIN recovery_cases c        ON c.id = ea.case_id
          JOIN financial_obligations o ON o.id = c.obligation_id
          JOIN recovery_actions ra     ON ra.id = ea.action_id
         WHERE ea.case_id = %s
           AND ea.state IN {OPEN_ATTEMPT_STATES}
         ORDER BY ea.id DESC
         LIMIT 1
        """,
        (case_id,),
    ).fetchone()
    if row is None:
        raise ReconciliationBlocked(f"case {case_id} has no open execution attempt")
    return OpenAttempt(
        attempt_id=int(row[0]),
        action_id=int(row[1]),
        case_id=int(row[2]),
        idempotency_key=str(row[3]),
        state=str(row[4]),
        amount_minor=int(row[5]),
        currency=str(row[6]),
        customer_ref=str(row[7]),
        expire_by=int(row[8]) if row[8] is not None else None,
    )


def poll_count(conn: psycopg.Connection, case_id: int) -> int:
    """The poll bound counts provider GETs only -- never re-POSTs, never attempts."""
    return _request_count(conn, case_id=case_id, operation=OPERATION_FETCH)


def post_count(conn: psycopg.Connection, attempt_id: int) -> int:
    """A separate bound: financial POST retries within one attempt."""
    row = conn.execute(
        """
        SELECT count(*) FROM provider_requests
         WHERE attempt_id = %s AND operation = %s
        """,
        (attempt_id, OPERATION_CREATE),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _request_count(conn: psycopg.Connection, *, case_id: int, operation: str) -> int:
    row = conn.execute(
        """
        SELECT count(*)
          FROM provider_requests pr
          JOIN execution_attempts ea ON ea.id = pr.attempt_id
         WHERE ea.case_id = %s AND pr.operation = %s
        """,
        (case_id, operation),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _next_request_no(conn: psycopg.Connection, attempt_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(request_no), 0) + 1 FROM provider_requests WHERE attempt_id = %s",
        (attempt_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _insert_request(
    conn: psycopg.Connection,
    *,
    attempt: OpenAttempt,
    operation: str,
    body: dict[str, Any],
) -> int:
    row = conn.execute(
        """
        INSERT INTO provider_requests (
            attempt_id, operation, request_no, idempotency_key,
            request_body, outcome
        ) VALUES (%s, %s, %s, %s, %s, 'IN_FLIGHT')
        RETURNING id
        """,
        (
            attempt.attempt_id,
            operation,
            _next_request_no(conn, attempt.attempt_id),
            attempt.idempotency_key,
            Jsonb(body),
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# Evidence classification
# ---------------------------------------------------------------------------


def classify(fetch: FetchResult, attempt: OpenAttempt) -> tuple[CaseState, str]:
    """Map provider evidence + local state to a destination and a reason code.

    The three outcomes are deliberately asymmetric:

      FOUND       the mechanism exists -> adopt it, whatever its status.
                  No LinkStatus is terminal-failure evidence: expiry is
                  unverified for non-payment (ADR-006) and CANCELLED has the
                  same unverified-finality problem.
      NOT_FOUND   authoritative ONLY when the POST provably went out. Then it
                  means the provider created nothing -> confirmed failure.
                  Otherwise it is inconclusive and round 2 handles it.
      NO_EVIDENCE never evidence of anything. Poll again.
    """
    if fetch.outcome is FetchOutcome.FOUND:
        if not fetch.provider_correlation_id:
            # Found but unusable: contradictory, not optimism.
            return CaseState.AMBIGUOUS, "reconcile_found_unusable"
        if fetch.amount_minor is not None and fetch.amount_minor != attempt.amount_minor:
            # An amount that disagrees with the attempt is a contradiction.
            return CaseState.AMBIGUOUS, "reconcile_amount_mismatch"
        return CaseState.AWAITING_CUSTOMER, "reconcile_adopted"

    if fetch.outcome is FetchOutcome.NOT_FOUND:
        if attempt.post_provably_sent:
            return CaseState.ATTEMPT_FAILED, "reconcile_confirmed_failed"
        return CaseState.AMBIGUOUS, "reconcile_inconclusive_not_found"

    return CaseState.AMBIGUOUS, "reconcile_no_evidence"


def _fetch_outcome_enum(outcome: FetchOutcome) -> str:
    return outcome.value


# ---------------------------------------------------------------------------
# Round 1 -- the read
# ---------------------------------------------------------------------------


def reconcile_case(
    conn: psycopg.Connection,
    case_id: int,
    *,
    provider: PaymentProvider,
    fencing_token: int,
    source_state: CaseState = CaseState.AMBIGUOUS,
    worker_id: str | None = None,
    max_polls: int | None = None,
    max_posts: int | None = None,
) -> ReconcileResult:
    """One reconciliation cycle. Network calls sit strictly between commits."""
    operational = load_operational()
    polls_allowed = (
        max_polls if max_polls is not None
        else int(operational["reconciliation_max_polls"])
    )
    posts_allowed = (
        max_posts if max_posts is not None
        else int(operational["reconciliation_max_posts_per_attempt"])
    )

    attempt = open_attempt_for(conn, case_id)

    # Poll cap, checked before any network work.
    if poll_count(conn, case_id) >= polls_allowed:
        return _expire_unresolved(
            conn,
            case_id=case_id,
            source_state=source_state,
            fencing_token=fencing_token,
            polls=poll_count(conn, case_id),
        )

    # --- TXN A: enter RECONCILING and record the query we are about to make ---
    request_id: dict[str, int] = {}

    def _open_query(inner: psycopg.Connection) -> None:
        request_id["id"] = _insert_request(
            inner,
            attempt=attempt,
            operation=OPERATION_FETCH,
            body={"operation": OPERATION_FETCH, "reference_id": attempt.idempotency_key},
        )
        _audit(
            inner,
            event_type="reconciliation_query_sent",
            case_id=case_id,
            attempt=attempt,
            provider_request_id=request_id["id"],
            worker_id=worker_id,
            fencing_token=fencing_token,
            reason_code="reconciliation_query_sent",
            detail={"reference_id": attempt.idempotency_key},
        )

    with conn.transaction():
        entered = _enter_reconciling(
            conn,
            case_id=case_id,
            source_state=source_state,
            fencing_token=fencing_token,
            worker_id=worker_id,
            side_effects=_open_query,
        )
    if not entered:
        return ReconcileResult(
            case_id=case_id,
            fetch_outcome=None,
            case_state=source_state,
            applied=False,
        )

    # ---------------------- network boundary: read-only ----------------------
    fetch = provider.fetch_by_reference(reference_id=attempt.idempotency_key)
    # -------------------------------------------------------------------------

    target, reason = classify(fetch, attempt)

    # --- TXN B: record the answer and move the case -------------------------
    def _settle_query(inner: psycopg.Connection) -> None:
        _finish_query(inner, request_id["id"], fetch)
        if target is CaseState.AWAITING_CUSTOMER:
            _adopt(inner, attempt=attempt, fetch=fetch)
        elif target is CaseState.ATTEMPT_FAILED:
            _mark_terminal_failed(inner, attempt=attempt)
        _audit(
            inner,
            event_type="reconciliation_result",
            case_id=case_id,
            attempt=attempt,
            provider_request_id=request_id["id"],
            worker_id=worker_id,
            fencing_token=fencing_token,
            reason_code=reason,
            provider_correlation_id=fetch.provider_correlation_id,
            detail={
                "fetch_outcome": fetch.outcome.value,
                "link_status": fetch.link_status.value if fetch.link_status else None,
                "post_provably_sent": attempt.post_provably_sent,
            },
        )

    with conn.transaction():
        applied = _fenced(
            conn,
            case_id=case_id,
            expected=CaseState.RECONCILING,
            target=target,
            fencing_token=fencing_token,
            reason=reason,
            worker_id=worker_id,
            side_effects=_settle_query,
        )

    if not applied:
        # Lost the lease mid-flight. Discard, never re-apply.
        return ReconcileResult(
            case_id=case_id,
            fetch_outcome=fetch.outcome,
            case_state=CaseState.RECONCILING,
            applied=False,
        )

    # --- Round 2: same-key re-POST, only when provably safe (ADR-013) --------
    if reason == "reconcile_inconclusive_not_found":
        if post_count(conn, attempt.attempt_id) < posts_allowed:
            return _repost(
                conn,
                attempt=attempt,
                provider=provider,
                fencing_token=fencing_token,
                worker_id=worker_id,
            )

    return ReconcileResult(
        case_id=case_id,
        fetch_outcome=fetch.outcome,
        case_state=target,
        applied=True,
        adopted_correlation_id=fetch.provider_correlation_id
        if target is CaseState.AWAITING_CUSTOMER
        else None,
        poll_count=poll_count(conn, case_id),
    )


def _fenced(
    conn: psycopg.Connection,
    *,
    case_id: int,
    expected: CaseState,
    target: CaseState,
    fencing_token: int,
    reason: str,
    worker_id: str | None,
    side_effects: Any,
) -> bool:
    """RECONCILING -> target. Self-transitions are not legal edges, so a
    'stay AMBIGUOUS' result still moves RECONCILING -> AMBIGUOUS."""
    return fenced_transition(
        conn,
        case_id,
        expected,
        target,
        fencing_token,
        reason,
        worker_id=worker_id,
        side_effects=side_effects,
    )


# ---------------------------------------------------------------------------
# Round 2 -- the bounded same-key re-POST (ADR-013)
# ---------------------------------------------------------------------------


def _repost(
    conn: psycopg.Connection,
    *,
    attempt: OpenAttempt,
    provider: PaymentProvider,
    fencing_token: int,
    worker_id: str | None,
) -> ReconcileResult:
    """Re-send the original POST under the ORIGINAL key.

    Creates no action row, no attempt row, and claims no budget -- it is a
    retry of the same financial mechanism, not a new one, which is why I4/I5
    still hold. A new idempotency key is never generated here.
    """
    # The case is back in AMBIGUOUS after round 1's write-back; re-enter
    # RECONCILING so it is never dispatchable while the POST is in flight.
    request_id: dict[str, int] = {}

    def _open_post(inner: psycopg.Connection) -> None:
        request_id["id"] = _insert_request(
            inner,
            attempt=attempt,
            operation=OPERATION_CREATE,
            body={
                "operation": OPERATION_CREATE,
                "reference_id": attempt.idempotency_key,
                "amount": attempt.amount_minor,
                "currency": attempt.currency,
                "retry_of_lost_dispatch": True,
            },
        )
        _audit(
            inner,
            event_type="reconciliation_repost_sent",
            case_id=attempt.case_id,
            attempt=attempt,
            provider_request_id=request_id["id"],
            worker_id=worker_id,
            fencing_token=fencing_token,
            reason_code="reconciliation_repost_sent",
            detail={"reference_id": attempt.idempotency_key, "same_key": True},
        )

    with conn.transaction():
        entered = transition(
            conn,
            attempt.case_id,
            CaseState.AMBIGUOUS,
            CaseState.RECONCILING,
            fencing_token,
            "reconciliation_repost",
            side_effects=_open_post,
        )
    if not entered:
        return ReconcileResult(
            case_id=attempt.case_id,
            fetch_outcome=FetchOutcome.NOT_FOUND,
            case_state=CaseState.AMBIGUOUS,
            applied=False,
        )

    prepared = Prepared(
        case_id=attempt.case_id,
        action_id=attempt.action_id,
        attempt_id=attempt.attempt_id,
        request_id=request_id["id"],
        idempotency_key=attempt.idempotency_key,  # never regenerated
        amount_minor=attempt.amount_minor,
        currency=attempt.currency,
        customer_ref=attempt.customer_ref,
        expire_by=attempt.expire_by or 0,
        attempt_count=0,
    )

    # ------------------- network boundary: the financial POST ----------------
    result = call_provider(provider, prepared)
    # -------------------------------------------------------------------------

    settled = settle_dispatch(
        conn,
        prepared,
        result,
        fencing_token=fencing_token,
        worker_id=worker_id,
        expected_state=CaseState.RECONCILING,
    )

    return ReconcileResult(
        case_id=attempt.case_id,
        fetch_outcome=FetchOutcome.NOT_FOUND,
        case_state=settled.case_state,
        applied=settled.applied,
        adopted_correlation_id=settled.provider_correlation_id,
        reposted=True,
    )


# ---------------------------------------------------------------------------
# Row writes
# ---------------------------------------------------------------------------


def _finish_query(
    conn: psycopg.Connection, request_id: int, fetch: FetchResult
) -> None:
    body: dict[str, Any] = {"fetch_outcome": fetch.outcome.value}
    if fetch.response_body is not None:
        body["response"] = fetch.response_body
    if fetch.error_class is not None:
        body["error_class"] = fetch.error_class.value
    if fetch.link_status is not None:
        body["link_status"] = fetch.link_status.value

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
            _fetch_outcome_enum(fetch.outcome),
            fetch.http_status,
            Jsonb(body),
            fetch.provider_correlation_id,
            request_id,
        ),
    )


def _adopt(
    conn: psycopg.Connection, *, attempt: OpenAttempt, fetch: FetchResult
) -> None:
    """Attach the existing mechanism. Creates nothing; charges no budget."""
    conn.execute(
        """
        UPDATE execution_attempts
           SET state = 'ACCEPTED',
               settled_at = COALESCE(settled_at, now())
         WHERE id = %s
        """,
        (attempt.attempt_id,),
    )
    conn.execute(
        "UPDATE recovery_actions SET status = 'LIVE' WHERE id = %s AND status <> 'LIVE'",
        (attempt.action_id,),
    )


def _mark_terminal_failed(conn: psycopg.Connection, *, attempt: OpenAttempt) -> None:
    """The reconciler positively confirmed the provider created nothing."""
    conn.execute(
        """
        UPDATE execution_attempts
           SET state = 'REJECTED',
               settled_at = COALESCE(settled_at, now())
         WHERE id = %s
        """,
        (attempt.attempt_id,),
    )
    conn.execute(
        """
        UPDATE recovery_actions
           SET status = 'TERMINAL_FAILED',
               resolved_at = COALESCE(resolved_at, now())
         WHERE id = %s
        """,
        (attempt.action_id,),
    )


def _expire_unresolved(
    conn: psycopg.Connection,
    *,
    case_id: int,
    source_state: CaseState,
    fencing_token: int,
    polls: int,
) -> ReconcileResult:
    """Poll budget exhausted. Distinct from VERIFIED_FAILED, flagged for review."""
    with conn.transaction():
        applied = transition(
            conn,
            case_id,
            source_state,
            CaseState.EXPIRED_UNRESOLVED,
            fencing_token,
            "reconciliation_polls_exhausted",
        )
    return ReconcileResult(
        case_id=case_id,
        fetch_outcome=None,
        case_state=CaseState.EXPIRED_UNRESOLVED if applied else source_state,
        applied=applied,
        poll_count=polls,
        expired=True,
    )


def _audit(
    conn: psycopg.Connection,
    *,
    event_type: str,
    case_id: int,
    attempt: OpenAttempt,
    provider_request_id: int,
    worker_id: str | None,
    fencing_token: int,
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
            attempt.action_id,
            attempt.attempt_id,
            provider_request_id,
            worker_id,
            fencing_token,
            reason_code,
            provider_correlation_id,
            Jsonb(detail or {}),
            case_id,
        ),
    )


# ---------------------------------------------------------------------------
# Job entry point: periodic poll over AMBIGUOUS with an expired lease
# ---------------------------------------------------------------------------


def reconcile_once(
    conn: psycopg.Connection,
    *,
    provider: PaymentProvider,
    worker_id: str = "reconciler",
    lease_seconds: int | None = None,
) -> ReconcileResult | None:
    """Claim one case and run a cycle. Returns None when nothing is claimable."""
    from reclaim.config import lease_seconds_for

    lease = lease_seconds or lease_seconds_for("reconciliation")
    claimed = claim_for_reconciliation(conn, worker_id=worker_id, lease_seconds=lease)
    if claimed is None:
        return None
    claim, source_state = claimed
    return reconcile_case(
        conn,
        claim.case_id,
        provider=provider,
        fencing_token=claim.fencing_token,
        source_state=source_state,
        worker_id=worker_id,
    )
