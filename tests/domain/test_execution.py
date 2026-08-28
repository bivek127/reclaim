"""Execution dispatch: crash safety, idempotency, and budget/action boundaries (I2, I5)."""

from __future__ import annotations

import pytest
import psycopg
from psycopg.errors import UniqueViolation

from reclaim.domain.execution import (
    BudgetExhausted,
    DispatchAborted,
    action_status_for,
    attempt_state_for,
    call_provider,
    case_target_for,
    dispatch,
    new_idempotency_key,
    prepare_dispatch,
    request_outcome_for,
    settle_dispatch,
)
from reclaim.domain.states import CaseState
from reclaim.provider.contract import UNKNOWN_OUTCOMES, ProviderOutcome
from tests.db.helpers import insert_action, insert_attempt
from tests.domain.execution_helpers import (
    LINK_TTL_SECONDS,
    StubProvider,
    actions_for,
    attempts_for,
    breaker_row,
    case_row,
    requests_for,
    seed_dispatchable,
)


def _dispatch(conn, ids, provider, *, token=0, worker="w1"):
    return dispatch(
        conn,
        ids["case_id"],
        provider=provider,
        fencing_token=token,
        policy_decision_id=ids["policy_id"],
        link_ttl_seconds=LINK_TTL_SECONDS,
        worker_id=worker,
    )


# ---- happy path ----------------------------------------------------------


def test_accepted_reaches_awaiting_customer(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)

    result = _dispatch(conn, ids, StubProvider(ProviderOutcome.ACCEPTED))

    assert result.applied is True
    assert case_row(conn, ids["case_id"])["state"] == "AWAITING_CUSTOMER"
    assert attempts_for(conn, ids["case_id"])[0]["state"] == "ACCEPTED"
    assert actions_for(conn, ids["case_id"])[0]["status"] == "LIVE"
    assert requests_for(conn, ids["case_id"])[0]["outcome"] == "ACCEPTED"


def test_accepted_stores_provider_correlation_id(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)

    _dispatch(conn, ids, StubProvider(correlation_id="plink_Abc123"))

    assert requests_for(conn, ids["case_id"])[0]["provider_correlation_id"] == "plink_Abc123"


def test_amount_is_copied_from_the_obligation(conn: psycopg.Connection) -> None:
    """trg_attempt_amount also enforces this; no caller may supply an amount."""
    ids = seed_dispatchable(conn, amount_minor=42_500)
    provider = StubProvider()

    _dispatch(conn, ids, provider)

    assert attempts_for(conn, ids["case_id"])[0]["amount_minor"] == 42_500
    assert provider.calls[0]["amount_minor"] == 42_500


def test_action_deadline_is_after_provider_expiry(conn: psycopg.Connection) -> None:
    """ck_deadline_after_provider: our deadline must never precede the provider's."""
    ids = seed_dispatchable(conn)

    _dispatch(conn, ids, StubProvider())

    action = actions_for(conn, ids["case_id"])[0]
    assert action["action_deadline_at"] > action["provider_expires_at"]


# ---- a provider timeout becomes AMBIGUOUS --------------------------------


def test_provider_timeout_becomes_ambiguous(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)

    result = _dispatch(conn, ids, StubProvider(ProviderOutcome.TIMEOUT, http_status=None))

    assert result.case_state is CaseState.AMBIGUOUS
    assert case_row(conn, ids["case_id"])["state"] == "AMBIGUOUS"


def test_timeout_leaves_attempt_unknown_not_rejected(conn: psycopg.Connection) -> None:
    """I3: an unknown outcome must never be inferred to failure."""
    ids = seed_dispatchable(conn)

    _dispatch(conn, ids, StubProvider(ProviderOutcome.TIMEOUT))

    assert attempts_for(conn, ids["case_id"])[0]["state"] == "UNKNOWN"
    assert actions_for(conn, ids["case_id"])[0]["status"] == "UNRESOLVED"
    assert actions_for(conn, ids["case_id"])[0]["resolved_at"] is None


@pytest.mark.parametrize("outcome", sorted(UNKNOWN_OUTCOMES, key=lambda o: o.value))
def test_every_unknown_outcome_routes_to_ambiguous(
    conn: psycopg.Connection, outcome
) -> None:
    ids = seed_dispatchable(conn, suffix=f"u{outcome.value}")

    _dispatch(conn, ids, StubProvider(outcome))

    assert case_row(conn, ids["case_id"])["state"] == "AMBIGUOUS"
    assert attempts_for(conn, ids["case_id"])[0]["state"] == "UNKNOWN"


def test_auth_error_is_ambiguous_not_attempt_failed(conn: psycopg.Connection) -> None:
    """A misconfigured key is not a business rejection."""
    ids = seed_dispatchable(conn)

    _dispatch(conn, ids, StubProvider(ProviderOutcome.AUTH_ERROR))

    assert case_row(conn, ids["case_id"])["state"] == "AMBIGUOUS"


# ---- a database failure before the provider call -------------------------


def test_db_failure_before_provider_call_leaves_no_attempt(
    conn: psycopg.Connection,
) -> None:
    """Wrong expected state aborts TXN 1; nothing is created, nothing is sent."""
    ids = seed_dispatchable(conn, state="POLICY_EVAL")
    provider = StubProvider()

    with pytest.raises(DispatchAborted):
        _dispatch(conn, ids, provider)

    assert case_row(conn, ids["case_id"])["state"] == "POLICY_EVAL"
    assert attempts_for(conn, ids["case_id"]) == []
    assert actions_for(conn, ids["case_id"]) == []
    assert provider.calls == []


def test_aborted_txn1_does_not_consume_budget(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn, state="POLICY_EVAL")

    with pytest.raises(DispatchAborted):
        _dispatch(conn, ids, StubProvider())

    assert case_row(conn, ids["case_id"])["attempt_count"] == 0


def test_exhausted_budget_never_calls_the_provider(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn, max_attempts=2, attempt_count=2)
    provider = StubProvider()

    with pytest.raises(BudgetExhausted):
        _dispatch(conn, ids, provider)

    assert provider.calls == []
    assert attempts_for(conn, ids["case_id"]) == []


def test_stale_token_never_calls_the_provider(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)
    provider = StubProvider()

    with pytest.raises(BudgetExhausted):
        _dispatch(conn, ids, provider, token=99)

    assert provider.calls == []
    assert case_row(conn, ids["case_id"])["attempt_count"] == 0


# ---- DB failure after provider success recovers via reconciliation -------


def test_db_failure_after_provider_success_recovers_by_reference(
    conn: psycopg.Connection,
) -> None:
    """Durable boundary only. Reaching AWAITING_CUSTOMER is reconciliation's job."""
    ids = seed_dispatchable(conn)
    prepared = prepare_dispatch(
        conn,
        ids["case_id"],
        fencing_token=0,
        policy_decision_id=ids["policy_id"],
        link_ttl_seconds=LINK_TTL_SECONDS,
    )
    call_provider(StubProvider(ProviderOutcome.ACCEPTED), prepared)
    # TXN 2 never runs -- the crash.

    assert case_row(conn, ids["case_id"])["state"] == "EXECUTING"
    assert requests_for(conn, ids["case_id"])[0]["outcome"] == "IN_FLIGHT"
    assert requests_for(conn, ids["case_id"])[0]["completed_at"] is None
    attempts = attempts_for(conn, ids["case_id"])
    assert len(attempts) == 1
    assert attempts[0]["state"] == "PREPARED"
    assert attempts[0]["idempotency_key"] == prepared.idempotency_key


# ---- duplicate reference adopts -------------------------------------------


def test_duplicate_reference_adopts_existing(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)

    result = _dispatch(
        conn,
        ids,
        StubProvider(
            ProviderOutcome.DUPLICATE_REFERENCE,
            correlation_id="plink_Existing1",
            http_status=400,
        ),
    )

    assert result.case_state is CaseState.AWAITING_CUSTOMER
    assert case_row(conn, ids["case_id"])["state"] == "AWAITING_CUSTOMER"
    assert result.provider_correlation_id == "plink_Existing1"


def test_duplicate_reference_is_not_attempt_failed(conn: psycopg.Connection) -> None:
    """A duplicate is proof our own earlier request landed, not a failure."""
    ids = seed_dispatchable(conn)

    _dispatch(conn, ids, StubProvider(ProviderOutcome.DUPLICATE_REFERENCE, http_status=400))

    assert attempts_for(conn, ids["case_id"])[0]["state"] == "ACCEPTED"
    assert actions_for(conn, ids["case_id"])[0]["status"] == "LIVE"
    assert requests_for(conn, ids["case_id"])[0]["outcome"] == "DUPLICATE_REFERENCE"


def test_duplicate_creates_no_second_mechanism(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)

    _dispatch(conn, ids, StubProvider(ProviderOutcome.DUPLICATE_REFERENCE, http_status=400))

    assert len(attempts_for(conn, ids["case_id"])) == 1
    assert len(actions_for(conn, ids["case_id"])) == 1
    assert case_row(conn, ids["case_id"])["attempt_count"] == 1


# ---- rejection paths -----------------------------------------------------


def test_rejected_becomes_attempt_failed(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)

    _dispatch(conn, ids, StubProvider(ProviderOutcome.REJECTED, http_status=400))

    assert case_row(conn, ids["case_id"])["state"] == "ATTEMPT_FAILED"
    assert actions_for(conn, ids["case_id"])[0]["status"] == "TERMINAL_FAILED"
    assert actions_for(conn, ids["case_id"])[0]["resolved_at"] is not None


def test_transport_error_becomes_attempt_failed(conn: psycopg.Connection) -> None:
    """Zero bytes written proves nothing was created, so it resolves as a
    failure rather than ambiguity."""
    ids = seed_dispatchable(conn)

    _dispatch(conn, ids, StubProvider(ProviderOutcome.TRANSPORT_ERROR, http_status=None))

    assert case_row(conn, ids["case_id"])["state"] == "ATTEMPT_FAILED"
    assert requests_for(conn, ids["case_id"])[0]["outcome"] == "TRANSPORT_ERROR"


# ---- I2: idempotency -----------------------------------------------------


def test_key_persisted_before_dispatch(conn: psycopg.Connection) -> None:
    """The provider is only reachable after the key is committed."""
    ids = seed_dispatchable(conn)
    seen: dict[str, object] = {}

    def _assert_committed(reference_id: str) -> None:
        # A separate connection can only see committed rows.
        with psycopg.connect(conn.info.dsn, autocommit=True) as other:
            row = other.execute(
                "SELECT state FROM execution_attempts WHERE idempotency_key = %s",
                (reference_id,),
            ).fetchone()
            seen["row"] = row

    _dispatch(conn, ids, StubProvider(on_call=_assert_committed))

    assert seen["row"] is not None, "key was not committed before the network call"
    assert seen["row"][0] == "PREPARED"


def test_provider_reference_equals_idempotency_key(conn: psycopg.Connection) -> None:
    """The idempotency key and the provider reference are one string, not two."""
    ids = seed_dispatchable(conn)
    provider = StubProvider()

    _dispatch(conn, ids, provider)

    attempt = attempts_for(conn, ids["case_id"])[0]
    assert attempt["provider_reference"] == attempt["idempotency_key"]
    assert provider.calls[0]["reference_id"] == attempt["idempotency_key"]


def test_key_shape_matches_spec(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)

    _dispatch(conn, ids, StubProvider())

    key = attempts_for(conn, ids["case_id"])[0]["idempotency_key"]
    assert key.startswith("rcv_")
    assert len(key) == 30
    assert len(key) <= 40  # the provider's documented reference_id cap


def test_request_row_carries_the_same_key(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)

    _dispatch(conn, ids, StubProvider())

    assert (
        requests_for(conn, ids["case_id"])[0]["idempotency_key"]
        == attempts_for(conn, ids["case_id"])[0]["idempotency_key"]
    )


def test_keys_are_unique_across_dispatches(conn: psycopg.Connection) -> None:
    keys = {new_idempotency_key() for _ in range(500)}
    assert len(keys) == 500


# ---- I5: one unresolved financial mechanism ------------------------------


def test_prior_action_success_after_second_proposed(conn: psycopg.Connection) -> None:
    """The database refuses the second open action, before any network call."""
    ids = seed_dispatchable(conn)
    _dispatch(conn, ids, StubProvider(ProviderOutcome.TIMEOUT))  # -> UNRESOLVED
    conn.execute(
        "UPDATE recovery_cases SET state='ACTION_READY' WHERE id=%s", (ids["case_id"],)
    )
    provider = StubProvider()

    with pytest.raises(UniqueViolation):
        _dispatch(conn, ids, provider)

    assert provider.calls == []


def test_second_open_attempt_on_one_action_rejected(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)
    prepared = prepare_dispatch(
        conn,
        ids["case_id"],
        fencing_token=0,
        policy_decision_id=ids["policy_id"],
        link_ttl_seconds=LINK_TTL_SECONDS,
    )

    with pytest.raises(UniqueViolation):
        insert_attempt(
            conn,
            prepared.action_id,
            ids["case_id"],
            attempt_no=2,
            idempotency_key="rcv_other",
        )


def test_unknown_attempt_still_blocks_a_new_attempt(conn: psycopg.Connection) -> None:
    """attempt_state UNKNOWN is inside uq_action_one_open_attempt."""
    ids = seed_dispatchable(conn)
    _dispatch(conn, ids, StubProvider(ProviderOutcome.TIMEOUT))
    action_id = actions_for(conn, ids["case_id"])[0]["id"]

    with pytest.raises(UniqueViolation):
        insert_attempt(
            conn, action_id, ids["case_id"], attempt_no=2, idempotency_key="rcv_other2"
        )


def test_duplicate_idempotency_key_rejected(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)
    _dispatch(conn, ids, StubProvider(ProviderOutcome.REJECTED, http_status=400))
    key = attempts_for(conn, ids["case_id"])[0]["idempotency_key"]
    other = seed_dispatchable(conn, suffix="2")
    action_id = insert_action(conn, other["case_id"], other["policy_id"], status="LIVE")

    with pytest.raises(UniqueViolation):
        insert_attempt(conn, action_id, other["case_id"], idempotency_key=key)


# ---- promote-or-create ---------------------------------------------------


def test_existing_proposed_action_is_promoted_not_duplicated(
    conn: psycopg.Connection,
) -> None:
    """The human-review path: a PROPOSED action already exists."""
    ids = seed_dispatchable(conn)
    action_id = insert_action(conn, ids["case_id"], ids["policy_id"], status="PROPOSED")

    _dispatch(conn, ids, StubProvider())

    actions = actions_for(conn, ids["case_id"])
    assert len(actions) == 1, "a second action row would violate I5"
    assert actions[0]["id"] == action_id
    assert actions[0]["status"] == "LIVE"


def test_no_existing_action_creates_one(conn: psycopg.Connection) -> None:
    """The automated dispatch path, with no PROPOSED action ahead of it."""
    ids = seed_dispatchable(conn)

    _dispatch(conn, ids, StubProvider())

    actions = actions_for(conn, ids["case_id"])
    assert len(actions) == 1
    assert actions[0]["status"] == "LIVE"
    assert actions[0]["sequence_no"] == 1


# ---- outcome mapping is total over provider outcomes ---------------------


def test_mapping_is_total_over_provider_outcomes() -> None:
    for outcome in ProviderOutcome:
        assert request_outcome_for(outcome)
        assert attempt_state_for(outcome)
        assert action_status_for(outcome)
        assert case_target_for(outcome)


@pytest.mark.parametrize("outcome", list(ProviderOutcome))
def test_every_outcome_records_its_own_enum_value(
    conn: psycopg.Connection, outcome
) -> None:
    """Migration 019: no outcome is collapsed onto another any more.

    Replaces the earlier test that pinned the temporary collapse onto
    TIMEOUT. This is the stronger property: a 401 is recorded as AUTH_ERROR,
    not disguised as a timeout.
    """
    ids = seed_dispatchable(conn, suffix=f"c{outcome.value}")

    _dispatch(conn, ids, StubProvider(outcome))

    request = requests_for(conn, ids["case_id"])[0]
    assert request["outcome"] == outcome.value
    assert request["response_body"]["provider_outcome"] == outcome.value


def test_outcome_enum_values_are_distinct() -> None:
    """No two ProviderOutcomes share a request_outcome member."""
    mapped = [request_outcome_for(o) for o in ProviderOutcome]

    assert len(set(mapped)) == len(mapped)


def test_audit_detail_carries_exact_outcome(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)

    _dispatch(conn, ids, StubProvider(ProviderOutcome.RATE_LIMITED, http_status=429))

    row = conn.execute(
        """
        SELECT detail FROM audit_events
         WHERE case_id = %s AND event_type = 'provider_response_received'
        """,
        (ids["case_id"],),
    ).fetchone()
    assert row is not None
    assert row[0]["provider_outcome"] == "RATE_LIMITED"


def test_completed_at_set_for_every_settled_outcome(conn: psycopg.Connection) -> None:
    """ck_completed_shape is a biconditional."""
    for i, outcome in enumerate(ProviderOutcome):
        ids = seed_dispatchable(conn, suffix=f"s{i}")
        _dispatch(conn, ids, StubProvider(outcome))
        assert requests_for(conn, ids["case_id"])[0]["completed_at"] is not None


# ---- breaker interaction -------------------------------------------------


def test_accepted_dispatch_resets_failure_counter(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn, suffix="b1")
    _dispatch(conn, ids, StubProvider(ProviderOutcome.TIMEOUT))
    assert breaker_row(conn)["consecutive_failures"] == 1

    ok = seed_dispatchable(conn, suffix="b2")
    _dispatch(conn, ok, StubProvider(ProviderOutcome.ACCEPTED))

    assert breaker_row(conn)["consecutive_failures"] == 0


def test_dispatch_never_opens_the_breaker(conn: psycopg.Connection) -> None:
    """Even past the threshold, breaker state belongs to the monitor job."""
    for i in range(7):
        ids = seed_dispatchable(conn, suffix=f"f{i}")
        _dispatch(conn, ids, StubProvider(ProviderOutcome.TIMEOUT))

    row = breaker_row(conn)
    assert row["consecutive_failures"] == 7
    assert row["state"] == "CLOSED"


# ---- provider boundary ---------------------------------------------------


def test_execution_never_calls_fetch_by_reference(conn: psycopg.Connection) -> None:
    """Reconciliation owns the read; the stub raises if dispatch tries it."""
    ids = seed_dispatchable(conn)

    _dispatch(conn, ids, StubProvider(ProviderOutcome.TIMEOUT))

    assert case_row(conn, ids["case_id"])["state"] == "AMBIGUOUS"


def test_provider_called_exactly_once_per_dispatch(conn: psycopg.Connection) -> None:
    """The executor never auto-retries a dispatch."""
    ids = seed_dispatchable(conn)
    provider = StubProvider(ProviderOutcome.TIMEOUT)

    _dispatch(conn, ids, provider)

    assert len(provider.calls) == 1
    assert len(requests_for(conn, ids["case_id"])) == 1


# ---- executor boundary: RETRY_CHARGE must raise --------------------------


def _propose_action(conn, ids, action_type: str) -> int:
    """A PROPOSED action of an arbitrary type, as human review would leave."""
    return insert_action(
        conn, ids["case_id"], ids["policy_id"], status="PROPOSED",
        action_type=action_type,
    )


def test_retry_charge_proposed_action_raises(conn: psycopg.Connection) -> None:
    """The executor must raise if handed a RETRY_CHARGE action."""
    from reclaim.provider.contract import RetryChargeUnsupported

    ids = seed_dispatchable(conn)
    _propose_action(conn, ids, "RETRY_CHARGE")
    provider = StubProvider()

    with pytest.raises(RetryChargeUnsupported):
        _dispatch(conn, ids, provider)

    assert provider.calls == [], "no provider call may happen"


def test_retry_charge_is_rejected_before_budget_claim(
    conn: psycopg.Connection,
) -> None:
    from reclaim.provider.contract import RetryChargeUnsupported

    ids = seed_dispatchable(conn)
    _propose_action(conn, ids, "RETRY_CHARGE")
    before = case_row(conn, ids["case_id"])["attempt_count"]

    with pytest.raises(RetryChargeUnsupported):
        _dispatch(conn, ids, StubProvider())

    assert case_row(conn, ids["case_id"])["attempt_count"] == before


def test_retry_charge_is_not_promoted_to_live(conn: psycopg.Connection) -> None:
    from reclaim.provider.contract import RetryChargeUnsupported

    ids = seed_dispatchable(conn)
    action_id = _propose_action(conn, ids, "RETRY_CHARGE")

    with pytest.raises(RetryChargeUnsupported):
        _dispatch(conn, ids, StubProvider())

    actions = actions_for(conn, ids["case_id"])
    assert len(actions) == 1
    assert actions[0]["id"] == action_id
    assert actions[0]["status"] == "PROPOSED", "must not be promoted"


def test_retry_charge_creates_no_attempt_or_request(
    conn: psycopg.Connection,
) -> None:
    from reclaim.provider.contract import RetryChargeUnsupported

    ids = seed_dispatchable(conn)
    _propose_action(conn, ids, "RETRY_CHARGE")

    with pytest.raises(RetryChargeUnsupported):
        _dispatch(conn, ids, StubProvider())

    assert attempts_for(conn, ids["case_id"]) == []
    assert requests_for(conn, ids["case_id"]) == []


def test_retry_charge_leaves_case_state_unchanged(
    conn: psycopg.Connection,
) -> None:
    from reclaim.provider.contract import RetryChargeUnsupported

    ids = seed_dispatchable(conn)
    _propose_action(conn, ids, "RETRY_CHARGE")

    with pytest.raises(RetryChargeUnsupported):
        _dispatch(conn, ids, StubProvider())

    assert case_row(conn, ids["case_id"])["state"] == "ACTION_READY"


def test_retry_charge_action_type_is_never_rewritten(
    conn: psycopg.Connection,
) -> None:
    """Refuse it; do not silently turn it into a payment link."""
    from reclaim.provider.contract import RetryChargeUnsupported

    ids = seed_dispatchable(conn)
    _propose_action(conn, ids, "RETRY_CHARGE")

    with pytest.raises(RetryChargeUnsupported):
        _dispatch(conn, ids, StubProvider())

    assert actions_for(conn, ids["case_id"])[0]["status"] == "PROPOSED"
    row = conn.execute(
        "SELECT action_type FROM recovery_actions WHERE case_id = %s",
        (ids["case_id"],),
    ).fetchone()
    assert row is not None and row[0] == "RETRY_CHARGE"


def test_escalate_proposed_action_is_also_refused(
    conn: psycopg.Connection,
) -> None:
    """ESCALATE is a routing verdict, not a dispatchable financial action."""
    from reclaim.domain.execution import ActionTypeUnsupported

    ids = seed_dispatchable(conn)
    _propose_action(conn, ids, "ESCALATE")
    provider = StubProvider()

    with pytest.raises(ActionTypeUnsupported):
        _dispatch(conn, ids, provider)

    assert provider.calls == []
    assert attempts_for(conn, ids["case_id"]) == []


def test_create_payment_link_proposed_action_still_promotes(
    conn: psycopg.Connection,
) -> None:
    """The guard must not break the legitimate human-review path."""
    ids = seed_dispatchable(conn)
    action_id = _propose_action(conn, ids, "CREATE_PAYMENT_LINK")

    result = _dispatch(conn, ids, StubProvider())

    assert result.applied is True
    actions = actions_for(conn, ids["case_id"])
    assert len(actions) == 1
    assert actions[0]["id"] == action_id
    assert actions[0]["status"] == "LIVE"


def test_promote_update_cannot_select_a_forbidden_action(
    conn: psycopg.Connection,
) -> None:
    """Defence in depth: even bypassing the guard, promotion cannot occur.

    Calls _acquire_action directly, which is what prepare_dispatch's guard
    normally protects. The promote UPDATE's action_type predicate skips the
    RETRY_CHARGE row, the INSERT fallback runs, and uq_case_one_open_action
    turns it into a database error rather than a payment link.
    """
    from psycopg.errors import UniqueViolation

    from reclaim.domain.execution import _acquire_action

    ids = seed_dispatchable(conn)
    _propose_action(conn, ids, "RETRY_CHARGE")

    with pytest.raises(UniqueViolation):
        with conn.transaction():
            _acquire_action(
                conn,
                case_id=ids["case_id"],
                policy_decision_id=ids["policy_id"],
                expire_by=2_000_000_000,
            )
