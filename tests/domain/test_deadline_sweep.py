"""An expired payment window escalates for human judgement, it does not fail.

The forbidden reaction is the point of this file. The dangerous mistake is
not failing to escalate -- it is concluding that an expired link means the
customer did not pay. That is the highest-consequence unverified assumption
in the system, so most of these tests assert what must NOT happen.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from reclaim.domain.states import CaseState
from reclaim.domain.sweeper import REASON_ACTION_DEADLINE, expire_action_deadlines
from tests.db.helpers import (
    insert_action,
    insert_attempt,
    insert_case,
    insert_obligation,
    insert_policy_decision,
)

_SEQ = itertools.count(1)
AMOUNT = 10_000


def _past(minutes: int = 30) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def _future(minutes: int = 30) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def seed_awaiting(
    conn: psycopg.Connection,
    *,
    deadline: datetime | None,
    state: str = "AWAITING_CUSTOMER",
    action_status: str = "LIVE",
) -> dict[str, int]:
    """A case as a successful dispatch leaves it."""
    n = next(_SEQ)
    obligation_id = insert_obligation(
        conn,
        anchor_key=f"ord_d{n}",
        anchor_canonical=f"order:ord_d{n}",
        amount_minor=AMOUNT,
        customer_ref=f"cust_d{n}",
        source_event_id=f"evt_d{n}",
    )
    case_id = insert_case(conn, obligation_id, state=state)
    policy_id = insert_policy_decision(conn, case_id)
    # ck_deadline_after_provider: action_deadline_at must exceed provider expiry.
    action_id = insert_action(
        conn,
        case_id,
        policy_id,
        status=action_status,
        provider_expires_at=(deadline - timedelta(minutes=10)) if deadline else None,
        action_deadline_at=deadline,
    )
    attempt_id = insert_attempt(
        conn,
        action_id,
        case_id,
        idempotency_key=f"rcv_d{n}",
        provider_reference=f"rcv_d{n}",
        state="ACCEPTED",
        amount_minor=AMOUNT,
    )
    return {
        "case_id": case_id,
        "action_id": action_id,
        "attempt_id": attempt_id,
        "policy_id": policy_id,
    }


def _case(conn: psycopg.Connection, case_id: int) -> dict:
    row = conn.execute(
        "SELECT state::text, recovered_amount_minor, attempt_count, fencing_token "
        "FROM recovery_cases WHERE id = %s",
        (case_id,),
    ).fetchone()
    return {
        "state": row[0],
        "revenue": row[1],
        "attempt_count": row[2],
        "fencing_token": row[3],
    }


def _count(conn: psycopg.Connection, table: str, case_id: int) -> int:
    return conn.execute(
        f"SELECT count(*) FROM {table} WHERE case_id = %s", (case_id,)
    ).fetchone()[0]


# ---- row 23: the escalation itself --------------------------------------


def test_expired_link_is_not_terminal_without_query(
    conn: psycopg.Connection,
) -> None:
    """A past-deadline AWAITING_CUSTOMER case escalates, not fails."""
    ids = seed_awaiting(conn, deadline=_past())

    result = expire_action_deadlines(conn)

    assert result.escalated == 1
    assert _case(conn, ids["case_id"])["state"] == "ESCALATED"


def test_expiry_never_marks_the_action_terminal_failed(
    conn: psycopg.Connection,
) -> None:
    """TERMINAL_FAILED needs provider evidence, which expiry alone is not.

    An expired window says we stopped waiting. It does not say the customer
    failed to pay -- that distinction is the entire subject of this module.
    """
    ids = seed_awaiting(conn, deadline=_past())

    expire_action_deadlines(conn)

    row = conn.execute(
        "SELECT status::text, resolved_at FROM recovery_actions WHERE id = %s",
        (ids["action_id"],),
    ).fetchone()
    assert row[0] == "LIVE"
    assert row[1] is None


def test_expiry_never_creates_a_second_action(conn: psycopg.Connection) -> None:
    """Row 23 forbids an auto second action; I5 forbids two open mechanisms."""
    ids = seed_awaiting(conn, deadline=_past())

    expire_action_deadlines(conn)

    assert _count(conn, "recovery_actions", ids["case_id"]) == 1
    assert _count(conn, "execution_attempts", ids["case_id"]) == 1


def test_expiry_consumes_no_attempt_budget(conn: psycopg.Connection) -> None:
    ids = seed_awaiting(conn, deadline=_past())
    before = _case(conn, ids["case_id"])["attempt_count"]

    expire_action_deadlines(conn)

    assert _case(conn, ids["case_id"])["attempt_count"] == before


def test_expiry_writes_no_revenue(conn: psycopg.Connection) -> None:
    """I8 holds: the sweep runs as recovery_app, which cannot write revenue."""
    ids = seed_awaiting(conn, deadline=_past())

    expire_action_deadlines(conn)

    assert _case(conn, ids["case_id"])["revenue"] == 0


def test_expiry_creates_no_provider_request(conn: psycopg.Connection) -> None:
    """The sweep is not a provider interaction."""
    ids = seed_awaiting(conn, deadline=_past())

    expire_action_deadlines(conn)

    n = conn.execute(
        "SELECT count(*) FROM provider_requests pr "
        "JOIN execution_attempts ea ON ea.id = pr.attempt_id WHERE ea.case_id = %s",
        (ids["case_id"],),
    ).fetchone()[0]
    assert n == 0


def test_sweeper_imports_nothing_from_the_provider_layer() -> None:
    """Structural: no provider call is possible, not merely absent."""
    import ast
    import inspect

    from reclaim.domain import sweeper

    tree = ast.parse(inspect.getsource(sweeper))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    assert [m for m in imported if "provider" in m or "razorpay" in m] == []


# ---- consistency with the existing escalation path: provenance + review --


def test_escalation_creates_exactly_one_pending_review(
    conn: psycopg.Connection,
) -> None:
    """Escalation entry, via the existing on_entered_escalated TTL path."""
    ids = seed_awaiting(conn, deadline=_past())

    expire_action_deadlines(conn)

    rows = conn.execute(
        "SELECT status::text FROM human_reviews WHERE case_id = %s",
        (ids["case_id"],),
    ).fetchall()
    assert [r[0] for r in rows] == ["PENDING"]


def test_escalation_records_provenance(conn: psycopg.Connection) -> None:
    """A reviewer must be able to see why the case arrived."""
    ids = seed_awaiting(conn, deadline=_past())

    expire_action_deadlines(conn)

    reasons = conn.execute(
        "SELECT reason_code FROM policy_decisions WHERE case_id = %s ORDER BY id",
        (ids["case_id"],),
    ).fetchall()
    assert REASON_ACTION_DEADLINE in [r[0] for r in reasons]


# ---- negative cases: no premature escalation ----------------------------


def test_case_before_its_deadline_is_untouched(conn: psycopg.Connection) -> None:
    ids = seed_awaiting(conn, deadline=_future())

    result = expire_action_deadlines(conn)

    assert result.escalated == 0
    assert _case(conn, ids["case_id"])["state"] == "AWAITING_CUSTOMER"
    assert _count(conn, "human_reviews", ids["case_id"]) == 0


def test_null_deadline_is_untouched(conn: psycopg.Connection) -> None:
    """A missing deadline must never be read as 'expired'."""
    ids = seed_awaiting(conn, deadline=None)

    result = expire_action_deadlines(conn)

    assert result.escalated == 0
    assert _case(conn, ids["case_id"])["state"] == "AWAITING_CUSTOMER"


@pytest.mark.parametrize("state", ["EXECUTING", "AMBIGUOUS", "VERIFIED_RECOVERED"])
def test_only_awaiting_customer_is_swept(
    conn: psycopg.Connection, state: str
) -> None:
    ids = seed_awaiting(conn, deadline=_past(), state=state)

    result = expire_action_deadlines(conn)

    assert result.escalated == 0
    assert _case(conn, ids["case_id"])["state"] == state


def test_resolved_action_is_not_swept(conn: psycopg.Connection) -> None:
    """Only a LIVE action's window can expire."""
    ids = seed_awaiting(conn, deadline=_past(), action_status="UNRESOLVED")

    result = expire_action_deadlines(conn)

    assert result.escalated == 0


def test_already_escalated_case_is_not_swept_twice(
    conn: psycopg.Connection,
) -> None:
    """Idempotence: a second tick must not add a second review."""
    ids = seed_awaiting(conn, deadline=_past())
    expire_action_deadlines(conn)

    second = expire_action_deadlines(conn)

    assert second.escalated == 0
    assert _count(conn, "human_reviews", ids["case_id"]) == 1


# ---- fencing + audit ----------------------------------------------------


def test_sweep_bumps_the_fencing_token(conn: psycopg.Connection) -> None:
    """I6: a worker holding the old token must lose."""
    ids = seed_awaiting(conn, deadline=_past())
    before = _case(conn, ids["case_id"])["fencing_token"]

    expire_action_deadlines(conn)

    assert _case(conn, ids["case_id"])["fencing_token"] > before


def test_stale_worker_cannot_undo_the_escalation(
    conn: psycopg.Connection,
) -> None:
    """A verifier holding the pre-sweep token writes nothing."""
    from reclaim.domain.leases import fenced_transition

    ids = seed_awaiting(conn, deadline=_past())
    stale_token = _case(conn, ids["case_id"])["fencing_token"]
    expire_action_deadlines(conn)

    applied = fenced_transition(
        conn,
        ids["case_id"],
        CaseState.AWAITING_CUSTOMER,
        CaseState.VERIFIED_RECOVERED,
        stale_token,
        "late_verification",
        worker_id="stale-verifier",
    )

    assert applied is False
    assert _case(conn, ids["case_id"])["state"] == "ESCALATED"
    assert _case(conn, ids["case_id"])["revenue"] == 0


def test_escalation_is_reconstructable_from_the_audit_trail(
    conn: psycopg.Connection,
) -> None:
    """The escalation must be explainable from audit_events alone."""
    from reclaim.audit import load_case_audit_trail, reconstruct_case_history

    ids = seed_awaiting(conn, deadline=_past())
    expire_action_deadlines(conn)

    history = reconstruct_case_history(load_case_audit_trail(conn, ids["case_id"]))

    escalations = [
        s
        for s in history.state_changes
        if s.new_state == "ESCALATED" and s.reason_code == REASON_ACTION_DEADLINE
    ]
    assert len(escalations) == 1
    assert escalations[0].prev_state == "AWAITING_CUSTOMER"


def test_transition_and_review_are_one_atomic_commit(
    conn: psycopg.Connection,
) -> None:
    """State, provenance, review and audit land together or not at all."""
    ids = seed_awaiting(conn, deadline=_past())

    expire_action_deadlines(conn)

    assert _case(conn, ids["case_id"])["state"] == "ESCALATED"
    assert _count(conn, "human_reviews", ids["case_id"]) == 1
    assert _count(conn, "policy_decisions", ids["case_id"]) >= 1
    audit = conn.execute(
        "SELECT count(*) FROM audit_events WHERE case_id = %s AND reason_code = %s",
        (ids["case_id"], REASON_ACTION_DEADLINE),
    ).fetchone()[0]
    assert audit >= 1


def test_batch_limit_is_respected(conn: psycopg.Connection) -> None:
    for _ in range(3):
        seed_awaiting(conn, deadline=_past())

    result = expire_action_deadlines(conn, limit=2)

    assert result.escalated == 2
