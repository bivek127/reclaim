"""The recovery-case state machine."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from reclaim.domain.states import (
    ALLOWED_TRANSITIONS,
    CASE_STATES,
    CLOCK_STOPPED_STATES,
    TERMINAL_STATES,
    CaseState,
)
from reclaim.domain.transitions import TransitionIllegal, transition
from tests.db.helpers import insert_case, insert_obligation

LEGAL = sorted(ALLOWED_TRANSITIONS, key=lambda pair: (pair[0].value, pair[1].value))
ILLEGAL = [
    (src, dst)
    for src in CASE_STATES
    for dst in CASE_STATES
    if (src, dst) not in ALLOWED_TRANSITIONS
]


def _seed_case(
    conn: psycopg.Connection,
    state: CaseState,
    *,
    fencing_token: int = 0,
    worker_id: str | None = "worker-1",
    active_since: datetime | None = None,
    active_elapsed_ms: int = 0,
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
    if state in CLOCK_STOPPED_STATES:
        since = None
    elif active_since is not None:
        since = active_since
    else:
        since = datetime.now(timezone.utc) - timedelta(seconds=2)

    case_id = insert_case(
        conn,
        obligation_id,
        state=state.value,
        worker_id=worker_id,
        active_since=since,
    )
    conn.execute(
        """
        UPDATE recovery_cases
           SET fencing_token = %s,
               active_elapsed_ms = %s
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


def _audit_rows(conn: psycopg.Connection, case_id: int) -> list[tuple]:
    return conn.execute(
        """
        SELECT prev_state, new_state, fencing_token, worker_id, reason_code, event_type
          FROM audit_events
         WHERE case_id = %s
         ORDER BY id
        """,
        (case_id,),
    ).fetchall()


@pytest.mark.parametrize(
    "expected,new",
    LEGAL,
    ids=[f"{src.value}->{dst.value}" for src, dst in LEGAL],
)
def test_legal_transition_succeeds(
    conn: psycopg.Connection,
    expected: CaseState,
    new: CaseState,
) -> None:
    case_id = _seed_case(conn, expected, fencing_token=7, worker_id="worker-legal")
    assert transition(
        conn,
        case_id,
        expected,
        new,
        fencing_token=7,
        reason_code=f"ok:{expected.value}->{new.value}",
    ) is True

    state, token, worker_id, active_since, _elapsed = _case_row(conn, case_id)
    assert state == new.value
    assert token == 7
    if new in TERMINAL_STATES:
        assert worker_id is None
        assert active_since is None
    elif new is CaseState.HALTED:
        assert active_since is None
    else:
        assert active_since is not None

    audit = _audit_rows(conn, case_id)
    assert len(audit) == 1
    prev, nxt, fencing, worker, reason, event_type = audit[0]
    assert prev == expected.value
    assert nxt == new.value
    assert fencing == 7
    assert worker == "worker-legal"
    assert reason == f"ok:{expected.value}->{new.value}"
    assert event_type == "state_transition"


@pytest.mark.parametrize(
    "expected,new",
    ILLEGAL,
    ids=[f"{src.value}->{dst.value}" for src, dst in ILLEGAL],
)
def test_illegal_transition_raises_and_writes_nothing(
    conn: psycopg.Connection,
    expected: CaseState,
    new: CaseState,
) -> None:
    case_id = _seed_case(conn, expected, fencing_token=3, worker_id="worker-illegal")
    before = _case_row(conn, case_id)
    with pytest.raises(TransitionIllegal):
        transition(
            conn,
            case_id,
            expected,
            new,
            fencing_token=3,
            reason_code="should-not-apply",
            side_effects=lambda _c: (_ for _ in ()).throw(AssertionError("side effect ran")),
        )
    assert _case_row(conn, case_id) == before
    assert _audit_rows(conn, case_id) == []


def test_ambiguous_cannot_go_to_executing(conn: psycopg.Connection) -> None:
    case_id = _seed_case(conn, CaseState.AMBIGUOUS)
    with pytest.raises(TransitionIllegal):
        transition(
            conn,
            case_id,
            CaseState.AMBIGUOUS,
            CaseState.EXECUTING,
            fencing_token=0,
            reason_code="forbidden",
        )
    assert _case_row(conn, case_id)[0] == "AMBIGUOUS"
    assert _audit_rows(conn, case_id) == []


def test_reconciling_cannot_go_to_executing(conn: psycopg.Connection) -> None:
    case_id = _seed_case(conn, CaseState.RECONCILING)
    with pytest.raises(TransitionIllegal):
        transition(
            conn,
            case_id,
            CaseState.RECONCILING,
            CaseState.EXECUTING,
            fencing_token=0,
            reason_code="forbidden",
        )
    assert _case_row(conn, case_id)[0] == "RECONCILING"
    assert _audit_rows(conn, case_id) == []


def test_wrong_expected_state_returns_false(conn: psycopg.Connection) -> None:
    case_id = _seed_case(conn, CaseState.NEW, fencing_token=1)
    ran: list[int] = []
    result = transition(
        conn,
        case_id,
        CaseState.ENRICHING,
        CaseState.DIAGNOSING,
        fencing_token=1,
        reason_code="stale-expected",
        side_effects=lambda _c: ran.append(1),
    )
    assert result is False
    assert ran == []
    assert _case_row(conn, case_id)[0] == "NEW"
    assert _audit_rows(conn, case_id) == []


def test_wrong_fencing_token_returns_false(conn: psycopg.Connection) -> None:
    case_id = _seed_case(conn, CaseState.NEW, fencing_token=5)
    ran: list[int] = []
    result = transition(
        conn,
        case_id,
        CaseState.NEW,
        CaseState.ENRICHING,
        fencing_token=4,
        reason_code="stale-token",
        side_effects=lambda _c: ran.append(1),
    )
    assert result is False
    assert ran == []
    assert _case_row(conn, case_id)[0] == "NEW"
    assert _audit_rows(conn, case_id) == []


def test_side_effect_failure_rolls_back_state_and_audit(conn: psycopg.Connection) -> None:
    case_id = _seed_case(conn, CaseState.NEW, fencing_token=0)
    event_id = f"side-{uuid.uuid4().hex}"

    def boom(tx: psycopg.Connection) -> None:
        tx.execute(
            """
            INSERT INTO webhook_events (
                provider_event_id, event_type, signature_valid,
                resolution, payload
            ) VALUES (%s, 'payment.failed', true, 'IGNORED', '{}'::jsonb)
            """,
            (event_id,),
        )
        raise RuntimeError("side effect failed")

    with pytest.raises(RuntimeError, match="side effect failed"):
        transition(
            conn,
            case_id,
            CaseState.NEW,
            CaseState.ENRICHING,
            fencing_token=0,
            reason_code="atomic",
            side_effects=boom,
        )

    assert _case_row(conn, case_id)[0] == "NEW"
    assert _audit_rows(conn, case_id) == []
    leftover = conn.execute(
        "SELECT count(*) FROM webhook_events WHERE provider_event_id = %s",
        (event_id,),
    ).fetchone()
    assert leftover == (0,)


def test_successful_side_effect_commits_with_transition(conn: psycopg.Connection) -> None:
    case_id = _seed_case(conn, CaseState.NEW, fencing_token=0)
    event_id = f"ok-{uuid.uuid4().hex}"

    def persist(tx: psycopg.Connection) -> None:
        tx.execute(
            """
            INSERT INTO webhook_events (
                provider_event_id, event_type, signature_valid,
                resolution, payload
            ) VALUES (%s, 'payment.failed', true, 'IGNORED', '{}'::jsonb)
            """,
            (event_id,),
        )

    assert transition(
        conn,
        case_id,
        CaseState.NEW,
        CaseState.ENRICHING,
        fencing_token=0,
        reason_code="with-side-effect",
        side_effects=persist,
    )
    assert _case_row(conn, case_id)[0] == "ENRICHING"
    assert len(_audit_rows(conn, case_id)) == 1
    row = conn.execute(
        "SELECT count(*) FROM webhook_events WHERE provider_event_id = %s",
        (event_id,),
    ).fetchone()
    assert row == (1,)


def test_entering_halted_stops_ttl_clock(conn: psycopg.Connection) -> None:
    started = datetime.now(timezone.utc) - timedelta(seconds=3)
    case_id = _seed_case(
        conn,
        CaseState.ACTION_READY,
        active_since=started,
        active_elapsed_ms=1000,
    )
    assert transition(
        conn,
        case_id,
        CaseState.ACTION_READY,
        CaseState.HALTED,
        fencing_token=0,
        reason_code="breaker_open",
    )
    state, _token, _worker, active_since, elapsed = _case_row(conn, case_id)
    assert state == "HALTED"
    assert active_since is None
    assert elapsed >= 1000 + 2000


def test_leaving_halted_restarts_active_since(conn: psycopg.Connection) -> None:
    case_id = _seed_case(conn, CaseState.HALTED, active_elapsed_ms=5000)
    before = datetime.now(timezone.utc)
    assert transition(
        conn,
        case_id,
        CaseState.HALTED,
        CaseState.ACTION_READY,
        fencing_token=0,
        reason_code="breaker_reset",
    )
    state, _token, _worker, active_since, elapsed = _case_row(conn, case_id)
    assert state == "ACTION_READY"
    assert active_since is not None
    assert active_since >= before
    assert elapsed == 5000


def test_entering_terminal_stops_ttl_clock(conn: psycopg.Connection) -> None:
    started = datetime.now(timezone.utc) - timedelta(seconds=2)
    case_id = _seed_case(
        conn,
        CaseState.POLICY_EVAL,
        worker_id="worker-term",
        active_since=started,
        active_elapsed_ms=250,
    )
    assert transition(
        conn,
        case_id,
        CaseState.POLICY_EVAL,
        CaseState.VERIFIED_FAILED,
        fencing_token=0,
        reason_code="no_viable_action",
    )
    state, _token, worker_id, active_since, elapsed = _case_row(conn, case_id)
    assert state == "VERIFIED_FAILED"
    assert worker_id is None
    assert active_since is None
    assert elapsed >= 250
    audit = _audit_rows(conn, case_id)
    assert audit[0][3] == "worker-term"
