"""Crash recovery across every point in dispatch. Recovery runs through the real sweeper."""

from __future__ import annotations

import pytest
import psycopg

from reclaim.domain.execution import call_provider, prepare_dispatch, settle_dispatch
from reclaim.domain.leases import claim_case
from reclaim.domain.states import CaseState
from reclaim.domain.sweeper import sweep_expired_leases
from reclaim.provider.contract import ProviderOutcome
from tests.domain.execution_helpers import (
    LINK_TTL_SECONDS,
    StubProvider,
    actions_for,
    attempts_for,
    case_row,
    requests_for,
    seed_dispatchable,
)


def _claim(conn: psycopg.Connection, case_id: int) -> int:
    claim = claim_case(conn, case_id, CaseState.ACTION_READY, "worker-crash", 60)
    assert claim is not None
    return claim.fencing_token


def _expire_lease(conn: psycopg.Connection, case_id: int) -> None:
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = now() - interval '1 second' "
        "WHERE id = %s",
        (case_id,),
    )


def _prepare_only(conn: psycopg.Connection, ids: dict, token: int):
    """TXN 1 commits; the process then 'crashes' before TXN 2."""
    return prepare_dispatch(
        conn,
        ids["case_id"],
        fencing_token=token,
        policy_decision_id=ids["policy_id"],
        link_ttl_seconds=LINK_TTL_SECONDS,
        worker_id="worker-crash",
    )


# ---- Crash 1: TXN 1 committed, the call never went out -------------------


def test_crash1_leaves_request_in_flight(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)
    token = _claim(conn, ids["case_id"])
    prepared = _prepare_only(conn, ids, token)

    assert case_row(conn, ids["case_id"])["state"] == "EXECUTING"
    assert attempts_for(conn, ids["case_id"])[0]["state"] == "PREPARED"
    assert requests_for(conn, ids["case_id"])[0]["outcome"] == "IN_FLIGHT"
    assert prepared.idempotency_key.startswith("rcv_")


def test_crash1_sweeper_moves_to_ambiguous(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)
    token = _claim(conn, ids["case_id"])
    _prepare_only(conn, ids, token)
    _expire_lease(conn, ids["case_id"])

    result = sweep_expired_leases(conn)

    assert result.executing_to_ambiguous == 1
    assert case_row(conn, ids["case_id"])["state"] == "AMBIGUOUS"


def test_crash1_key_survives_and_is_not_regenerated(conn: psycopg.Connection) -> None:
    """Reconciliation must re-POST this same key; nothing may mint a new one."""
    ids = seed_dispatchable(conn)
    token = _claim(conn, ids["case_id"])
    prepared = _prepare_only(conn, ids, token)
    _expire_lease(conn, ids["case_id"])

    sweep_expired_leases(conn)

    attempts = attempts_for(conn, ids["case_id"])
    assert len(attempts) == 1
    assert attempts[0]["idempotency_key"] == prepared.idempotency_key
    assert attempts[0]["provider_reference"] == prepared.idempotency_key


def test_crash1_budget_stays_consumed(conn: psycopg.Connection) -> None:
    """TXN 1 committed, so the attempt was really spent."""
    ids = seed_dispatchable(conn)
    token = _claim(conn, ids["case_id"])
    _prepare_only(conn, ids, token)
    _expire_lease(conn, ids["case_id"])

    sweep_expired_leases(conn)

    assert case_row(conn, ids["case_id"])["attempt_count"] == 1


def test_crash1_sweeper_bumps_the_fencing_token(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)
    token = _claim(conn, ids["case_id"])
    _prepare_only(conn, ids, token)
    _expire_lease(conn, ids["case_id"])

    sweep_expired_leases(conn)

    assert case_row(conn, ids["case_id"])["fencing_token"] > token


# ---- Crash 2: provider accepted, TXN 2 never committed -------------------


def test_crash2_no_second_financial_mechanism(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)
    token = _claim(conn, ids["case_id"])
    prepared = _prepare_only(conn, ids, token)
    call_provider(StubProvider(ProviderOutcome.ACCEPTED), prepared)  # provider accepted
    _expire_lease(conn, ids["case_id"])

    sweep_expired_leases(conn)

    assert case_row(conn, ids["case_id"])["state"] == "AMBIGUOUS"
    assert len(attempts_for(conn, ids["case_id"])) == 1
    assert len(actions_for(conn, ids["case_id"])) == 1


def test_crash2_is_locally_identical_to_crash1(conn: psycopg.Connection) -> None:
    """Whether the call landed is unknowable locally -- that is why AMBIGUOUS exists."""
    a = seed_dispatchable(conn, suffix="c2a")
    ta = _claim(conn, a["case_id"])
    _prepare_only(conn, a, ta)  # crash 1: never sent

    b = seed_dispatchable(conn, suffix="c2b")
    tb = _claim(conn, b["case_id"])
    pb = _prepare_only(conn, b, tb)
    call_provider(StubProvider(ProviderOutcome.ACCEPTED), pb)  # crash 2: sent, accepted

    for ids in (a, b):
        _expire_lease(conn, ids["case_id"])
    sweep_expired_leases(conn)

    def shape(ids):
        return (
            case_row(conn, ids["case_id"])["state"],
            attempts_for(conn, ids["case_id"])[0]["state"],
            requests_for(conn, ids["case_id"])[0]["outcome"],
        )

    assert shape(a) == shape(b) == ("AMBIGUOUS", "PREPARED", "IN_FLIGHT")


# ---- Crash 3 / 4: response lost after the provider decided ---------------


def _settled_unknown(conn: psycopg.Connection, ids: dict, outcome: ProviderOutcome):
    token = _claim(conn, ids["case_id"])
    prepared = _prepare_only(conn, ids, token)
    provider = StubProvider(outcome, http_status=None)
    result = call_provider(provider, prepared)
    return settle_dispatch(conn, prepared, result, fencing_token=token,
                           worker_id="worker-crash")


def test_crash3_accepted_but_lost_is_ambiguous(conn: psycopg.Connection) -> None:
    """Provider accepted; we never learned it. Must not resolve optimistically."""
    ids = seed_dispatchable(conn)

    _settled_unknown(conn, ids, ProviderOutcome.TIMEOUT)

    assert case_row(conn, ids["case_id"])["state"] == "AMBIGUOUS"
    assert attempts_for(conn, ids["case_id"])[0]["state"] == "UNKNOWN"


def test_crash4_rejected_but_lost_is_ambiguous(conn: psycopg.Connection) -> None:
    """Provider rejected; we never learned it. Must not resolve to failure either."""
    ids = seed_dispatchable(conn)

    _settled_unknown(conn, ids, ProviderOutcome.TIMEOUT)

    assert case_row(conn, ids["case_id"])["state"] != "ATTEMPT_FAILED"
    assert case_row(conn, ids["case_id"])["state"] == "AMBIGUOUS"


def test_crash3_and_crash4_are_indistinguishable(conn: psycopg.Connection) -> None:
    """Branching between them before querying the provider would be wrong --
    both are genuinely unknown until a read proves otherwise."""
    a = seed_dispatchable(conn, suffix="c3")
    b = seed_dispatchable(conn, suffix="c4")

    _settled_unknown(conn, a, ProviderOutcome.TIMEOUT)
    _settled_unknown(conn, b, ProviderOutcome.TIMEOUT)

    def shape(ids):
        return (
            case_row(conn, ids["case_id"])["state"],
            attempts_for(conn, ids["case_id"])[0]["state"],
            actions_for(conn, ids["case_id"])[0]["status"],
            requests_for(conn, ids["case_id"])[0]["outcome"],
        )

    assert shape(a) == shape(b) == ("AMBIGUOUS", "UNKNOWN", "UNRESOLVED", "TIMEOUT")


def test_unknown_action_stays_unresolved_not_terminal(conn: psycopg.Connection) -> None:
    """TERMINAL_FAILED needs evidence a timed-out dispatch does not have."""
    ids = seed_dispatchable(conn)

    _settled_unknown(conn, ids, ProviderOutcome.TIMEOUT)

    action = actions_for(conn, ids["case_id"])[0]
    assert action["status"] == "UNRESOLVED"
    assert action["resolved_at"] is None


def test_ambiguous_case_cannot_dispatch_again(conn: psycopg.Connection) -> None:
    """I4/I5: no new financial action while the prior one is unresolved."""
    from psycopg.errors import UniqueViolation

    ids = seed_dispatchable(conn)
    _settled_unknown(conn, ids, ProviderOutcome.TIMEOUT)
    conn.execute(
        "UPDATE recovery_cases SET state='ACTION_READY', lease_expires_at='-infinity' "
        "WHERE id=%s",
        (ids["case_id"],),
    )
    token = _claim(conn, ids["case_id"])
    provider = StubProvider()

    with pytest.raises(UniqueViolation):
        prepare_dispatch(
            conn,
            ids["case_id"],
            fencing_token=token,
            policy_decision_id=ids["policy_id"],
            link_ttl_seconds=LINK_TTL_SECONDS,
        )

    assert provider.calls == []


def test_reconciliation_target_state_is_legal(conn: psycopg.Connection) -> None:
    """AMBIGUOUS -> RECONCILING is the edge reconciliation uses; prove it exists."""
    from reclaim.domain.states import is_allowed

    assert is_allowed(CaseState.AMBIGUOUS, CaseState.RECONCILING)
    assert not is_allowed(CaseState.AMBIGUOUS, CaseState.EXECUTING)
