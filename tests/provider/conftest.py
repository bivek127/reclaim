"""Fixtures for the provider adapter suite. Deterministic tests need no network."""

from __future__ import annotations

import json
from typing import Any, Iterator

import pytest

from reclaim.provider.config import ProviderConfig
from reclaim.provider.razorpay import RazorpayAdapter
from reclaim.provider.transport import HttpResponse, TransportFailure

FAKE_KEY_ID = "rzp_test_deterministic"
FAKE_KEY_SECRET = "not-a-real-secret"


class StubTransport:
    """Returns canned responses in order, or raises a canned TransportFailure."""

    def __init__(self, *responses: HttpResponse | TransportFailure) -> None:
        self._queue = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
        connect_timeout: float,
        read_timeout: float,
    ) -> HttpResponse:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "headers": headers,
                "body": json.loads(body.decode("utf-8")) if body else None,
                "connect_timeout": connect_timeout,
                "read_timeout": read_timeout,
            }
        )
        if not self._queue:
            raise AssertionError(f"unexpected extra request: {method} {path}")
        nxt = self._queue.pop(0)
        if isinstance(nxt, TransportFailure):
            raise nxt
        return nxt


def json_response(status: int, payload: Any) -> HttpResponse:
    return HttpResponse(
        status=status,
        body=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def raw_response(status: int, body: bytes) -> HttpResponse:
    return HttpResponse(status=status, body=body, headers={})


def link_payload(
    *,
    link_id: str = "plink_TestLink000001",
    reference_id: str = "rcv_ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    status: str = "created",
    amount: int = 10_000,
    amount_paid: int = 0,
    expire_by: int = 1_800_000_000,
) -> dict[str, Any]:
    return {
        "id": link_id,
        "reference_id": reference_id,
        "status": status,
        "amount": amount,
        "amount_paid": amount_paid,
        "currency": "INR",
        "expire_by": expire_by,
        "short_url": "https://rzp.io/i/testlink",
    }


def error_payload(
    code: str = "BAD_REQUEST_ERROR", description: str = "something went wrong"
) -> dict[str, Any]:
    return {"error": {"code": code, "description": description, "source": "business"}}


@pytest.fixture
def provider_config() -> ProviderConfig:
    return ProviderConfig(
        key_id=FAKE_KEY_ID,
        key_secret=FAKE_KEY_SECRET,
        webhook_secret="webhook-secret",
        base_url="api.razorpay.com",
        connect_timeout_seconds=5,
        create_link_timeout_seconds=15,
        fetch_timeout_seconds=10,
        payment_link_ttl_seconds=3600,
    )


@pytest.fixture
def make_adapter(provider_config: ProviderConfig) -> Iterator[Any]:
    def factory(*responses: HttpResponse | TransportFailure) -> tuple[RazorpayAdapter, StubTransport]:
        transport = StubTransport(*responses)
        return RazorpayAdapter(provider_config, transport=transport), transport

    yield factory
