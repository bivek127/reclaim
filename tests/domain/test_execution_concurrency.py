"""Execution races against real concurrent PostgreSQL connections.

Every test here opens genuinely independent psycopg connections in separate
threads. The constraints under test *are* the thing being tested, so mocking
the database would defeat the purpose entirely.
"""

from __future__ import annotations

import threading
from typing import Any

import psycopg
import pytest
from psycopg.errors import UniqueViolation

from reclaim.domain.execution import (
    BudgetExhausted,
    DispatchAborted,
    call_provider,
    dispatch,
    prepare_dispatch,
    settle_dispatch,
)
from reclaim.domain.leases import claim_case
from reclaim.domain.states import CaseState
from reclaim.domain.sweeper import sweep_expired_leases
from reclaim.provider.contract import ProviderOutcome
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


def _run_parallel(dsn: str, worker, count: int = 2) -> list[Any]:
    """Each thread gets its own connection and its own transaction."""
    results: list[Any] = [None] * count
    barrier = threading.Barrier(count)

    def target(index: int) -> None:
        with psycopg.connect(dsn, autocommit=True) as own:
            barrier.wait()
            try:
                results[index] = ("ok", worker(own, index))
            except Exception as exc:  # noqa: BLE001 - recorded, then asserted on
                results[index] = ("err", exc)

    threads = [threading.Thread(target=target, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads), "a worker thread deadlocked"
    return results


def _outcomes(results: list[Any]) -> tuple[int, list[Exception]]:
    ok = sum(1 for kind, _ in results if kind == "ok")
    errors = [value for kind, value in results if kind == "err"]
    return ok, errors


# ---- two workers, one case -----------------------------------------------


def test_two_workers_cannot_both_claim_for_execution(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    ids = seed_dispatchable(conn)

    def worker(own: psycopg.Connection, _i: int):
        claim = claim_case(own, ids["case_id"], CaseState.ACTION_READY, "w", 60)
        if claim is None:
            raise RuntimeError("lost the claim race")
        return claim.fencing_token

    ok, errors = _outcomes(_run_parallel(migrated_database, worker))

    assert ok == 1, "exactly one worker may hold the lease"
    assert len(errors) == 1


def test_concurrent_dispatch_produces_one_attempt(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    """The decisive duplicate-charge test: two dispatchers, one financial mechanism."""
    ids = seed_dispatchable(conn)
    providers = [StubProvider(), StubProvider()]

    def worker(own: psycopg.Connection, index: int):
        return dispatch(
            own,
            ids["case_id"],
            provider=providers[index],
            fencing_token=0,
            policy_decision_id=ids["policy_id"],
            link_ttl_seconds=LINK_TTL_SECONDS,
            worker_id=f"w{index}",
        )

    ok, _errors = _outcomes(_run_parallel(migrated_database, worker))

    assert ok == 1, "only one dispatch may succeed"
    assert len(attempts_for(conn, ids["case_id"])) == 1
    assert len(actions_for(conn, ids["case_id"])) == 1
    assert sum(len(p.calls) for p in providers) == 1, "the provider was called twice"


def test_concurrent_dispatch_spends_exactly_one_budget_unit(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    ids = seed_dispatchable(conn, max_attempts=2)

    def worker(own: psycopg.Connection, index: int):
        return dispatch(
            own,
            ids["case_id"],
            provider=StubProvider(),
            fencing_token=0,
            policy_decision_id=ids["policy_id"],
            link_ttl_seconds=LINK_TTL_SECONDS,
        )

    _run_parallel(migrated_database, worker)

    assert case_row(conn, ids["case_id"])["attempt_count"] == 1


def test_race_for_the_last_budget_unit(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    """max_attempts=1 with one already spent leaves nothing; nobody may dispatch."""
    ids = seed_dispatchable(conn, max_attempts=1, attempt_count=1)
    providers = [StubProvider(), StubProvider()]

    def worker(own: psycopg.Connection, index: int):
        return dispatch(
            own,
            ids["case_id"],
            provider=providers[index],
            fencing_token=0,
            policy_decision_id=ids["policy_id"],
            link_ttl_seconds=LINK_TTL_SECONDS,
        )

    ok, errors = _outcomes(_run_parallel(migrated_database, worker))

    assert ok == 0
    assert all(isinstance(e, BudgetExhausted) for e in errors)
    assert sum(len(p.calls) for p in providers) == 0
    assert case_row(conn, ids["case_id"])["attempt_count"] == 1


def test_one_budget_unit_two_racers_one_winner(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    ids = seed_dispatchable(conn, max_attempts=1, attempt_count=0)
    providers = [StubProvider(), StubProvider()]

    def worker(own: psycopg.Connection, index: int):
        return dispatch(
            own,
            ids["case_id"],
            provider=providers[index],
            fencing_token=0,
            policy_decision_id=ids["policy_id"],
            link_ttl_seconds=LINK_TTL_SECONDS,
        )

    ok, _ = _outcomes(_run_parallel(migrated_database, worker))

    assert ok == 1
    assert sum(len(p.calls) for p in providers) == 1
    assert case_row(conn, ids["case_id"])["attempt_count"] == 1


# ---- fencing / stale worker ----------------------------------------------


def test_stale_worker_write_back_is_a_noop(conn: psycopg.Connection) -> None:
    """I6: a slow worker returning after the sweeper must not clobber state."""
    ids = seed_dispatchable(conn)
    claim = claim_case(conn, ids["case_id"], CaseState.ACTION_READY, "slow", 60)
    assert claim is not None
    prepared = prepare_dispatch(
        conn,
        ids["case_id"],
        fencing_token=claim.fencing_token,
        policy_decision_id=ids["policy_id"],
        link_ttl_seconds=LINK_TTL_SECONDS,
        worker_id="slow",
    )
    result = call_provider(StubProvider(ProviderOutcome.ACCEPTED), prepared)

    # Sweeper reclaims mid-flight and bumps the token.
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = now() - interval '1 s' WHERE id=%s",
        (ids["case_id"],),
    )
    sweep_expired_leases(conn)

    settled = settle_dispatch(
        conn, prepared, result, fencing_token=claim.fencing_token, worker_id="slow"
    )

    assert settled.applied is False, "a stale token must not apply"
    assert case_row(conn, ids["case_id"])["state"] == "AMBIGUOUS"


def test_stale_write_back_is_audited(conn: psycopg.Connection) -> None:
    ids = seed_dispatchable(conn)
    claim = claim_case(conn, ids["case_id"], CaseState.ACTION_READY, "slow", 60)
    assert claim is not None
    prepared = prepare_dispatch(
        conn,
        ids["case_id"],
        fencing_token=claim.fencing_token,
        policy_decision_id=ids["policy_id"],
        link_ttl_seconds=LINK_TTL_SECONDS,
        worker_id="slow",
    )
    result = call_provider(StubProvider(), prepared)
    conn.execute(
        "UPDATE recovery_cases SET lease_expires_at = now() - interval '1 s' WHERE id=%s",
        (ids["case_id"],),
    )
    sweep_expired_leases(conn)

    settle_dispatch(
        conn, prepared, result, fencing_token=claim.fencing_token, worker_id="slow"
    )

    row = conn.execute(
        "SELECT count(*) FROM audit_events WHERE case_id=%s AND reason_code='stale_write_rejected'",
        (ids["case_id"],),
    ).fetchone()
    assert row is not None and row[0] >= 1


def test_stale_dispatch_attempt_is_refused(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    ids = seed_dispatchable(conn)
    claim = claim_case(conn, ids["case_id"], CaseState.ACTION_READY, "w1", 60)
    assert claim is not None
    provider = StubProvider()

    with pytest.raises(BudgetExhausted):
        dispatch(
            conn,
            ids["case_id"],
            provider=provider,
            fencing_token=claim.fencing_token - 1,  # stale
            policy_decision_id=ids["policy_id"],
            link_ttl_seconds=LINK_TTL_SECONDS,
        )

    assert provider.calls == []


# ---- breaker counter under concurrency -----------------------------------


def test_concurrent_settlements_count_exactly(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    """No lost updates on consecutive_failures."""
    cases = [seed_dispatchable(conn, suffix=f"cc{i}") for i in range(4)]

    def worker(own: psycopg.Connection, index: int):
        return dispatch(
            own,
            cases[index]["case_id"],
            provider=StubProvider(ProviderOutcome.TIMEOUT),
            fencing_token=0,
            policy_decision_id=cases[index]["policy_id"],
            link_ttl_seconds=LINK_TTL_SECONDS,
        )

    ok, errors = _outcomes(_run_parallel(migrated_database, worker, count=4))

    assert ok == 4, f"unexpected failures: {errors}"
    assert breaker_row(conn)["consecutive_failures"] == 4
    assert breaker_row(conn)["state"] == "CLOSED"


# ---- I5 under concurrency ------------------------------------------------


def test_second_action_blocked_while_first_unresolved(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    """Two workers cannot both open an action on the same case, under real
    concurrency."""
    ids = seed_dispatchable(conn)
    dispatch(
        conn,
        ids["case_id"],
        provider=StubProvider(ProviderOutcome.TIMEOUT),
        fencing_token=0,
        policy_decision_id=ids["policy_id"],
        link_ttl_seconds=LINK_TTL_SECONDS,
    )
    conn.execute(
        "UPDATE recovery_cases SET state='ACTION_READY' WHERE id=%s", (ids["case_id"],)
    )

    def worker(own: psycopg.Connection, index: int):
        return dispatch(
            own,
            ids["case_id"],
            provider=StubProvider(),
            fencing_token=0,
            policy_decision_id=ids["policy_id"],
            link_ttl_seconds=LINK_TTL_SECONDS,
        )

    ok, errors = _outcomes(_run_parallel(migrated_database, worker))

    assert ok == 0
    assert all(isinstance(e, UniqueViolation) for e in errors), errors
    assert len(actions_for(conn, ids["case_id"])) == 1
    assert len(attempts_for(conn, ids["case_id"])) == 1
