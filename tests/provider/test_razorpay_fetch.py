"""fetch_by_reference. The NOT_FOUND / NO_EVIDENCE split is load-bearing."""

from __future__ import annotations

import pytest

from reclaim.provider.contract import ErrorClass, FetchOutcome, LinkStatus
from reclaim.provider.transport import TransportFailure, TransportPhase
from tests.provider.conftest import error_payload, json_response, link_payload, raw_response

REFERENCE = "rcv_ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _fetch(adapter):
    return adapter.fetch_by_reference(reference_id=REFERENCE)


def test_single_match_is_found(make_adapter) -> None:
    adapter, _ = make_adapter(
        json_response(200, {"payment_links": [link_payload(link_id="plink_Found1")]})
    )

    result = _fetch(adapter)

    assert result.outcome is FetchOutcome.FOUND
    assert result.provider_correlation_id == "plink_Found1"


def test_found_result_carries_status_and_amounts(make_adapter) -> None:
    adapter, _ = make_adapter(
        json_response(
            200,
            {"payment_links": [link_payload(status="paid", amount=10_000, amount_paid=10_000)]},
        )
    )

    result = _fetch(adapter)

    assert result.link_status is LinkStatus.PAID
    assert result.amount_minor == 10_000
    assert result.amount_paid_minor == 10_000
    assert result.currency == "INR"


def test_empty_array_is_not_found(make_adapter) -> None:
    """Positive evidence that nothing was created under this reference."""
    adapter, _ = make_adapter(json_response(200, {"payment_links": []}))

    result = _fetch(adapter)

    assert result.outcome is FetchOutcome.NOT_FOUND
    assert result.error_class is None


def test_multiple_matches_are_never_guessed_between(make_adapter) -> None:
    """reference_id is unique by contract; several matches is an anomaly, not a result."""
    adapter, _ = make_adapter(
        json_response(
            200,
            {"payment_links": [link_payload(link_id="plink_A"), link_payload(link_id="plink_B")]},
        )
    )

    result = _fetch(adapter)

    assert result.outcome is FetchOutcome.NO_EVIDENCE
    assert result.provider_correlation_id is None


@pytest.mark.parametrize("status", [500, 502, 503])
def test_5xx_is_no_evidence_and_explicitly_not_not_found(make_adapter, status: int) -> None:
    """Treating a failed query as absence would license a second dispatch."""
    adapter, _ = make_adapter(json_response(status, error_payload(code="SERVER_ERROR")))

    result = _fetch(adapter)

    assert result.outcome is FetchOutcome.NO_EVIDENCE
    assert result.outcome is not FetchOutcome.NOT_FOUND
    assert result.error_class is ErrorClass.TRANSIENT_PROVIDER


def test_read_timeout_is_no_evidence(make_adapter) -> None:
    adapter, _ = make_adapter(
        TransportFailure(TransportPhase.READ, timed_out=True, detail="timed out")
    )

    result = _fetch(adapter)

    assert result.outcome is FetchOutcome.NO_EVIDENCE
    assert result.error_class is ErrorClass.TIMEOUT


def test_connect_failure_is_no_evidence(make_adapter) -> None:
    adapter, _ = make_adapter(
        TransportFailure(TransportPhase.CONNECT, timed_out=False, detail="refused")
    )

    assert _fetch(adapter).outcome is FetchOutcome.NO_EVIDENCE


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ErrorClass.AUTHENTICATION),
        (403, ErrorClass.AUTHENTICATION),
        (429, ErrorClass.RATE_LIMIT),
        (400, ErrorClass.VALIDATION),
    ],
)
def test_4xx_is_no_evidence_with_a_specific_class(
    make_adapter, status: int, expected: ErrorClass
) -> None:
    adapter, _ = make_adapter(json_response(status, error_payload()))

    result = _fetch(adapter)

    assert result.outcome is FetchOutcome.NO_EVIDENCE
    assert result.error_class is expected


def test_malformed_2xx_body_is_no_evidence(make_adapter) -> None:
    adapter, _ = make_adapter(raw_response(200, b"not json"))

    result = _fetch(adapter)

    assert result.outcome is FetchOutcome.NO_EVIDENCE
    assert result.error_class is ErrorClass.MALFORMED_RESPONSE


def test_2xx_missing_the_payment_links_key_is_no_evidence(make_adapter) -> None:
    adapter, _ = make_adapter(json_response(200, {"items": []}))

    assert _fetch(adapter).outcome is FetchOutcome.NO_EVIDENCE


def test_match_without_an_id_is_no_evidence(make_adapter) -> None:
    adapter, _ = make_adapter(json_response(200, {"payment_links": [{"status": "paid"}]}))

    assert _fetch(adapter).outcome is FetchOutcome.NO_EVIDENCE


def test_fetch_never_issues_a_second_request(make_adapter) -> None:
    """The read must never corroborate itself into a loop."""
    adapter, transport = make_adapter(json_response(400, error_payload()))

    _fetch(adapter)

    assert len(transport.calls) == 1
