"""Circuit-breaker gate and counting (ADR-011)."""

from __future__ import annotations

import pytest
import psycopg

from reclaim.domain.breaker import (
    FAILURE_OUTCOMES,
    SUCCESS_OUTCOMES,
    BreakerOpen,
    read_breaker,
    record_execution_outcome,
)
from reclaim.domain.execution import prepare_dispatch
from reclaim.provider.contract import ProviderOutcome
from tests.domain.execution_helpers import (
    LINK_TTL_SECONDS,
    attempts_for,
    breaker_row,
    case_row,
    seed_dispatchable,
)


def _open_breaker(conn: psycopg.Connection) -> None:
    conn.execute(
        "UPDATE circuit_breaker SET state='OPEN', opened_at=now(), "
        "reset_after=now() + interval '120 seconds' WHERE id=1"
    )


# ---- gate ------------------------------------------------------------


def test_open_breaker_halts_before_dispatch(conn: psycopg.Connection) -> None:
    """An OPEN breaker aborts to HALTED before dispatch, creating nothing."""
    ids = seed_dispatchable(conn)
    _open_breaker(conn)

    with pytest.raises(BreakerOpen):
        prepare_dispatch(
            conn,
            ids["case_id"],
            fencing_token=0,
            policy_decision_id=ids["policy_id"],
            link_ttl_seconds=LINK_TTL_SECONDS,
        )

    assert case_row(conn, ids["case_id"])["state"] == "HALTED"
    assert attempts_for(conn, ids["case_id"]) == []


def test_halted_case_spends_no_attempt_budget(conn: psycopg.Connection) -> None:
    """The budget claim rolls back with the rest of TXN 1."""
    ids = seed_dispatchable(conn)
    _open_breaker(conn)

    with pytest.raises(BreakerOpen):
        prepare_dispatch(
            conn,
            ids["case_id"],
            fencing_token=0,
            policy_decision_id=ids["policy_id"],
            link_ttl_seconds=LINK_TTL_SECONDS,
        )

    assert case_row(conn, ids["case_id"])["attempt_count"] == 0


def test_closed_breaker_permits_dispatch(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)

    prepare_dispatch(
        conn,
        ids["case_id"],
        fencing_token=0,
        policy_decision_id=ids["policy_id"],
        link_ttl_seconds=LINK_TTL_SECONDS,
    )

    assert case_row(conn, ids["case_id"])["state"] == "EXECUTING"


# ---- counting ------------------------------------------------------------


@pytest.mark.parametrize("outcome", sorted(FAILURE_OUTCOMES, key=lambda o: o.value))
def test_failure_outcomes_increment(conn: psycopg.Connection, outcome) -> None:
    assert record_execution_outcome(conn, outcome) == 1
    assert breaker_row(conn)["consecutive_failures"] == 1


@pytest.mark.parametrize("outcome", sorted(SUCCESS_OUTCOMES, key=lambda o: o.value))
def test_success_outcomes_reset(conn: psycopg.Connection, outcome) -> None:
    record_execution_outcome(conn, ProviderOutcome.TIMEOUT)
    record_execution_outcome(conn, ProviderOutcome.TIMEOUT)

    assert record_execution_outcome(conn, outcome) == 0


def test_every_provider_outcome_is_classified() -> None:
    """No outcome may fall between the two sets unnoticed."""
    for outcome in ProviderOutcome:
        assert outcome in FAILURE_OUTCOMES or outcome in SUCCESS_OUTCOMES


def test_failures_accumulate(conn: psycopg.Connection) -> None:
    for expected in (1, 2, 3):
        assert record_execution_outcome(conn, ProviderOutcome.PROVIDER_ERROR) == expected


# ---- the boundary that ADR-011 pins --------------------------------------


def test_executor_never_opens_the_breaker(conn: psycopg.Connection) -> None:
    """Breaker state belongs to the monitor job, not the executor (ADR-011)."""
    for _ in range(10):  # well past the threshold of 5
        record_execution_outcome(conn, ProviderOutcome.TIMEOUT)

    row = breaker_row(conn)
    assert row["consecutive_failures"] == 10
    assert row["state"] == "CLOSED"
    assert row["opened_at"] is None


def test_record_outcome_never_writes_state(conn: psycopg.Connection) -> None:
    _open_breaker(conn)

    record_execution_outcome(conn, ProviderOutcome.ACCEPTED)

    # Counter cleared, but the state the monitor owns is untouched.
    assert read_breaker(conn).state == "OPEN"
    assert breaker_row(conn)["consecutive_failures"] == 0
