"""Read-model projections over real rows built by real domain operations."""

from __future__ import annotations

import json

import psycopg
import pytest

from reclaim import readmodel
from reclaim.domain.states import CASE_STATES, CaseState
from tests.domain.review_helpers import seed_escalated
from tests.domain.verification_helpers import seed_awaiting_customer


def test_empty_database_reports_zeroes_not_errors(conn: psycopg.Connection) -> None:
    page = readmodel.list_cases(conn)
    assert page.total == 0 and page.rows == ()
    o = readmodel.overview(conn)
    assert o.attention_total == 0
    assert o.recovered_amount_minor == 0
    assert o.state_counts[CaseState.ESCALATED.value] == 0


def test_every_case_state_has_a_count_key(conn: psycopg.Connection) -> None:
    """The console renders the full vocabulary, so every state must be present."""
    counts = readmodel.overview(conn).state_counts
    assert set(counts) == {s.value for s in CASE_STATES}


def test_case_row_carries_money_as_stored_minor_units(
    conn: psycopg.Connection,
) -> None:
    seed_awaiting_customer(conn, amount_minor=425000)
    row = readmodel.list_cases(conn).rows[0]
    assert row.amount_minor == 425000
    assert isinstance(row.amount_minor, int)
    assert row.currency == "INR"


def test_state_filter_and_attention_filter(conn: psycopg.Connection) -> None:
    seed_awaiting_customer(conn, suffix="a")
    seed_escalated(conn)
    awaiting = readmodel.list_cases(conn, states=(CaseState.AWAITING_CUSTOMER.value,))
    assert awaiting.total == 1
    attention = readmodel.list_cases(conn, needs_attention=True)
    assert attention.total == 1
    assert attention.rows[0].state == CaseState.ESCALATED.value


def test_unknown_state_filter_is_ignored_not_fatal(conn: psycopg.Connection) -> None:
    seed_awaiting_customer(conn, suffix="b")
    assert readmodel.list_cases(conn, states=("NOT_A_STATE",)).total == 1


def test_search_matches_customer_and_provider_reference(
    conn: psycopg.Connection,
) -> None:
    ids = seed_awaiting_customer(conn, suffix="findme")
    customer = conn.execute(
        "SELECT o.customer_ref FROM financial_obligations o "
        "JOIN recovery_cases c ON c.obligation_id = o.id WHERE c.id = %s",
        (ids["case_id"],),
    ).fetchone()[0]
    assert readmodel.list_cases(conn, query=customer).total == 1
    assert readmodel.list_cases(conn, query=ids["reference"]).total == 1
    assert readmodel.list_cases(conn, query="no-such-thing").total == 0


def test_pending_review_flag_and_review_queue(conn: psycopg.Connection) -> None:
    seed_escalated(conn)
    row = readmodel.list_cases(conn, has_pending_review=True).rows[0]
    assert row.has_pending_review is True
    rows, total = readmodel.list_reviews(conn, status="PENDING")
    assert total == 1
    assert rows[0]["case_id"] == row.case_id
    assert rows[0]["amount_minor"] == row.amount_minor


def test_pagination_bounds_are_enforced(conn: psycopg.Connection) -> None:
    for i in range(5):
        seed_awaiting_customer(conn, suffix=f"p{i}")
    page = readmodel.list_cases(conn, limit=2, offset=0)
    assert len(page.rows) == 2 and page.total == 5
    assert readmodel.list_cases(conn, limit=9999).limit == 200
    assert readmodel.list_cases(conn, limit=0).limit == 1


def test_get_case_returns_none_for_unknown_case(conn: psycopg.Connection) -> None:
    assert readmodel.get_case(conn, 987654) is None


def test_get_case_assembles_every_investigation_section(
    conn: psycopg.Connection,
) -> None:
    ids = seed_escalated(conn)
    detail = readmodel.get_case(conn, ids["case_id"])
    assert detail is not None
    assert detail.case.state == CaseState.ESCALATED.value
    assert detail.obligation["amount_minor"] == detail.case.amount_minor
    assert len(detail.policy_decisions) >= 1
    assert len(detail.reviews) == 1


def test_recovered_total_counts_only_verified_recovered(
    conn: psycopg.Connection,
) -> None:
    """Revenue reporting must not drift from the state that authorises it."""
    seed_escalated(conn)
    assert readmodel.overview(conn).recovered_amount_minor == 0


def test_system_status_reports_breaker_and_lease_health(
    conn: psycopg.Connection,
) -> None:
    status = readmodel.system_status(conn)
    assert status["breaker"]["state"] in {"CLOSED", "OPEN"}
    for key in ("leases_held", "leases_expired", "open_actions",
                "unresolved_attempts", "stale_writes_rejected"):
        assert isinstance(status[key], int)


def _insert_unmappable(
    conn: psycopg.Connection,
    *,
    provider_event_id: str,
    event_type: str = "subscription.pending",
    payload: dict | None = None,
) -> int:
    row = conn.execute(
        """
        INSERT INTO webhook_events (
            provider_event_id, event_type, signature_valid,
            resolution, anchor_canonical, payload
        ) VALUES (%s, %s, true, 'UNMAPPABLE', NULL, %s::jsonb)
        RETURNING id
        """,
        (provider_event_id, event_type, json.dumps(payload or {"entity": "event"})),
    ).fetchone()
    assert row is not None
    return row[0]


def test_empty_unmappable_queue_reports_zero(conn: psycopg.Connection) -> None:
    rows, total = readmodel.list_unmappable_webhooks(conn)
    assert rows == () and total == 0


def test_unmappable_webhooks_are_listed_oldest_first(
    conn: psycopg.Connection,
) -> None:
    """A webhook that has been sitting unresolved longest is triaged first."""
    conn.execute(
        "INSERT INTO webhook_events (provider_event_id, event_type, "
        "signature_valid, resolution, payload, received_at) "
        "VALUES ('evt-old', 'subscription.pending', true, 'UNMAPPABLE', "
        "'{}'::jsonb, now() - interval '1 hour')"
    )
    conn.execute(
        "INSERT INTO webhook_events (provider_event_id, event_type, "
        "signature_valid, resolution, payload, received_at) "
        "VALUES ('evt-new', 'subscription.pending', true, 'UNMAPPABLE', "
        "'{}'::jsonb, now())"
    )
    rows, total = readmodel.list_unmappable_webhooks(conn)
    assert total == 2
    assert [r["provider_event_id"] for r in rows] == ["evt-old", "evt-new"]


def test_unmappable_webhooks_carry_the_full_stored_payload(
    conn: psycopg.Connection,
) -> None:
    """The raw payload is what lets an operator judge the correct anchor."""
    payload = {"event": "subscription.pending", "payload": {"subscription": {}}}
    _insert_unmappable(conn, provider_event_id="evt-payload", payload=payload)
    rows, _ = readmodel.list_unmappable_webhooks(conn)
    assert rows[0]["payload"] == payload
    assert rows[0]["event_type"] == "subscription.pending"


def test_resolved_and_ignored_webhooks_do_not_appear(
    conn: psycopg.Connection,
) -> None:
    """Only the resolution this queue exists for is ever returned."""
    conn.execute(
        "INSERT INTO webhook_events (provider_event_id, event_type, "
        "signature_valid, resolution, payload) "
        "VALUES ('evt-ignored', 'refund.created', true, 'IGNORED', '{}'::jsonb)"
    )
    conn.execute(
        "INSERT INTO webhook_events (provider_event_id, event_type, "
        "signature_valid, resolution, payload) "
        "VALUES ('evt-malformed', 'unknown', true, 'MALFORMED', '{}'::jsonb)"
    )
    rows, total = readmodel.list_unmappable_webhooks(conn)
    assert rows == () and total == 0


def test_unmappable_pagination_bounds_are_enforced(
    conn: psycopg.Connection,
) -> None:
    for i in range(5):
        _insert_unmappable(conn, provider_event_id=f"evt-page-{i}")
    page_rows, total = readmodel.list_unmappable_webhooks(conn, limit=2, offset=0)
    assert len(page_rows) == 2 and total == 5
    assert len(readmodel.list_unmappable_webhooks(conn, limit=9999)[0]) == 5
    assert len(readmodel.list_unmappable_webhooks(conn, limit=0)[0]) == 1
