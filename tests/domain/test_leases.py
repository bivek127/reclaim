"""Lease claim, fencing, sweeper, TTL expiry."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import psycopg
import pytest

from reclaim.config import LEASE_SECONDS
from reclaim.domain.leases import (
    PROVIDER_HTTP_TIMEOUT_SECONDS,
    claim_case,
    claim_next,
    fenced_transition,
    remaining_ttl_ms,
)
from reclaim.domain.states import TERMINAL_STATES, CaseState
from reclaim.domain.sweeper import expire_ttl, sweep_expired_leases
from reclaim.domain.transitions import TransitionIllegal, transition
from tests.db.helpers import insert_case, insert_obligation


def _seed_case(
    conn: psycopg.Connection,
    state: CaseState,
    *,
    fencing_token: int = 0,
    worker_id: str | None = None,
    lease_expired: bool = True,
    ttl_budget_ms: int = 72 * 60 * 60 * 1000,
    active_elapsed_ms: int = 0,
    active_since: datetime | None = None,
) -> int:
    token = uuid.uuid4().hex
    obligation_id = insert_obligation(
        conn,
        anchor_key=token,
        anchor_canonical=f"order:{token}",
        source_event_id=f"evt-{token}",
    )
    if state in TERMINAL_STATES:
        worker_id = None
    if state in TERMINAL_STATES | {CaseState.HALTED}:
        since = None
    elif active_since is not None:
        since = active_since
    else:
        since = datetime.now(timezone.utc) - timedelta(seconds=1)

    case_id = insert_case(
        conn,
        obligation_id,
        state=state.value,
        worker_id=worker_id,
        active_since=since,
        ttl_budget_ms=ttl_budget_ms,
    )
    lease_sql = (
        "lease_expires_at = '-infinity'"
        if lease_expired
        else "lease_expires_at = now() + interval '60 seconds'"
    )
    conn.execute(
        f"""
        UPDATE recovery_cases
           SET fencing_token = %s,
               active_elapsed_ms = %s,
               {lease_sql}
         WHERE id = %s
        """,
        (fencing_token, active_elapsed_ms, case_id),
    )
    return case_id


def _case_row(conn: psycopg.Connection, case_id: int) -> tuple:
    row = conn.execute(
        """
        SELECT state, fencing_token, worker_id, active_since, active_elapsed_ms
          FROM recovery_cases
         WHERE id = %s
        """,
        (case_id,),
    ).fetchone()
    assert row is not None
    return row


def test_execution_lease_covers_two_http_timeouts() -> None:
    assert LEASE_SECONDS["execution"] >= 2 * PROVIDER_HTTP_TIMEOUT_SECONDS
    assert LEASE_SECONDS == {
        "enrichment": 30,
        "diagnosis": 90,
        "policy": 90,
        "review": 90,
        "execution": 60,
        "reconciliation": 45,
        "verification": 45,
    }


def test_claim_increments_fencing_token_and_sets_worker(conn: psycopg.Connection) -> None:
    case_id = _seed_case(conn, CaseState.ACTION_READY, fencing_token=4)
    claimed = claim_case(
        conn,
        case_id,
        CaseState.ACTION_READY,
        "worker-a",
        LEASE_SECONDS["execution"],
    )
    assert claimed is not None
    assert claimed.fencing_token == 5
    state, token, worker_id, _since, _elapsed = _case_row(conn, case_id)
    assert state == "ACTION_READY"
    assert token == 5
    assert worker_id == "worker-a"
    still_leased = conn.execute(
        "SELECT lease_expires_at > now() FROM recovery_cases WHERE id = %s",
        (case_id,),
    ).fetchone()
    assert still_leased == (True,)
    audit = conn.execute(
        "SELECT event_type FROM audit_events WHERE case_id = %s",
        (case_id,),
    ).fetchall()
    assert audit == [("lease_claimed",)]


def test_claim_requires_expired_lease(conn: psycopg.Connection) -> None:
    case_id = _seed_case(
        conn,
        CaseState.ACTION_READY,
        fencing_token=1,
        worker_id="holder",
        lease_expired=False,
    )
    before = _case_row(conn, case_id)
    assert (
        claim_case(
            conn,
            case_id,
            CaseState.ACTION_READY,
            "intruder",
            LEASE_SECONDS["execution"],
        )
        is None
    )
    assert _case_row(conn, case_id)[:3] == before[:3]


def test_claim_requires_expected_state(conn: psycopg.Connection) -> None:
    case_id = _seed_case(conn, CaseState.ACTION_READY, fencing_token=0)
    assert (
        claim_case(
            conn,
            case_id,
            CaseState.EXECUTING,
            "worker-a",
            LEASE_SECONDS["execution"],
        )
        is None
    )
    assert _case_row(conn, case_id)[0] == "ACTION_READY"
    assert _case_row(conn, case_id)[1] == 0


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES, key=lambda s: s.value))
def test_terminal_cases_cannot_be_claimed(
    conn: psycopg.Connection,
    state: CaseState,
) -> None:
    case_id = _seed_case(conn, state, fencing_token=2)
    assert (
        claim_case(conn, case_id, state, "worker-a", LEASE_SECONDS["enrichment"])
        is None
    )
    assert _case_row(conn, case_id)[2] is None


def test_halted_cannot_be_claimed(conn: psycopg.Connection) -> None:
    case_id = _seed_case(conn, CaseState.HALTED, fencing_token=3, worker_id="breaker")
    before = _case_row(conn, case_id)
    assert (
        claim_case(
            conn,
            case_id,
            CaseState.HALTED,
            "worker-a",
            LEASE_SECONDS["execution"],
        )
        is None
    )
    assert _case_row(conn, case_id)[:3] == before[:3]


def test_two_workers_cannot_both_claim(
    conn: psycopg.Connection,
    migrated_database: str,
) -> None:
    case_id = _seed_case(conn, CaseState.ACTION_READY, fencing_token=0)
    barrier = Barrier(2)
    results: list[int | None] = []
    errors: list[BaseException] = []

    def worker(name: str) -> None:
        try:
            with psycopg.connect(migrated_database) as worker_conn:
                barrier.wait(timeout=5)
                claimed = claim_case(
                    worker_conn,
                    case_id,
                    CaseState.ACTION_READY,
                    name,
                    LEASE_SECONDS["execution"],
                )
                worker_conn.commit()
                results.append(None if claimed is None else claimed.fencing_token)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, "worker-a"), pool.submit(worker, "worker-b")]
        for future in futures:
            future.result(timeout=10)

    assert errors == []
    assert results.count(None) == 1
    winners = [token for token in results if token is not None]
    assert winners == [1]
    state, token, worker_id, *_rest = _case_row(conn, case_id)
    assert state == "ACTION_READY"
    assert token == 1
    assert worker_id in {"worker-a", "worker-b"}


def test_stale_worker_write_is_noop(conn: psycopg.Connection) -> None:
    case_id = _seed_case(
        conn,
        CaseState.EXECUTING,
        fencing_token=5,
        worker_id="worker-a",
        lease_expired=True,
    )
    swept = sweep_expired_leases(conn)
    assert swept.executing_to_ambiguous == 1
    state, token, worker_id, *_rest = _case_row(conn, case_id)
    assert state == "AMBIGUOUS"
    assert token == 6
    assert worker_id is None

    applied = fenced_transition(
        conn,
        case_id,
        CaseState.EXECUTING,
        CaseState.AWAITING_CUSTOMER,
        fencing_token=5,
        reason_code="provider_accepted",
        worker_id="worker-a",
    )
    assert applied is False
    state, token, *_rest = _case_row(conn, case_id)
    assert state == "AMBIGUOUS"
    assert token == 6
    stale = conn.execute(
        """
        SELECT event_type, fencing_token
          FROM audit_events
         WHERE case_id = %s AND event_type = 'stale_write_rejected'
        """,
        (case_id,),
    ).fetchall()
    assert stale == [("stale_write_rejected", 5)]
    attempts = conn.execute("SELECT count(*) FROM execution_attempts").fetchone()
    assert attempts == (0,)


def test_lease_expiry_during_provider_call_becomes_ambiguous(
    conn: psycopg.Connection,
) -> None:
    case_id = _seed_case(
        conn,
        CaseState.EXECUTING,
        fencing_token=1,
        worker_id="executor",
        lease_expired=True,
    )
    result = sweep_expired_leases(conn)
    assert result.executing_to_ambiguous == 1
    assert _case_row(conn, case_id)[0] == "AMBIGUOUS"
    audit = conn.execute(
        """
        SELECT event_type, prev_state, new_state, reason_code
          FROM audit_events
         WHERE case_id = %s AND event_type = 'state_transition'
        """,
        (case_id,),
    ).fetchall()
    assert audit == [("state_transition", "EXECUTING", "AMBIGUOUS", "lease_expired")]
    assert conn.execute("SELECT count(*) FROM recovery_actions").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM execution_attempts").fetchone() == (0,)


def test_sweeper_releases_action_ready_without_state_change(
    conn: psycopg.Connection,
) -> None:
    case_id = _seed_case(
        conn,
        CaseState.ACTION_READY,
        fencing_token=2,
        worker_id="executor",
        lease_expired=True,
    )
    result = sweep_expired_leases(conn)
    assert result.released == 1
    state, token, worker_id, *_rest = _case_row(conn, case_id)
    assert state == "ACTION_READY"
    assert token == 3
    assert worker_id is None
    claimed = claim_case(
        conn,
        case_id,
        CaseState.ACTION_READY,
        "executor-2",
        LEASE_SECONDS["execution"],
    )
    assert claimed is not None


def test_sweeper_preserves_ambiguous_and_reconciling(conn: psycopg.Connection) -> None:
    ambiguous_id = _seed_case(
        conn,
        CaseState.AMBIGUOUS,
        fencing_token=1,
        worker_id="reconciler",
        lease_expired=True,
    )
    reconciling_id = _seed_case(
        conn,
        CaseState.RECONCILING,
        fencing_token=1,
        worker_id="reconciler",
        lease_expired=True,
    )
    sweep_expired_leases(conn)
    assert _case_row(conn, ambiguous_id)[0] == "AMBIGUOUS"
    assert _case_row(conn, reconciling_id)[0] == "RECONCILING"
    assert _case_row(conn, ambiguous_id)[2] is None
    assert _case_row(conn, reconciling_id)[2] is None


def test_ambiguous_is_not_executable(conn: psycopg.Connection) -> None:
    case_id = _seed_case(conn, CaseState.AMBIGUOUS, fencing_token=0)
    assert (
        claim_case(
            conn,
            case_id,
            CaseState.EXECUTING,
            "executor",
            LEASE_SECONDS["execution"],
        )
        is None
    )
    with pytest.raises(TransitionIllegal):
        fenced_transition(
            conn,
            case_id,
            CaseState.AMBIGUOUS,
            CaseState.EXECUTING,
            fencing_token=0,
            reason_code="illegal-dispatch",
        )


def test_reconciling_is_not_executable(conn: psycopg.Connection) -> None:
    case_id = _seed_case(conn, CaseState.RECONCILING, fencing_token=0)
    with pytest.raises(TransitionIllegal):
        transition(
            conn,
            case_id,
            CaseState.RECONCILING,
            CaseState.EXECUTING,
            fencing_token=0,
            reason_code="illegal-dispatch",
        )


def test_ttl_pauses_during_halted(conn: psycopg.Connection) -> None:
    started = datetime.now(timezone.utc) - timedelta(seconds=2)
    case_id = _seed_case(
        conn,
        CaseState.ACTION_READY,
        active_since=started,
        active_elapsed_ms=0,
        ttl_budget_ms=60_000,
    )
    remaining_before = remaining_ttl_ms(conn, case_id)
    assert transition(
        conn,
        case_id,
        CaseState.ACTION_READY,
        CaseState.HALTED,
        fencing_token=0,
        reason_code="breaker_open",
    )
    remaining_halted = remaining_ttl_ms(conn, case_id)
    conn.execute("SELECT pg_sleep(1)")
    remaining_after_wait = remaining_ttl_ms(conn, case_id)
    assert remaining_after_wait == remaining_halted
    assert remaining_halted <= remaining_before


def test_breaker_reset_resumes_ttl_from_pause(conn: psycopg.Connection) -> None:
    case_id = _seed_case(
        conn,
        CaseState.HALTED,
        active_elapsed_ms=4_000,
        ttl_budget_ms=60_000,
    )
    remaining_halted = remaining_ttl_ms(conn, case_id)
    before = datetime.now(timezone.utc)
    assert transition(
        conn,
        case_id,
        CaseState.HALTED,
        CaseState.ACTION_READY,
        fencing_token=0,
        reason_code="breaker_reset",
    )
    _state, _token, _worker, active_since, elapsed = _case_row(conn, case_id)
    assert active_since is not None
    assert active_since >= before
    assert elapsed == 4_000
    remaining_resumed = remaining_ttl_ms(conn, case_id)
    assert remaining_resumed <= remaining_halted
    assert remaining_halted - remaining_resumed < 2_000


def test_ttl_expiry_routes_by_prior_state(conn: psycopg.Connection) -> None:
    diagnosing_id = _seed_case(
        conn,
        CaseState.DIAGNOSING,
        ttl_budget_ms=1,
        active_elapsed_ms=5_000,
        active_since=datetime.now(timezone.utc) - timedelta(seconds=2),
    )
    ambiguous_id = _seed_case(
        conn,
        CaseState.AMBIGUOUS,
        ttl_budget_ms=1,
        active_elapsed_ms=5_000,
        active_since=datetime.now(timezone.utc) - timedelta(seconds=2),
    )
    result = expire_ttl(conn)
    assert result.expired == 2
    assert _case_row(conn, diagnosing_id)[0] == "ESCALATED"
    assert _case_row(conn, ambiguous_id)[0] == "EXPIRED_UNRESOLVED"
    assert _case_row(conn, diagnosing_id)[0] != "VERIFIED_FAILED"
    assert _case_row(conn, ambiguous_id)[0] != "VERIFIED_FAILED"


def test_claim_next_uses_skip_locked(conn: psycopg.Connection) -> None:
    case_id = _seed_case(conn, CaseState.ENRICHING, fencing_token=0)
    claimed = claim_next(
        conn,
        CaseState.ENRICHING,
        "case-worker",
        LEASE_SECONDS["enrichment"],
    )
    assert claimed is not None
    assert claimed.case_id == case_id
    assert claimed.fencing_token == 1
