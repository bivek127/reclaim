"""Constraint tests for financial_obligations."""

from __future__ import annotations

import psycopg
import pytest
from psycopg.errors import CheckViolation, UniqueViolation

from tests.db.helpers import insert_obligation


def test_duplicate_obligation_anchor_rejected(conn: psycopg.Connection) -> None:
    insert_obligation(conn, anchor_canonical="order:dup")
    with pytest.raises(UniqueViolation):
        insert_obligation(conn, anchor_key="other", anchor_canonical="order:dup")


def test_non_positive_amount_rejected(conn: psycopg.Connection) -> None:
    with pytest.raises(CheckViolation):
        insert_obligation(conn, amount_minor=0)


def test_negative_amount_rejected(conn: psycopg.Connection) -> None:
    with pytest.raises(CheckViolation):
        insert_obligation(conn, amount_minor=-1)


def test_lowercase_currency_rejected(conn: psycopg.Connection) -> None:
    with pytest.raises(CheckViolation):
        insert_obligation(conn, currency="inr")


def test_invalid_order_anchor_shape_rejected(conn: psycopg.Connection) -> None:
    with pytest.raises(CheckViolation):
        insert_obligation(
            conn,
            anchor_kind="ORDER",
            anchor_key="x",
            anchor_canonical="subcycle:x:y",
        )


def test_invalid_subscription_anchor_shape_rejected(conn: psycopg.Connection) -> None:
    with pytest.raises(CheckViolation):
        insert_obligation(
            conn,
            anchor_kind="SUBSCRIPTION_CYCLE",
            anchor_key="sub:1",
            anchor_canonical="order:sub:1",
        )


def test_financial_fields_are_immutable(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    with pytest.raises(psycopg.Error, match="financial fields are immutable"):
        conn.execute(
            "UPDATE financial_obligations SET amount_minor = 20000 WHERE id = %s",
            (obligation_id,),
        )

    with pytest.raises(psycopg.Error, match="financial fields are immutable"):
        conn.execute(
            "UPDATE financial_obligations SET currency = 'USD' WHERE id = %s",
            (obligation_id,),
        )

    with pytest.raises(psycopg.Error, match="financial fields are immutable"):
        conn.execute(
            "UPDATE financial_obligations SET customer_ref = 'other' WHERE id = %s",
            (obligation_id,),
        )

    with pytest.raises(psycopg.Error, match="financial fields are immutable"):
        conn.execute(
            "UPDATE financial_obligations SET anchor_canonical = 'order:other' WHERE id = %s",
            (obligation_id,),
        )
