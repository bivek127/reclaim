"""Razorpay webhook ingress.

The transport's whole job is ordering: verify the signature over the bytes as
received, refuse a delivery it cannot identify, and hand everything else to
`ingest_webhook`. What happens *after* that -- parsing, dedup, obligation and
case creation -- belongs to the ingest suite and is not re-asserted here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os

import psycopg
import pytest

from reclaim.api import main as api_main

# Reuse the existing API harness rather than standing up a second server.
from tests.api.test_endpoints import (  # noqa: F401
    ApiClient,
    api_server,
    client,
)

WEBHOOK_SECRET = "whsec_test_stage4"
SIG = "X-Razorpay-Signature"
EVENT_ID = "X-Razorpay-Event-Id"


@pytest.fixture(autouse=True)
def webhook_secret(monkeypatch):
    """The route reads the secret through the existing provider config.

    `load_provider_config` validates the whole provider configuration, so the
    outbound API credentials must also be present even though ingress never
    uses them. Test-mode placeholders; the adapter refuses live keys anyway.
    """
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_stage4")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret_stage4")
    yield


def sign(raw: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def failed_payment(order_id: str = "ord_hook1", amount: int = 25_000) -> bytes:
    return json.dumps(
        {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": f"pay_{order_id}",
                        "order_id": order_id,
                        "amount": amount,
                        "currency": "INR",
                        "customer_id": "cust_hook",
                        "error_code": "BAD_REQUEST_ERROR",
                    }
                }
            },
        }
    ).encode()


def counts(conn: psycopg.Connection) -> tuple[int, int, int]:
    def n(table: str) -> int:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    return n("webhook_events"), n("financial_obligations"), n("recovery_cases")


# ---- the happy path -------------------------------------------------------


def test_a_signed_payment_failure_reaches_the_ingest_domain(
    client: ApiClient, conn: psycopg.Connection
) -> None:
    raw = failed_payment("ord_ok")
    before = counts(conn)

    status, _ = client.post_raw(
        "/api/webhooks/razorpay",
        raw,
        {SIG: sign(raw), EVENT_ID: "evt_ok_1"},
    )

    assert status == 200
    events, obligations, cases = counts(conn)
    assert (events, obligations, cases) == (
        before[0] + 1, before[1] + 1, before[2] + 1
    ), "the domain recorded the event and opened one case"

    row = conn.execute(
        "SELECT provider_event_id, event_type, signature_valid "
        "FROM webhook_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row == ("evt_ok_1", "payment.failed", True)


def test_the_event_id_reaches_the_domain_verbatim(
    client: ApiClient, conn: psycopg.Connection
) -> None:
    """Not trimmed to something else, not hashed, not derived from the body."""
    raw = failed_payment("ord_verbatim")
    header_value = "evt_Verbatim-ID_9910"

    client.post_raw(
        "/api/webhooks/razorpay", raw, {SIG: sign(raw), EVENT_ID: header_value}
    )

    stored = conn.execute(
        "SELECT provider_event_id FROM webhook_events ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert stored == header_value


# ---- signature ------------------------------------------------------------


def test_an_invalid_signature_is_refused_and_writes_nothing(
    client: ApiClient, conn: psycopg.Connection
) -> None:
    raw = failed_payment("ord_badsig")
    before = counts(conn)

    status, _ = client.post_raw(
        "/api/webhooks/razorpay",
        raw,
        {SIG: sign(raw, "the-wrong-secret"), EVENT_ID: "evt_badsig"},
    )

    assert status == 400
    assert counts(conn) == before, "a rejected delivery leaves no trace"


def test_a_missing_signature_is_refused(
    client: ApiClient, conn: psycopg.Connection
) -> None:
    raw = failed_payment("ord_nosig")
    before = counts(conn)

    status, _ = client.post_raw("/api/webhooks/razorpay", raw, {EVENT_ID: "evt_nosig"})

    assert status == 400
    assert counts(conn) == before


def test_tampering_with_the_body_after_signing_is_detected(
    client: ApiClient, conn: psycopg.Connection
) -> None:
    """The signature covers the bytes as received, so any edit invalidates it."""
    original = failed_payment("ord_tamper", amount=25_000)
    signature = sign(original)
    tampered = original.replace(b'"amount": 25000', b'"amount": 9900000')
    assert tampered != original

    before = counts(conn)
    status, _ = client.post_raw(
        "/api/webhooks/razorpay", tampered, {SIG: signature, EVENT_ID: "evt_tamper"}
    )

    assert status == 400
    assert counts(conn) == before, "the inflated amount never reached the domain"


def test_verification_sees_the_exact_bytes_that_were_sent(
    client: ApiClient, conn: psycopg.Connection, monkeypatch
) -> None:
    """Byte-for-byte: no re-encoding, no key reordering, no whitespace change."""
    raw = b'{"event":"payment.failed",  "payload":{"payment":{"entity":{}}}  }'
    seen: list[bytes] = []
    real = api_main.verify_webhook_signature

    def spy(body, signature, secret):
        seen.append(body)
        return real(body, signature, secret)

    monkeypatch.setattr(api_main, "verify_webhook_signature", spy)
    client.post_raw(
        "/api/webhooks/razorpay", raw, {SIG: sign(raw), EVENT_ID: "evt_exact"}
    )

    assert seen == [raw]


# ---- event id -------------------------------------------------------------


@pytest.mark.parametrize(
    "headers,label",
    [({}, "absent"), ({EVENT_ID: ""}, "empty"), ({EVENT_ID: "   "}, "blank")],
    ids=["absent", "empty", "blank"],
)
def test_a_delivery_without_a_usable_event_id_is_refused(
    client: ApiClient, conn: psycopg.Connection, headers: dict, label: str
) -> None:
    """Fail closed: an unidentifiable delivery cannot be deduplicated, so it
    must not be allowed to open a case."""
    raw = failed_payment(f"ord_noid_{label}")
    before = counts(conn)

    status, _ = client.post_raw(
        "/api/webhooks/razorpay", raw, {SIG: sign(raw), **headers}
    )

    assert status == 400
    assert counts(conn) == before, "zero database writes"


def test_a_missing_event_id_is_refused_before_signature_work(
    client: ApiClient, conn: psycopg.Connection
) -> None:
    """Even a perfectly signed delivery is refused without an id."""
    raw = failed_payment("ord_noid_signed")
    before = counts(conn)

    status, _ = client.post_raw("/api/webhooks/razorpay", raw, {SIG: sign(raw)})

    assert status == 400
    assert counts(conn) == before


# ---- idempotency stays in the domain --------------------------------------


def test_a_redelivered_event_id_is_deduplicated_by_the_domain(
    client: ApiClient, conn: psycopg.Connection
) -> None:
    raw = failed_payment("ord_dup")
    headers = {SIG: sign(raw), EVENT_ID: "evt_dup_same"}

    first, _ = client.post_raw("/api/webhooks/razorpay", raw, headers)
    after_first = counts(conn)
    second, _ = client.post_raw("/api/webhooks/razorpay", raw, headers)

    assert (first, second) == (200, 200), "a duplicate is not an error"
    assert counts(conn) == after_first, "the unique index absorbed it"


def test_two_event_ids_for_one_anchor_still_reach_the_domain(
    client: ApiClient, conn: psycopg.Connection
) -> None:
    """The transport must not collapse distinct deliveries: deciding that two
    events describe one obligation is the anchor's job, not the route's."""
    raw = failed_payment("ord_two_ids")
    sig = sign(raw)

    client.post_raw("/api/webhooks/razorpay", raw, {SIG: sig, EVENT_ID: "evt_a"})
    events_after_first, obligations_after_first, cases_after_first = counts(conn)
    client.post_raw("/api/webhooks/razorpay", raw, {SIG: sig, EVENT_ID: "evt_b"})
    events, obligations, cases = counts(conn)

    assert events == events_after_first + 1, "both deliveries were recorded"
    assert obligations == obligations_after_first, "one obligation"
    assert cases == cases_after_first, "one case"


def test_a_malformed_payload_stays_the_domains_decision(
    client: ApiClient, conn: psycopg.Connection
) -> None:
    """The route does not parse, so it cannot reject on shape."""
    raw = b"{not json at all"
    before_cases = counts(conn)[2]

    status, _ = client.post_raw(
        "/api/webhooks/razorpay", raw, {SIG: sign(raw), EVENT_ID: "evt_malformed_http"}
    )

    assert status == 200, "recorded, not rejected -- the domain's choice"
    row = conn.execute(
        "SELECT resolution, event_type FROM webhook_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row == ("MALFORMED", "unknown")
    assert counts(conn)[2] == before_cases, "no case from an unparseable body"


# ---- layering -------------------------------------------------------------


def test_the_route_runs_no_sql_of_its_own() -> None:
    """Every write goes through ingest_webhook."""
    import inspect

    source = inspect.getsource(api_main.post_razorpay_webhook) + inspect.getsource(
        api_main._ingest
    )
    for forbidden in ("execute(", "INSERT", "UPDATE", "DELETE", "SELECT", "cursor("):
        assert forbidden not in source, f"the transport contains {forbidden!r}"
    assert "ingest_webhook(" in source, "every write goes through the domain"


def test_the_secret_comes_from_the_existing_provider_configuration() -> None:
    import inspect

    source = inspect.getsource(api_main.post_razorpay_webhook)
    assert "load_provider_config()" in source
    assert WEBHOOK_SECRET not in source
    assert "RAZORPAY_WEBHOOK_SECRET" not in source, "no second secret mechanism"
