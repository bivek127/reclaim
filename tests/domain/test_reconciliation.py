"""Reconciliation: evidence model, poll/post bounds, and dead-key detection (I3, I4)."""

from __future__ import annotations

import psycopg
import pytest
from psycopg.errors import UniqueViolation

from reclaim.domain.execution import call_provider, prepare_dispatch, settle_dispatch
from reclaim.domain.leases import claim_case
from reclaim.domain.reconciliation import (
    OPERATION_CREATE,
    OPERATION_FETCH,
    classify,
    open_attempt_for,
    poll_count,
    post_count,
    reconcile_case,
)
from reclaim.domain.states import CaseState, is_allowed
from reclaim.provider.contract import ErrorClass, FetchOutcome, LinkStatus, ProviderOutcome
from tests.db.helpers import insert_action, insert_attempt
from tests.domain.execution_helpers import (
    LINK_TTL_SECONDS,
    StubProvider,
    StubReconcileProvider,
    actions_for,
    attempts_for,
    case_row,
    fetch_found,
    fetch_no_evidence,
    fetch_not_found,
    requests_for,
    seed_dispatchable,
)


def _to_ambiguous_lost_response(conn, ids, *, sent: bool = True):
    """Drive a case to AMBIGUOUS the way a real dispatch would.

    sent=True  -> TXN 2 ran with an unknown outcome (attempt UNKNOWN).
    sent=False -> TXN 2 never ran (attempt PREPARED), then the sweeper moves it.
    """
    claim = claim_case(conn, ids["case_id"], CaseState.ACTION_READY, "w-exec", 60)
    assert claim is not None
    prepared = prepare_dispatch(
        conn,
        ids["case_id"],
        fencing_token=claim.fencing_token,
        policy_decision_id=ids["policy_id"],
        link_ttl_seconds=LINK_TTL_SECONDS,
        worker_id="w-exec",
    )
    if sent:
        result = call_provider(StubProvider(ProviderOutcome.TIMEOUT), prepared)
        settle_dispatch(
            conn, prepared, result, fencing_token=claim.fencing_token, worker_id="w-exec"
        )
        token = claim.fencing_token
    else:
        from reclaim.domain.sweeper import sweep_expired_leases

        conn.execute(
            "UPDATE recovery_cases SET lease_expires_at = now() - interval '1 s' "
            "WHERE id = %s",
            (ids["case_id"],),
        )
        sweep_expired_leases(conn)
        token = case_row(conn, ids["case_id"])["fencing_token"]
    assert case_row(conn, ids["case_id"])["state"] == "AMBIGUOUS"
    return prepared, token


# ---- Row 6: provider success, response lost -> adopt ---------------------


def test_provider_success_lost_response_adopts(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids)
    provider = StubReconcileProvider(fetch_found(correlation_id="plink_Adopted1"))

    result = reconcile_case(
        conn, ids["case_id"], provider=provider, fencing_token=token
    )

    assert result.case_state is CaseState.AWAITING_CUSTOMER
    assert case_row(conn, ids["case_id"])["state"] == "AWAITING_CUSTOMER"
    assert result.adopted_correlation_id == "plink_Adopted1"


def test_adoption_creates_no_second_mechanism(conn: psycopg.Connection) -> None:
    """Row 6 forbids a 2nd link and a budget increment."""
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids)
    before = case_row(conn, ids["case_id"])["attempt_count"]
    provider = StubReconcileProvider(fetch_found())

    reconcile_case(conn, ids["case_id"], provider=provider, fencing_token=token)

    assert len(attempts_for(conn, ids["case_id"])) == 1
    assert len(actions_for(conn, ids["case_id"])) == 1
    assert case_row(conn, ids["case_id"])["attempt_count"] == before
    assert provider.create_calls == [], "reconciliation must not POST on FOUND"


def test_adoption_marks_attempt_accepted_and_action_live(
    conn: psycopg.Connection,
) -> None:
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids)

    reconcile_case(
        conn, ids["case_id"], provider=StubReconcileProvider(fetch_found()),
        fencing_token=token,
    )

    assert attempts_for(conn, ids["case_id"])[0]["state"] == "ACCEPTED"
    action = actions_for(conn, ids["case_id"])[0]
    assert action["status"] == "LIVE"
    assert action["resolved_at"] is None


@pytest.mark.parametrize("status", list(LinkStatus))
def test_every_link_status_adopts_none_is_terminal(
    conn: psycopg.Connection, status: LinkStatus
) -> None:
    """No provider-side status is terminal-failure evidence (ADR-006)."""
    ids = seed_dispatchable(conn, suffix=f"ls{status.value}")
    _, token = _to_ambiguous_lost_response(conn, ids)

    result = reconcile_case(
        conn,
        ids["case_id"],
        provider=StubReconcileProvider(fetch_found(link_status=status)),
        fencing_token=token,
    )

    assert result.case_state is CaseState.AWAITING_CUSTOMER
    assert actions_for(conn, ids["case_id"])[0]["status"] != "TERMINAL_FAILED"


def test_amount_mismatch_is_contradictory_not_adopted(
    conn: psycopg.Connection,
) -> None:
    """A re-query amount that disagrees is a contradiction, not a match."""
    ids = seed_dispatchable(conn, amount_minor=10_000)
    _, token = _to_ambiguous_lost_response(conn, ids)

    result = reconcile_case(
        conn,
        ids["case_id"],
        provider=StubReconcileProvider(fetch_found(amount_minor=99_999)),
        fencing_token=token,
    )

    assert result.case_state is CaseState.AMBIGUOUS
    assert attempts_for(conn, ids["case_id"])[0]["state"] == "UNKNOWN"


# ---- provider failure, response lost -> confirmed failed -----------------


def test_provider_failure_lost_response_resolves_failed(
    conn: psycopg.Connection,
) -> None:
    """attempt UNKNOWN + NOT_FOUND = the POST went out and created nothing."""
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids, sent=True)

    result = reconcile_case(
        conn, ids["case_id"], provider=StubReconcileProvider(fetch_not_found()),
        fencing_token=token,
    )

    assert result.case_state is CaseState.ATTEMPT_FAILED
    assert case_row(conn, ids["case_id"])["state"] == "ATTEMPT_FAILED"


def test_confirmed_failure_marks_action_terminal(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids, sent=True)

    reconcile_case(
        conn, ids["case_id"], provider=StubReconcileProvider(fetch_not_found()),
        fencing_token=token,
    )

    action = actions_for(conn, ids["case_id"])[0]
    assert action["status"] == "TERMINAL_FAILED"
    assert action["resolved_at"] is not None
    assert attempts_for(conn, ids["case_id"])[0]["state"] == "REJECTED"


def test_confirmed_failure_never_reaches_verified_failed(
    conn: psycopg.Connection,
) -> None:
    """A confirmed provider failure never reaches VERIFIED_FAILED --
    verification is a separate, independent module's job."""
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids, sent=True)

    reconcile_case(
        conn, ids["case_id"], provider=StubReconcileProvider(fetch_not_found()),
        fencing_token=token,
    )

    assert case_row(conn, ids["case_id"])["state"] != "VERIFIED_FAILED"


def test_confirmed_failure_makes_no_post(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids, sent=True)
    provider = StubReconcileProvider(fetch_not_found())

    reconcile_case(conn, ids["case_id"], provider=provider, fencing_token=token)

    assert provider.create_calls == []


# ---- I3: no evidence never becomes failure -------------------------------


@pytest.mark.parametrize(
    "error_class",
    [
        ErrorClass.TIMEOUT,
        ErrorClass.TRANSIENT_PROVIDER,
        ErrorClass.RATE_LIMIT,
        ErrorClass.AUTHENTICATION,
        ErrorClass.MALFORMED_RESPONSE,
        ErrorClass.NETWORK,
        ErrorClass.VALIDATION,
    ],
)
def test_no_evidence_never_resolves(
    conn: psycopg.Connection, error_class: ErrorClass
) -> None:
    """timeout / 5xx / 429 / auth / malformed are NOT 'not found'."""
    ids = seed_dispatchable(conn, suffix=f"ne{error_class.value}")
    _, token = _to_ambiguous_lost_response(conn, ids)

    result = reconcile_case(
        conn,
        ids["case_id"],
        provider=StubReconcileProvider(fetch_no_evidence(error_class)),
        fencing_token=token,
    )

    assert result.case_state is CaseState.AMBIGUOUS
    assert case_row(conn, ids["case_id"])["state"] == "AMBIGUOUS"
    assert attempts_for(conn, ids["case_id"])[0]["state"] == "UNKNOWN"
    assert actions_for(conn, ids["case_id"])[0]["status"] == "UNRESOLVED"


def test_no_evidence_leaves_action_unresolved_forever(
    conn: psycopg.Connection,
) -> None:
    """Repeated no-evidence polls must never drift toward a verdict."""
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids)

    for _ in range(5):
        reconcile_case(
            conn,
            ids["case_id"],
            provider=StubReconcileProvider(fetch_no_evidence()),
            fencing_token=token,
        )
        token = case_row(conn, ids["case_id"])["fencing_token"]

    assert case_row(conn, ids["case_id"])["state"] == "AMBIGUOUS"
    assert actions_for(conn, ids["case_id"])[0]["status"] == "UNRESOLVED"


def test_classify_is_total_and_never_optimistic() -> None:
    """Only FOUND may produce AWAITING_CUSTOMER."""
    from reclaim.domain.reconciliation import OpenAttempt

    sent = OpenAttempt(1, 1, 1, "rcv_k", "UNKNOWN", 10_000, "INR", "c", None)
    unsent = OpenAttempt(1, 1, 1, "rcv_k", "PREPARED", 10_000, "INR", "c", None)

    for attempt in (sent, unsent):
        assert classify(fetch_no_evidence(), attempt)[0] is CaseState.AMBIGUOUS
    assert classify(fetch_found(), sent)[0] is CaseState.AWAITING_CUSTOMER
    assert classify(fetch_not_found(), sent)[0] is CaseState.ATTEMPT_FAILED
    assert classify(fetch_not_found(), unsent)[0] is CaseState.AMBIGUOUS


# ---- NOT_FOUND is not globally equivalent to failure ---------------------


def test_not_found_with_unsent_post_does_not_fail_the_case(
    conn: psycopg.Connection,
) -> None:
    """attempt PREPARED: the POST may never have gone out. Never ATTEMPT_FAILED."""
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids, sent=False)
    assert attempts_for(conn, ids["case_id"])[0]["state"] == "PREPARED"

    result = reconcile_case(
        conn,
        ids["case_id"],
        provider=StubReconcileProvider(
            fetch_not_found(), create_outcome=ProviderOutcome.ACCEPTED
        ),
        fencing_token=token,
    )

    assert result.reposted is True
    assert result.case_state is not CaseState.ATTEMPT_FAILED


def test_repost_uses_the_same_key(conn: psycopg.Connection) -> None:
    """Never generate a new financial idempotency key during reconciliation."""
    ids = seed_dispatchable(conn)
    prepared, token = _to_ambiguous_lost_response(conn, ids, sent=False)
    provider = StubReconcileProvider(fetch_not_found())

    reconcile_case(conn, ids["case_id"], provider=provider, fencing_token=token)

    assert len(provider.create_calls) == 1
    assert provider.create_calls[0]["reference_id"] == prepared.idempotency_key
    assert attempts_for(conn, ids["case_id"])[0]["idempotency_key"] == (
        prepared.idempotency_key
    )


def test_repost_creates_no_new_action_attempt_or_budget(
    conn: psycopg.Connection,
) -> None:
    """I4/I5: a re-POST is a retry of the same mechanism, not a new one."""
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids, sent=False)
    before = case_row(conn, ids["case_id"])["attempt_count"]

    reconcile_case(
        conn, ids["case_id"], provider=StubReconcileProvider(fetch_not_found()),
        fencing_token=token,
    )

    assert len(actions_for(conn, ids["case_id"])) == 1
    assert len(attempts_for(conn, ids["case_id"])) == 1
    assert case_row(conn, ids["case_id"])["attempt_count"] == before


def test_repost_accepted_reaches_awaiting_customer(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids, sent=False)

    result = reconcile_case(
        conn,
        ids["case_id"],
        provider=StubReconcileProvider(
            fetch_not_found(), create_outcome=ProviderOutcome.ACCEPTED
        ),
        fencing_token=token,
    )

    assert result.case_state is CaseState.AWAITING_CUSTOMER
    assert case_row(conn, ids["case_id"])["state"] == "AWAITING_CUSTOMER"


def test_repost_duplicate_reference_adopts(conn: psycopg.Connection) -> None:
    """The misdiagnosis guard: NOT_FOUND was wrong, the link existed."""
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids, sent=False)

    result = reconcile_case(
        conn,
        ids["case_id"],
        provider=StubReconcileProvider(
            fetch_not_found(),
            create_outcome=ProviderOutcome.DUPLICATE_REFERENCE,
            create_correlation_id="plink_Existing9",
        ),
        fencing_token=token,
    )

    assert result.case_state is CaseState.AWAITING_CUSTOMER
    assert len(attempts_for(conn, ids["case_id"])) == 1


def test_repost_rejected_is_attempt_failed(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids, sent=False)

    result = reconcile_case(
        conn,
        ids["case_id"],
        provider=StubReconcileProvider(
            fetch_not_found(), create_outcome=ProviderOutcome.REJECTED
        ),
        fencing_token=token,
    )

    assert result.case_state is CaseState.ATTEMPT_FAILED


def test_repost_self_limits_to_one_per_attempt(conn: psycopg.Connection) -> None:
    """Stronger than the 3-POST cap: the state machine allows only one.

    After a re-POST returns an unknown outcome the attempt becomes UNKNOWN,
    which makes the next NOT_FOUND authoritative -- so the second round
    confirms failure instead of POSTing again. The cap is a backstop, not the
    mechanism.
    """
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids, sent=False)
    reposts = 0

    for _ in range(4):
        if case_row(conn, ids["case_id"])["state"] not in ("AMBIGUOUS", "RECONCILING"):
            break
        result = reconcile_case(
            conn,
            ids["case_id"],
            provider=StubReconcileProvider(
                fetch_not_found(), create_outcome=ProviderOutcome.TIMEOUT
            ),
            fencing_token=token,
            max_polls=99,
        )
        reposts += int(result.reposted)
        token = case_row(conn, ids["case_id"])["fencing_token"]

    assert reposts == 1, "only one same-key re-POST is reachable"
    assert case_row(conn, ids["case_id"])["state"] == "ATTEMPT_FAILED"
    assert len(attempts_for(conn, ids["case_id"])) == 1


def test_total_financial_posts_stay_within_the_cap(conn: psycopg.Connection) -> None:
    """At most 3 create_payment_link requests per attempt, ever."""
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids, sent=False)

    for _ in range(4):
        if case_row(conn, ids["case_id"])["state"] not in ("AMBIGUOUS", "RECONCILING"):
            break
        reconcile_case(
            conn,
            ids["case_id"],
            provider=StubReconcileProvider(
                fetch_not_found(), create_outcome=ProviderOutcome.TIMEOUT
            ),
            fencing_token=token,
            max_polls=99,
        )
        token = case_row(conn, ids["case_id"])["fencing_token"]

    attempt_id = attempts_for(conn, ids["case_id"])[0]["id"]
    assert post_count(conn, attempt_id) <= 3


# ---- bounded polling -> EXPIRED_UNRESOLVED --------------------------------


def test_reconciliation_timeout_expires_unresolved(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids)

    for _ in range(4):
        reconcile_case(
            conn,
            ids["case_id"],
            provider=StubReconcileProvider(fetch_no_evidence()),
            fencing_token=token,
            max_polls=3,
        )
        token = case_row(conn, ids["case_id"])["fencing_token"]

    assert case_row(conn, ids["case_id"])["state"] == "EXPIRED_UNRESOLVED"


def test_expired_unresolved_is_not_verified_failed(conn: psycopg.Connection) -> None:
    """An exhausted poll budget forbids VERIFIED_FAILED and lift inclusion."""
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids)

    for _ in range(3):
        reconcile_case(
            conn, ids["case_id"], provider=StubReconcileProvider(fetch_no_evidence()),
            fencing_token=token, max_polls=2,
        )
        token = case_row(conn, ids["case_id"])["fencing_token"]

    state = case_row(conn, ids["case_id"])["state"]
    assert state == "EXPIRED_UNRESOLVED"
    assert state != "VERIFIED_FAILED"
    assert case_row(conn, ids["case_id"])["attempt_count"] == 1


def test_poll_count_counts_only_fetches(conn: psycopg.Connection) -> None:
    """A re-POST must never consume the poll budget."""
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids, sent=False)

    reconcile_case(
        conn, ids["case_id"], provider=StubReconcileProvider(fetch_not_found()),
        fencing_token=token,
    )

    assert poll_count(conn, ids["case_id"]) == 1


def test_ttl_exhaustion_also_expires(conn: psycopg.Connection) -> None:
    """A case can also expire via TTL exhaustion, already delivered by expire_ttl."""
    from reclaim.domain.sweeper import expire_ttl

    ids = seed_dispatchable(conn)
    _to_ambiguous_lost_response(conn, ids)
    conn.execute(
        "UPDATE recovery_cases SET ttl_budget_ms = 0, active_elapsed_ms = 0 WHERE id = %s",
        (ids["case_id"],),
    )

    expire_ttl(conn)

    assert case_row(conn, ids["case_id"])["state"] == "EXPIRED_UNRESOLVED"


# ---- I4: no new financial mechanism while unresolved ---------------------


def test_no_edge_from_ambiguous_or_reconciling_to_dispatch() -> None:
    """The strongest form of I4: the edge does not exist to be taken."""
    for src in (CaseState.AMBIGUOUS, CaseState.RECONCILING):
        assert not is_allowed(src, CaseState.EXECUTING)
        assert not is_allowed(src, CaseState.ACTION_READY)


def test_second_action_blocked_during_reconciliation(
    conn: psycopg.Connection,
) -> None:
    ids = seed_dispatchable(conn)
    _to_ambiguous_lost_response(conn, ids)

    with pytest.raises(UniqueViolation):
        insert_action(conn, ids["case_id"], ids["policy_id"], sequence_no=2,
                      status="PROPOSED")


def test_second_attempt_blocked_during_reconciliation(
    conn: psycopg.Connection,
) -> None:
    ids = seed_dispatchable(conn)
    _to_ambiguous_lost_response(conn, ids)
    action_id = actions_for(conn, ids["case_id"])[0]["id"]

    with pytest.raises(UniqueViolation):
        insert_attempt(conn, action_id, ids["case_id"], attempt_no=2,
                       idempotency_key="rcv_second")


def test_reconciliation_records_its_query(conn: psycopg.Connection) -> None:
    """A provider_requests row for the query itself, not only the outcome."""
    ids = seed_dispatchable(conn)
    _, token = _to_ambiguous_lost_response(conn, ids)

    reconcile_case(
        conn, ids["case_id"], provider=StubReconcileProvider(fetch_found()),
        fencing_token=token,
    )

    fetches = [r for r in requests_for(conn, ids["case_id"])]
    outcomes = {r["outcome"] for r in fetches}
    assert "FOUND" in outcomes
    assert all(r["completed_at"] is not None for r in fetches)


def test_open_attempt_lookup_requires_an_open_attempt(
    conn: psycopg.Connection,
) -> None:
    from reclaim.domain.reconciliation import ReconciliationBlocked

    ids = seed_dispatchable(conn)

    with pytest.raises(ReconciliationBlocked):
        open_attempt_for(conn, ids["case_id"])
