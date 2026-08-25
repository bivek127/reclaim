"""Status normalization: provider strings never reach the domain."""

from __future__ import annotations

import pytest

from reclaim.provider.contract import Customer, LinkStatus, ProviderOutcome
from reclaim.provider.razorpay import normalize_link_status
from tests.provider.conftest import json_response, link_payload, raw_response

REFERENCE = "rcv_ABCDEFGHIJKLMNOPQRSTUVWXYZ"
CUSTOMER = Customer(email="payer@example.com")


def _create(adapter):
    return adapter.create_payment_link(
        reference_id=REFERENCE,
        amount_minor=10_000,
        currency="INR",
        customer=CUSTOMER,
    )


def test_2xx_with_id_is_accepted(make_adapter) -> None:
    adapter, _ = make_adapter(json_response(200, link_payload(link_id="plink_Abc123")))

    result = _create(adapter)

    assert result.outcome is ProviderOutcome.ACCEPTED
    assert result.provider_correlation_id == "plink_Abc123"
    assert result.is_unknown is False


def test_accepted_result_carries_short_url_and_expire_by(make_adapter) -> None:
    adapter, _ = make_adapter(
        json_response(200, link_payload(expire_by=1_800_000_042))
    )

    result = _create(adapter)

    assert result.short_url == "https://rzp.io/i/testlink"
    assert result.expire_by == 1_800_000_042


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("created", LinkStatus.CREATED),
        ("partially_paid", LinkStatus.PARTIALLY_PAID),
        ("paid", LinkStatus.PAID),
        ("expired", LinkStatus.EXPIRED),
        ("cancelled", LinkStatus.CANCELLED),
    ],
)
def test_documented_statuses_normalize(make_adapter, raw: str, expected: LinkStatus) -> None:
    adapter, _ = make_adapter(json_response(200, link_payload(status=raw)))

    assert _create(adapter).link_status is expected


@pytest.mark.parametrize(
    "raw", ["issued", "attempted", "ISSUED", "", None, 7, "part_paid"]
)
def test_unrecognized_status_stays_unknown(raw: object) -> None:
    """An unknown provider state must remain unknown, never snap to a near neighbour."""
    assert normalize_link_status(raw) is LinkStatus.UNKNOWN


def test_status_casing_is_tolerated(make_adapter) -> None:
    adapter, _ = make_adapter(json_response(200, link_payload(status="PAID")))

    assert _create(adapter).link_status is LinkStatus.PAID


def test_raw_provider_status_is_preserved_for_the_audit_trail(make_adapter) -> None:
    adapter, _ = make_adapter(json_response(200, link_payload(status="issued")))

    result = _create(adapter)

    assert result.link_status is LinkStatus.UNKNOWN
    assert result.provider_status_raw == "issued"


def test_2xx_without_an_id_is_unparseable_not_accepted(make_adapter) -> None:
    adapter, _ = make_adapter(json_response(200, {"status": "created"}))

    result = _create(adapter)

    assert result.outcome is ProviderOutcome.UNPARSEABLE
    assert result.provider_correlation_id is None
    assert result.is_unknown is True


def test_2xx_with_non_json_body_is_unparseable(make_adapter) -> None:
    adapter, _ = make_adapter(raw_response(200, b"<html>maintenance</html>"))

    assert _create(adapter).outcome is ProviderOutcome.UNPARSEABLE


def test_2xx_with_a_json_array_body_is_unparseable(make_adapter) -> None:
    adapter, _ = make_adapter(json_response(200, ["unexpected"]))

    assert _create(adapter).outcome is ProviderOutcome.UNPARSEABLE


def test_expired_link_exposes_no_terminal_failure_flag(make_adapter) -> None:
    """Expiry as terminal-failure evidence is unverified (ADR-006), so it
    must not be readable as terminal."""
    adapter, _ = make_adapter(json_response(200, link_payload(status="expired")))

    result = _create(adapter)

    assert result.link_status is LinkStatus.EXPIRED
    assert not hasattr(result, "is_terminal")
    assert not hasattr(result, "terminal_failure")
