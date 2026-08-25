"""Deterministic provider error classification."""

from __future__ import annotations

import pytest

from reclaim.provider.contract import (
    UNKNOWN_OUTCOMES,
    Customer,
    ErrorClass,
    LinkStatus,
    ProviderOutcome,
)
from reclaim.provider.transport import TransportFailure, TransportPhase
from tests.provider.conftest import error_payload, json_response, link_payload

REFERENCE = "rcv_ABCDEFGHIJKLMNOPQRSTUVWXYZ"
CUSTOMER = Customer(email="payer@example.com")

NO_MATCH = {"payment_links": []}
ONE_MATCH = {"payment_links": [link_payload()]}


def _create(adapter):
    return adapter.create_payment_link(
        reference_id=REFERENCE,
        amount_minor=10_000,
        currency="INR",
        customer=CUSTOMER,
    )


# ---- duplicate reference ---------------------------------------------------


def test_400_with_a_matching_link_is_duplicate_reference(make_adapter) -> None:
    adapter, transport = make_adapter(
        json_response(400, error_payload()),
        json_response(200, ONE_MATCH),
    )

    result = _create(adapter)

    assert result.outcome is ProviderOutcome.DUPLICATE_REFERENCE
    assert result.error_class is ErrorClass.DUPLICATE_REFERENCE
    assert len(transport.calls) == 2


def test_duplicate_adopts_the_existing_links_correlation_id(make_adapter) -> None:
    adapter, _ = make_adapter(
        json_response(400, error_payload()),
        json_response(200, {"payment_links": [link_payload(link_id="plink_Existing1")]}),
    )

    result = _create(adapter)

    assert result.provider_correlation_id == "plink_Existing1"
    assert result.link_status is LinkStatus.CREATED


def test_400_with_no_matching_link_is_a_genuine_rejection(make_adapter) -> None:
    adapter, _ = make_adapter(
        json_response(400, error_payload(description="amount must be at least 100")),
        json_response(200, NO_MATCH),
    )

    result = _create(adapter)

    assert result.outcome is ProviderOutcome.REJECTED
    assert result.error_class is ErrorClass.VALIDATION
    assert result.provider_correlation_id is None


def test_400_with_an_inconclusive_corroborating_fetch_stays_unknown(make_adapter) -> None:
    """Cannot tell duplicate from rejection, so the safe direction is unknown."""
    adapter, _ = make_adapter(
        json_response(400, error_payload()),
        json_response(500, {"error": {"code": "SERVER_ERROR"}}),
    )

    result = _create(adapter)

    assert result.outcome is ProviderOutcome.UNKNOWN
    assert result.is_unknown is True


def test_duplicate_classification_does_not_depend_on_the_error_description(
    make_adapter,
) -> None:
    """Matching a guessed error string is forbidden; only the read is evidence."""
    adapter, _ = make_adapter(
        json_response(400, error_payload(description="totally unrelated wording")),
        json_response(200, ONE_MATCH),
    )

    assert _create(adapter).outcome is ProviderOutcome.DUPLICATE_REFERENCE


def test_rejection_classification_does_not_depend_on_the_error_description(
    make_adapter,
) -> None:
    adapter, _ = make_adapter(
        json_response(400, error_payload(description="reference id already exists")),
        json_response(200, NO_MATCH),
    )

    assert _create(adapter).outcome is ProviderOutcome.REJECTED


def test_corroborating_call_is_a_read_not_a_second_create(make_adapter) -> None:
    adapter, transport = make_adapter(
        json_response(400, error_payload()),
        json_response(200, ONE_MATCH),
    )

    _create(adapter)

    assert transport.calls[1]["method"] == "GET"
    assert [call["method"] for call in transport.calls].count("POST") == 1


def test_error_code_and_description_are_preserved_verbatim(make_adapter) -> None:
    adapter, _ = make_adapter(
        json_response(400, error_payload(code="BAD_REQUEST_ERROR", description="nope")),
        json_response(200, NO_MATCH),
    )

    result = _create(adapter)

    assert result.error_code == "BAD_REQUEST_ERROR"
    assert result.error_description == "nope"


# ---- statuses that never trigger corroboration --------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_is_not_a_business_rejection(make_adapter, status: int) -> None:
    """Our misconfiguration must not burn a case's attempt budget."""
    adapter, transport = make_adapter(json_response(status, error_payload()))

    result = _create(adapter)

    assert result.outcome is ProviderOutcome.AUTH_ERROR
    assert result.error_class is ErrorClass.AUTHENTICATION
    assert result.outcome is not ProviderOutcome.REJECTED
    assert len(transport.calls) == 1


def test_rate_limited_is_its_own_outcome_and_counts_as_unknown(make_adapter) -> None:
    adapter, transport = make_adapter(json_response(429, error_payload()))

    result = _create(adapter)

    assert result.outcome is ProviderOutcome.RATE_LIMITED
    assert result.outcome in UNKNOWN_OUTCOMES
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_is_a_transient_provider_error(make_adapter, status: int) -> None:
    adapter, _ = make_adapter(json_response(status, error_payload(code="SERVER_ERROR")))

    result = _create(adapter)

    assert result.outcome is ProviderOutcome.PROVIDER_ERROR
    assert result.error_class is ErrorClass.TRANSIENT_PROVIDER
    assert result.is_unknown is True


def test_non_400_4xx_corroborates_and_reports_business_rejection(make_adapter) -> None:
    adapter, _ = make_adapter(
        json_response(422, error_payload()),
        json_response(200, NO_MATCH),
    )

    result = _create(adapter)

    assert result.outcome is ProviderOutcome.REJECTED
    assert result.error_class is ErrorClass.BUSINESS_REJECTION


def test_unexpected_status_stays_unknown(make_adapter) -> None:
    adapter, _ = make_adapter(json_response(302, {}))

    assert _create(adapter).outcome is ProviderOutcome.UNKNOWN


# ---- transport -------------------------------------------------------------


def test_connect_failure_is_transport_error_not_ambiguity(make_adapter) -> None:
    """Zero bytes written: the request never reached the provider."""
    adapter, _ = make_adapter(
        TransportFailure(TransportPhase.CONNECT, timed_out=False, detail="refused")
    )

    result = _create(adapter)

    assert result.outcome is ProviderOutcome.TRANSPORT_ERROR
    assert result.error_class is ErrorClass.NETWORK
    assert result.outcome not in UNKNOWN_OUTCOMES


def test_connect_timeout_is_also_transport_error(make_adapter) -> None:
    adapter, _ = make_adapter(
        TransportFailure(TransportPhase.CONNECT, timed_out=True, detail="timed out")
    )

    result = _create(adapter)

    assert result.outcome is ProviderOutcome.TRANSPORT_ERROR
    assert result.error_class is ErrorClass.TIMEOUT


def test_read_timeout_is_unknown_because_bytes_were_written(make_adapter) -> None:
    adapter, _ = make_adapter(
        TransportFailure(TransportPhase.READ, timed_out=True, detail="timed out")
    )

    result = _create(adapter)

    assert result.outcome is ProviderOutcome.TIMEOUT
    assert result.is_unknown is True


def test_send_failure_after_connect_is_unknown(make_adapter) -> None:
    adapter, _ = make_adapter(
        TransportFailure(TransportPhase.SEND, timed_out=False, detail="reset")
    )

    result = _create(adapter)

    assert result.outcome is ProviderOutcome.UNKNOWN
    assert result.is_unknown is True


def test_transport_failure_result_still_reports_the_reference(make_adapter) -> None:
    adapter, _ = make_adapter(
        TransportFailure(TransportPhase.READ, timed_out=True, detail="timed out")
    )

    result = _create(adapter)

    assert result.provider_reference == REFERENCE
    assert result.request.operation == "create_payment_link"
