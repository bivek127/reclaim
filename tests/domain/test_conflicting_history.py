"""Resolution of `conflicting_history` from settled prior cases.

The ambiguity formula itself is the policy suite's subject. What matters here
is which rows the accessor is willing to count: only this customer's, only
settled outcomes, only inside the window, never the case being evaluated.

Every test runs against real PostgreSQL, because what is under test is a query
whose correctness lives in joins, an enum comparison and an interval.
"""

from __future__ import annotations

import psycopg
import pytest

from reclaim.domain.policy import resolve_conflicting_history
from reclaim.domain.states import CaseState
from tests.db.helpers import insert_case, insert_obligation

CUSTOMER = "cust_conflict@example.com"
OTHER_CUSTOMER = "cust_other@example.com"


def make_case(
    conn: psycopg.Connection,
    suffix: str,
    *,
    state: CaseState = CaseState.POLICY_EVAL,
    customer_ref: str = CUSTOMER,
    settled_days_ago: float | None = None,
) -> int:
    """A case for `customer_ref`, optionally back-dated to when it settled.

    `updated_at` is written directly because the accessor reads the moment an
    outcome was recorded, and a transition cannot be made to have happened in
    the past through the domain's own primitives.
    """
    obligation_id = insert_obligation(
        conn,
        anchor_key=f"ord_ch_{suffix}",
        anchor_canonical=f"order:ord_ch_{suffix}",
        source_event_id=f"evt_ch_{suffix}",
        customer_ref=customer_ref,
    )
    case_id = insert_case(conn, obligation_id, state=state.value)
    if settled_days_ago is not None:
        conn.execute(
            "UPDATE recovery_cases SET updated_at = now() - (%s || ' days')::interval"
            " WHERE id = %s",
            (str(settled_days_ago), case_id),
        )
    return case_id


@pytest.fixture
def subject(conn: psycopg.Connection) -> int:
    """The case being evaluated: this customer's, and not yet settled."""
    return make_case(conn, "subject")


def test_a_customer_with_no_prior_cases_has_no_conflict(
    conn: psycopg.Connection, subject: int
) -> None:
    assert resolve_conflicting_history(conn, subject) is False


def test_only_a_recovered_case_is_not_a_conflict(
    conn: psycopg.Connection, subject: int
) -> None:
    """One side of the formula is not the formula."""
    make_case(conn, "only_ok", state=CaseState.VERIFIED_RECOVERED, settled_days_ago=5)
    assert resolve_conflicting_history(conn, subject) is False


def test_only_a_failed_case_is_not_a_conflict(
    conn: psycopg.Connection, subject: int
) -> None:
    make_case(conn, "only_bad", state=CaseState.VERIFIED_FAILED, settled_days_ago=5)
    assert resolve_conflicting_history(conn, subject) is False


def test_both_outcomes_inside_the_window_conflict(
    conn: psycopg.Connection, subject: int
) -> None:
    make_case(conn, "both_ok", state=CaseState.VERIFIED_RECOVERED, settled_days_ago=5)
    make_case(conn, "both_bad", state=CaseState.VERIFIED_FAILED, settled_days_ago=20)
    assert resolve_conflicting_history(conn, subject) is True


def test_both_outcomes_outside_the_window_do_not_conflict(
    conn: psycopg.Connection, subject: int
) -> None:
    """History that has aged out is not history the formula sees."""
    make_case(conn, "old_ok", state=CaseState.VERIFIED_RECOVERED, settled_days_ago=45)
    make_case(conn, "old_bad", state=CaseState.VERIFIED_FAILED, settled_days_ago=60)
    assert resolve_conflicting_history(conn, subject) is False


def test_one_side_ageing_out_ends_the_conflict(
    conn: psycopg.Connection, subject: int
) -> None:
    """Both sides must be inside the window, not merely both present."""
    make_case(conn, "split_ok", state=CaseState.VERIFIED_RECOVERED, settled_days_ago=2)
    make_case(conn, "split_bad", state=CaseState.VERIFIED_FAILED, settled_days_ago=90)
    assert resolve_conflicting_history(conn, subject) is False


def test_expired_unresolved_is_not_counted_as_failure(
    conn: psycopg.Connection, subject: int
) -> None:
    """Nobody established what happened, so it is not evidence of failure."""
    make_case(conn, "exp_ok", state=CaseState.VERIFIED_RECOVERED, settled_days_ago=3)
    make_case(
        conn, "exp_unres", state=CaseState.EXPIRED_UNRESOLVED, settled_days_ago=3
    )
    assert resolve_conflicting_history(conn, subject) is False


def test_expired_unresolved_is_not_counted_as_success_either(
    conn: psycopg.Connection, subject: int
) -> None:
    make_case(conn, "exp2_bad", state=CaseState.VERIFIED_FAILED, settled_days_ago=3)
    make_case(
        conn, "exp2_unres", state=CaseState.EXPIRED_UNRESOLVED, settled_days_ago=3
    )
    assert resolve_conflicting_history(conn, subject) is False


def test_unsettled_cases_are_not_counted(
    conn: psycopg.Connection, subject: int
) -> None:
    """A case still in flight has no outcome to weigh."""
    make_case(conn, "live_ok", state=CaseState.VERIFIED_RECOVERED, settled_days_ago=3)
    make_case(conn, "live_await", state=CaseState.AWAITING_CUSTOMER)
    make_case(conn, "live_escal", state=CaseState.ESCALATED)
    assert resolve_conflicting_history(conn, subject) is False


def test_another_customers_history_is_ignored(
    conn: psycopg.Connection, subject: int
) -> None:
    make_case(
        conn, "them_ok", state=CaseState.VERIFIED_RECOVERED,
        customer_ref=OTHER_CUSTOMER, settled_days_ago=3,
    )
    make_case(
        conn, "them_bad", state=CaseState.VERIFIED_FAILED,
        customer_ref=OTHER_CUSTOMER, settled_days_ago=3,
    )
    assert resolve_conflicting_history(conn, subject) is False


def test_a_second_customers_history_does_not_leak_into_the_first(
    conn: psycopg.Connection, subject: int
) -> None:
    """Both customers have history; only the subject's own counts."""
    make_case(conn, "mine_ok", state=CaseState.VERIFIED_RECOVERED, settled_days_ago=3)
    make_case(
        conn, "theirs_bad", state=CaseState.VERIFIED_FAILED,
        customer_ref=OTHER_CUSTOMER, settled_days_ago=3,
    )
    assert resolve_conflicting_history(conn, subject) is False


def test_the_case_being_evaluated_is_excluded(conn: psycopg.Connection) -> None:
    """A terminal case asked about itself must not answer with itself."""
    settled = make_case(
        conn, "self_ok", state=CaseState.VERIFIED_RECOVERED, settled_days_ago=1
    )
    make_case(conn, "self_bad", state=CaseState.VERIFIED_FAILED, settled_days_ago=1)

    # Both sides exist among this customer's cases, but one of them *is* the
    # subject, so only one side remains once it is excluded.
    assert resolve_conflicting_history(conn, settled) is False


def test_customer_identity_comes_from_the_cases_own_obligation(
    conn: psycopg.Connection,
) -> None:
    """Two cases of the same customer are linked only through `customer_ref`;
    the accessor is given a case id and must make that hop itself."""
    subject = make_case(conn, "hop_subject")
    make_case(conn, "hop_ok", state=CaseState.VERIFIED_RECOVERED, settled_days_ago=4)
    make_case(conn, "hop_bad", state=CaseState.VERIFIED_FAILED, settled_days_ago=4)
    assert resolve_conflicting_history(conn, subject) is True

    # Same rows, a subject belonging to someone else: the hop is what changes
    # the answer, not the presence of qualifying history in the table.
    stranger = make_case(conn, "hop_stranger", customer_ref=OTHER_CUSTOMER)
    assert resolve_conflicting_history(conn, stranger) is False


def test_the_window_boundary_includes_just_inside_and_excludes_just_outside(
    conn: psycopg.Connection, subject: int
) -> None:
    """Asserted a second either side of 30 days rather than exactly on it:
    the query evaluates `now()` after the fixture writes, so a row placed
    exactly on the boundary would age past it between the two."""
    just_inside = 30 - (1 / 86400)
    make_case(
        conn, "edge_ok", state=CaseState.VERIFIED_RECOVERED,
        settled_days_ago=just_inside,
    )
    make_case(conn, "edge_bad", state=CaseState.VERIFIED_FAILED, settled_days_ago=1)
    assert resolve_conflicting_history(conn, subject) is True

    just_outside = 30 + (1 / 86400)
    conn.execute(
        "UPDATE recovery_cases SET updated_at = now() - (%s || ' days')::interval"
        " WHERE id = (SELECT c.id FROM recovery_cases c"
        "               JOIN financial_obligations o ON o.id = c.obligation_id"
        "              WHERE o.anchor_canonical = 'order:ord_ch_edge_ok')",
        (str(just_outside),),
    )
    assert resolve_conflicting_history(conn, subject) is False


def test_a_narrower_window_can_be_requested(
    conn: psycopg.Connection, subject: int
) -> None:
    """The 30-day default is the policy contract; the parameter exists so the
    window is stated at the call site rather than hidden in the query."""
    make_case(conn, "win_ok", state=CaseState.VERIFIED_RECOVERED, settled_days_ago=10)
    make_case(conn, "win_bad", state=CaseState.VERIFIED_FAILED, settled_days_ago=10)

    assert resolve_conflicting_history(conn, subject) is True
    assert resolve_conflicting_history(conn, subject, window_days=7) is False


def test_the_default_window_is_thirty_days() -> None:
    """Pinned as a literal so editing the default cannot move a test with it."""
    import inspect

    default = inspect.signature(resolve_conflicting_history).parameters["window_days"]
    assert default.default == 30
