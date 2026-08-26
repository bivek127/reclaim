"""Fixtures for audit-trail reconstruction tests."""

from __future__ import annotations

import itertools

import psycopg

from reclaim.domain.anchors import Anchor, AnchorKind, FinancialFacts
from reclaim.domain.lifecycle import create_obligation_and_case

_SEQ = itertools.count(1)


def ingest_case(
    conn: psycopg.Connection, *, amount_minor: int = 10_000, suffix: str | None = None
) -> tuple[int, int]:
    """A case created the way production creates one -- through lifecycle."""
    n = suffix or str(next(_SEQ))
    result = create_obligation_and_case(
        conn,
        anchor=Anchor(kind=AnchorKind.ORDER, key=f"ord_a{n}", canonical=f"order:ord_a{n}"),
        facts=FinancialFacts(
            amount_minor=amount_minor, currency="INR", customer_ref=f"cust_a{n}"
        ),
        source_event_id=f"evt_a{n}",
    )
    assert result.case_id is not None
    return result.obligation_id, result.case_id


def redeliver(conn: psycopg.Connection, suffix: str) -> tuple[int, int]:
    """Same anchor again -- the deduplication path."""
    return ingest_case(conn, suffix=suffix)
