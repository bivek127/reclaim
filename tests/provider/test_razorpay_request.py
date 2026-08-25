"""Request construction for create_payment_link."""

from __future__ import annotations

import time

import pytest

from reclaim.provider.contract import Customer, ProviderRequestInvalid
from tests.provider.conftest import json_response, link_payload

REFERENCE = "rcv_ABCDEFGHIJKLMNOPQRSTUVWXYZ"
CUSTOMER = Customer(name="A Payer", email="payer@example.com", contact="+919000000000")


def _create(adapter, **overrides):
    kwargs = {
        "reference_id": REFERENCE,
        "amount_minor": 10_000,
        "currency": "INR",
        "customer": CUSTOMER,
    }
    kwargs.update(overrides)
    return adapter.create_payment_link(**kwargs)


def test_posts_to_payment_links_endpoint(make_adapter) -> None:
    adapter, transport = make_adapter(json_response(200, link_payload()))

    _create(adapter)

    assert transport.calls[0]["method"] == "POST"
    assert transport.calls[0]["path"] == "/v1/payment_links"


def test_reference_id_is_sent_verbatim_as_the_idempotency_key(make_adapter) -> None:
    adapter, transport = make_adapter(json_response(200, link_payload()))

    result = _create(adapter)

    assert transport.calls[0]["body"]["reference_id"] == REFERENCE
    assert result.provider_reference == REFERENCE


def test_body_carries_amount_currency_and_customer(make_adapter) -> None:
    adapter, transport = make_adapter(json_response(200, link_payload()))

    _create(adapter)

    body = transport.calls[0]["body"]
    assert body["amount"] == 10_000
    assert body["currency"] == "INR"
    assert body["customer"] == {
        "name": "A Payer",
        "email": "payer@example.com",
        "contact": "+919000000000",
    }


def test_expire_by_defaults_to_now_plus_configured_ttl(make_adapter) -> None:
    adapter, transport = make_adapter(json_response(200, link_payload()))
    before = int(time.time())

    _create(adapter)

    expire_by = transport.calls[0]["body"]["expire_by"]
    assert before + 3600 <= expire_by <= int(time.time()) + 3600


def test_explicit_expire_by_is_respected(make_adapter) -> None:
    adapter, transport = make_adapter(json_response(200, link_payload()))

    _create(adapter, expire_by=1_900_000_000)

    assert transport.calls[0]["body"]["expire_by"] == 1_900_000_000


def test_per_operation_timeouts_come_from_config(make_adapter) -> None:
    adapter, transport = make_adapter(json_response(200, link_payload()))

    _create(adapter)

    assert transport.calls[0]["read_timeout"] == 15
    assert transport.calls[0]["connect_timeout"] == 5


def test_fetch_uses_its_own_shorter_timeout(make_adapter) -> None:
    adapter, transport = make_adapter(json_response(200, {"payment_links": []}))

    adapter.fetch_by_reference(reference_id=REFERENCE)

    assert transport.calls[0]["read_timeout"] == 10


def test_fetch_filters_by_reference_id(make_adapter) -> None:
    adapter, transport = make_adapter(json_response(200, {"payment_links": []}))

    adapter.fetch_by_reference(reference_id=REFERENCE)

    assert transport.calls[0]["method"] == "GET"
    assert transport.calls[0]["path"] == f"/v1/payment_links?reference_id={REFERENCE}"


def test_authorization_header_is_present_but_never_in_the_recorded_body(make_adapter) -> None:
    adapter, transport = make_adapter(json_response(200, link_payload()))

    result = _create(adapter)

    assert transport.calls[0]["headers"]["Authorization"].startswith("Basic ")
    assert "Authorization" not in str(result.request.body)
    assert "not-a-real-secret" not in str(result.request)


def test_reference_longer_than_the_provider_limit_raises(make_adapter) -> None:
    adapter, _ = make_adapter()

    with pytest.raises(ProviderRequestInvalid):
        _create(adapter, reference_id="r" * 41)


def test_empty_reference_raises(make_adapter) -> None:
    adapter, _ = make_adapter()

    with pytest.raises(ProviderRequestInvalid):
        _create(adapter, reference_id="")


@pytest.mark.parametrize("amount", [0, -1])
def test_non_positive_amount_raises(make_adapter, amount: int) -> None:
    adapter, _ = make_adapter()

    with pytest.raises(ProviderRequestInvalid):
        _create(adapter, amount_minor=amount)


def test_lowercase_currency_raises(make_adapter) -> None:
    adapter, _ = make_adapter()

    with pytest.raises(ProviderRequestInvalid):
        _create(adapter, currency="inr")


def test_customer_without_any_contact_channel_raises(make_adapter) -> None:
    adapter, _ = make_adapter()

    with pytest.raises(ProviderRequestInvalid):
        _create(adapter, customer=Customer(name="No Channel"))


def test_invalid_request_makes_no_network_call(make_adapter) -> None:
    adapter, transport = make_adapter()

    with pytest.raises(ProviderRequestInvalid):
        _create(adapter, amount_minor=0)

    assert transport.calls == []
