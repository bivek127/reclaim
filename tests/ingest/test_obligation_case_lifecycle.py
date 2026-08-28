"""Obligation and case ingest lifecycle, including duplicate delivery."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import psycopg

from reclaim.ingest.webhook import ingest_webhook


def _payment_failed_body(order_id: str = "order_abc", amount: int = 10_000) -> str:
    return json.dumps(
        {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_xyz",
                        "order_id": order_id,
                        "amount": amount,
                        "currency": "INR",
                        "customer_id": "cust_1",
                    }
                }
            },
        }
    )


def _counts(conn: psycopg.Connection) -> tuple[int, int, int]:
    obligations = conn.execute("SELECT count(*) FROM financial_obligations").fetchone()
    cases = conn.execute("SELECT count(*) FROM recovery_cases").fetchone()
    events = conn.execute("SELECT count(*) FROM webhook_events").fetchone()
    assert obligations is not None and cases is not None and events is not None
    return obligations[0], cases[0], events[0]


def test_duplicate_webhook_creates_one_case(conn: psycopg.Connection) -> None:
    body = _payment_failed_body()
    first = ingest_webhook(
        conn,
        signature_valid=True,
        raw_body=body,
        provider_event_id="evt_same",
    )
    second = ingest_webhook(
        conn,
        signature_valid=True,
        raw_body=body,
        provider_event_id="evt_same",
    )

    assert first.http_status == 200
    assert first.case_created is True
    assert second.http_status == 200
    assert second.duplicate_event is True
    assert second.case_created is False

    obligations, cases, events = _counts(conn)
    assert obligations == 1
    assert cases == 1
    assert events == 1

    case = conn.execute(
        "SELECT state, obligation_id FROM recovery_cases"
    ).fetchone()
    assert case is not None
    assert case[0] == "NEW"


def test_concurrent_duplicate_webhooks_create_one_case(
    conn: psycopg.Connection,
    migrated_database: str,
) -> None:
    body = _payment_failed_body()
    barrier = Barrier(2)
    errors: list[BaseException] = []

    def worker(event_id: str) -> None:
        try:
            with psycopg.connect(migrated_database) as worker_conn:
                barrier.wait(timeout=5)
                ingest_webhook(
                    worker_conn,
                    signature_valid=True,
                    raw_body=body,
                    provider_event_id=event_id,
                )
                worker_conn.commit()
        except BaseException as exc:  # noqa: BLE001 — collect any failure for the parent thread
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(worker, "evt_a"),
            pool.submit(worker, "evt_b"),
        ]
        for future in futures:
            future.result(timeout=10)

    assert errors == []

    obligations, cases, events = _counts(conn)
    assert obligations == 1
    assert cases == 1
    assert events == 2

    anchors = conn.execute(
        "SELECT DISTINCT anchor_canonical FROM financial_obligations"
    ).fetchall()
    assert anchors == [("order:order_abc",)]


def test_invalid_signature_writes_nothing(conn: psycopg.Connection) -> None:
    result = ingest_webhook(
        conn,
        signature_valid=False,
        raw_body=_payment_failed_body(),
        provider_event_id="evt_bad_sig",
    )
    assert result.http_status == 400
    assert _counts(conn) == (0, 0, 0)


def test_malformed_json_records_webhook_without_case(conn: psycopg.Connection) -> None:
    result = ingest_webhook(
        conn,
        signature_valid=True,
        raw_body="{not-json",
        provider_event_id="evt_malformed",
    )
    assert result.http_status == 200
    assert result.resolution == "MALFORMED"
    obligations, cases, events = _counts(conn)
    assert obligations == 0
    assert cases == 0
    assert events == 1
    row = conn.execute("SELECT case_id, resolution FROM webhook_events").fetchone()
    assert row == (None, "MALFORMED")


def test_payment_captured_does_not_create_case(conn: psycopg.Connection) -> None:
    body = json.dumps(
        {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "order_id": "order_abc",
                        "amount": 10_000,
                        "currency": "INR",
                        "customer_id": "cust_1",
                    }
                }
            },
        }
    )
    result = ingest_webhook(
        conn,
        signature_valid=True,
        raw_body=body,
        provider_event_id="evt_captured",
    )
    assert result.http_status == 200
    assert result.case_created is False
    assert _counts(conn) == (0, 0, 1)


def test_payment_link_does_not_create_case(conn: psycopg.Connection) -> None:
    body = json.dumps(
        {
            "event": "payment_link.paid",
            "payload": {
                "payment_link": {"entity": {"reference_id": "ref-1"}}
            },
        }
    )
    result = ingest_webhook(
        conn,
        signature_valid=True,
        raw_body=body,
        provider_event_id="evt_link",
    )
    assert result.http_status == 200
    assert result.resolution == "IGNORED"
    assert _counts(conn) == (0, 0, 1)
