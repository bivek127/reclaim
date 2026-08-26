"""I8: the revenue privilege boundary, proven against real roles.

Every test here executes a real statement through a real role connection and
asserts PostgreSQL's own answer. None of them inspect grant metadata --
"the grant looks right" is not the property under test.
"""

from __future__ import annotations

import itertools

import psycopg
import pytest
from psycopg.errors import CheckViolation, InsufficientPrivilege, RaiseException

from reclaim.domain.leases import claim_case
from reclaim.domain.states import CaseState
from reclaim.domain.transitions import transition
from tests.db.helpers import (
    insert_action,
    insert_attempt,
    insert_case,
    insert_obligation,
    insert_policy_decision,
)

AMOUNT = 10_000


_SEQ = itertools.count(1)


def _paid_case(conn: psycopg.Connection) -> dict[str, int]:
    """A case in AWAITING_CUSTOMER with one attempt. Unique anchor per call."""
    n = next(_SEQ)
    obligation_id = insert_obligation(
        conn,
        anchor_key=f"ord_p{n}",
        anchor_canonical=f"order:ord_p{n}",
        amount_minor=AMOUNT,
        source_event_id=f"evt_p{n}",
    )
    case_id = insert_case(conn, obligation_id, state="AWAITING_CUSTOMER")
    policy_id = insert_policy_decision(conn, case_id)
    action_id = insert_action(conn, case_id, policy_id, status="LIVE")
    attempt_id = insert_attempt(
        conn, action_id, case_id, idempotency_key=f"rcv_p{n}", amount_minor=AMOUNT
    )
    return {
        "obligation_id": obligation_id,
        "case_id": case_id,
        "policy_id": policy_id,
        "action_id": action_id,
        "attempt_id": attempt_id,
    }


def _agreeing_verification(
    conn: psycopg.Connection, ids: dict[str, int], amount: int = AMOUNT
) -> None:
    conn.execute(
        """
        INSERT INTO verifications (case_id, attempt_id, agrees, verified_amount_minor)
        VALUES (%s, %s, true, %s)
        """,
        (ids["case_id"], ids["attempt_id"], amount),
    )


def _revenue(conn: psycopg.Connection, case_id: int) -> int:
    row = conn.execute(
        "SELECT recovered_amount_minor FROM recovery_cases WHERE id = %s", (case_id,)
    ).fetchone()
    assert row is not None
    return int(row[0])


# ---- barrier 1: PostgreSQL column privilege (I8) -------------------------


def test_app_role_cannot_write_recovered_amount(
    conn: psycopg.Connection, app_conn: psycopg.Connection
) -> None:
    """I8's hard edge: the app role is refused by PostgreSQL, not by code."""
    ids = _paid_case(conn)
    _agreeing_verification(conn, ids)

    with pytest.raises(InsufficientPrivilege):
        app_conn.execute(
            "UPDATE recovery_cases SET recovered_amount_minor = %s WHERE id = %s",
            (AMOUNT, ids["case_id"]),
        )


def test_app_role_still_denied_after_migration_020(
    conn: psycopg.Connection, app_conn: psycopg.Connection
) -> None:
    """Migration 020 widened the VERIFIER's grants. It must not widen the app's."""
    ids = _paid_case(conn)
    _agreeing_verification(conn, ids)

    with pytest.raises(InsufficientPrivilege):
        app_conn.execute(
            "UPDATE recovery_cases SET recovered_amount_minor = 1 WHERE id = %s",
            (ids["case_id"],),
        )
    assert _revenue(conn, ids["case_id"]) == 0


def test_app_role_cannot_write_revenue_even_with_other_columns(
    conn: psycopg.Connection, app_conn: psycopg.Connection
) -> None:
    """Smuggling it alongside a column the app *may* write must also fail."""
    ids = _paid_case(conn)
    _agreeing_verification(conn, ids)

    with pytest.raises(InsufficientPrivilege):
        app_conn.execute(
            """
            UPDATE recovery_cases
               SET updated_at = now(), recovered_amount_minor = %s
             WHERE id = %s
            """,
            (AMOUNT, ids["case_id"]),
        )


def test_app_role_retains_its_legitimate_columns(
    conn: psycopg.Connection, app_conn: psycopg.Connection
) -> None:
    """020 must not have broken the app role's normal work."""
    ids = _paid_case(conn)

    app_conn.execute(
        "UPDATE recovery_cases SET worker_id = 'w', updated_at = now() WHERE id = %s",
        (ids["case_id"],),
    )


def test_forged_verification_still_cannot_yield_app_revenue(
    conn: psycopg.Connection, app_conn: psycopg.Connection
) -> None:
    """The strongest I8 statement: barrier 1 holds even against a forged row.

    Migration 018's blanket `GRANT ... ON ALL TABLES` means recovery_app CAN
    insert a verification row -- see test_app_role_can_insert_verifications
    below and ADR-015 for that finding. It does not matter: the app then
    satisfies barriers 2 and 3 completely and is *still* refused the revenue
    write by the column privilege, which is the barrier that cannot be
    worked around from inside the app role.
    """
    ids = _paid_case(conn)

    app_conn.execute(
        """
        INSERT INTO verifications (case_id, attempt_id, agrees, verified_amount_minor)
        VALUES (%s, %s, true, %s)
        """,
        (ids["case_id"], ids["attempt_id"], AMOUNT),
    )
    app_conn.execute(
        "UPDATE recovery_cases SET state = 'VERIFIED_RECOVERED', active_since = NULL "
        "WHERE id = %s",
        (ids["case_id"],),
    )

    with pytest.raises(InsufficientPrivilege):
        app_conn.execute(
            "UPDATE recovery_cases SET recovered_amount_minor = %s WHERE id = %s",
            (AMOUNT, ids["case_id"]),
        )

    assert _revenue(conn, ids["case_id"]) == 0


def test_app_role_can_insert_verifications(
    conn: psycopg.Connection, app_conn: psycopg.Connection
) -> None:
    """Documents a real over-grant deliberately left in place for now.

    The intended grants list INSERT on verifications for the verifier only,
    but migration 018 already granted INSERT on ALL TABLES to recovery_app,
    and 018 does not revoke it. Pinned so the behaviour is visible rather
    than surprising; closing it is a one-line REVOKE (ADR-015).
    """
    ids = _paid_case(conn)

    app_conn.execute(
        """
        INSERT INTO verifications (case_id, attempt_id, agrees, verified_amount_minor)
        VALUES (%s, %s, false, 0)
        """,
        (ids["case_id"], ids["attempt_id"]),
    )


# ---- migration 020: the verifier can now run the real flow ---------------


def test_verifier_can_claim_a_lease(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """Impossible before migration 020."""
    ids = _paid_case(conn)

    claim = claim_case(
        verifier_conn, ids["case_id"], CaseState.AWAITING_CUSTOMER, "verifier-1", 45
    )

    assert claim is not None
    assert claim.fencing_token > 0


def test_verifier_can_transition(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """transition() writes active_elapsed_ms, worker_id and an audit row."""
    ids = _paid_case(conn)
    _agreeing_verification(conn, ids)

    applied = transition(
        verifier_conn,
        ids["case_id"],
        CaseState.AWAITING_CUSTOMER,
        CaseState.VERIFIED_RECOVERED,
        0,
        "verification_agreed",
    )

    assert applied is True


def test_verifier_can_insert_audit_events(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """Every transition needs an audit row; the verifier writes its own."""
    ids = _paid_case(conn)

    verifier_conn.execute(
        """
        INSERT INTO audit_events (event_type, case_id, reason_code)
        VALUES ('verification_recorded', %s, 'test')
        """,
        (ids["case_id"],),
    )


def test_verifier_completes_the_full_verification_transaction(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """Verification, revenue write, and audit all commit as the verifier role,
    in one transaction."""
    ids = _paid_case(conn)

    with verifier_conn.transaction():
        verifier_conn.execute(
            """
            INSERT INTO verifications (case_id, attempt_id, agrees, verified_amount_minor)
            VALUES (%s, %s, true, %s)
            """,
            (ids["case_id"], ids["attempt_id"], AMOUNT),
        )

        def _revenue_write(inner: psycopg.Connection) -> None:
            inner.execute(
                "UPDATE recovery_cases SET recovered_amount_minor = %s WHERE id = %s",
                (AMOUNT, ids["case_id"]),
            )

        applied = transition(
            verifier_conn,
            ids["case_id"],
            CaseState.AWAITING_CUSTOMER,
            CaseState.VERIFIED_RECOVERED,
            0,
            "verification_agreed",
            side_effects=_revenue_write,
        )
        assert applied is True

    assert _revenue(conn, ids["case_id"]) == AMOUNT


def test_verifier_cannot_write_columns_outside_its_grant(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """020 is minimal: attempt_count is the app's, and stays the app's."""
    ids = _paid_case(conn)

    with pytest.raises(InsufficientPrivilege):
        verifier_conn.execute(
            "UPDATE recovery_cases SET attempt_count = 5 WHERE id = %s",
            (ids["case_id"],),
        )


# ---- barrier 2: guard_recovered_amount ----------------------------------


def test_revenue_without_any_verification_is_rejected(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """Even the verifier cannot invent a number no verification supports."""
    ids = _paid_case(conn)
    conn.execute(
        "UPDATE recovery_cases SET state = 'VERIFIED_RECOVERED', active_since = NULL "
        "WHERE id = %s",
        (ids["case_id"],),
    )

    with pytest.raises(RaiseException):
        verifier_conn.execute(
            "UPDATE recovery_cases SET recovered_amount_minor = %s WHERE id = %s",
            (AMOUNT, ids["case_id"]),
        )


def test_revenue_with_disagreeing_verification_is_rejected(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    ids = _paid_case(conn)
    conn.execute(
        """
        INSERT INTO verifications (case_id, attempt_id, agrees, verified_amount_minor)
        VALUES (%s, %s, false, 0)
        """,
        (ids["case_id"], ids["attempt_id"]),
    )
    conn.execute(
        "UPDATE recovery_cases SET state = 'VERIFIED_RECOVERED', active_since = NULL "
        "WHERE id = %s",
        (ids["case_id"],),
    )

    with pytest.raises(RaiseException):
        verifier_conn.execute(
            "UPDATE recovery_cases SET recovered_amount_minor = %s WHERE id = %s",
            (AMOUNT, ids["case_id"]),
        )


def test_revenue_with_mismatched_verified_amount_is_rejected(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """The guard compares the written number to verified_amount_minor exactly."""
    ids = _paid_case(conn)
    _agreeing_verification(conn, ids, amount=AMOUNT)
    conn.execute(
        "UPDATE recovery_cases SET state = 'VERIFIED_RECOVERED', active_since = NULL "
        "WHERE id = %s",
        (ids["case_id"],),
    )

    with pytest.raises(RaiseException):
        verifier_conn.execute(
            "UPDATE recovery_cases SET recovered_amount_minor = %s WHERE id = %s",
            (AMOUNT + 1, ids["case_id"]),
        )


# ---- barrier 3: ck_recovered_only_when_verified --------------------------


def test_revenue_in_wrong_state_is_rejected(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """Non-zero revenue is unwritable outside VERIFIED_RECOVERED."""
    ids = _paid_case(conn)
    _agreeing_verification(conn, ids)

    with pytest.raises(CheckViolation):
        verifier_conn.execute(
            "UPDATE recovery_cases SET recovered_amount_minor = %s WHERE id = %s",
            (AMOUNT, ids["case_id"]),  # still AWAITING_CUSTOMER
        )


def test_all_three_barriers_are_independent(
    conn: psycopg.Connection, app_conn: psycopg.Connection,
    verifier_conn: psycopg.Connection,
) -> None:
    """Each barrier rejects on its own, with the other two satisfied."""
    ids = _paid_case(conn)
    _agreeing_verification(conn, ids)

    # Barrier 1 alone: verification + state fine, wrong role.
    conn.execute(
        "UPDATE recovery_cases SET state = 'VERIFIED_RECOVERED', active_since = NULL "
        "WHERE id = %s",
        (ids["case_id"],),
    )
    with pytest.raises(InsufficientPrivilege):
        app_conn.execute(
            "UPDATE recovery_cases SET recovered_amount_minor = %s WHERE id = %s",
            (AMOUNT, ids["case_id"]),
        )

    # Barrier 2 alone: right role, right state, unsupported amount.
    with pytest.raises(RaiseException):
        verifier_conn.execute(
            "UPDATE recovery_cases SET recovered_amount_minor = %s WHERE id = %s",
            (AMOUNT + 7, ids["case_id"]),
        )

    # Barrier 3 alone: right role, supported amount, wrong state.
    other = _paid_case(conn)
    _agreeing_verification(conn, other)
    with pytest.raises(CheckViolation):
        verifier_conn.execute(
            "UPDATE recovery_cases SET recovered_amount_minor = %s WHERE id = %s",
            (AMOUNT, other["case_id"]),
        )
