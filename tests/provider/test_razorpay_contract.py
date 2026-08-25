"""Contract tests against REAL Razorpay TEST MODE.

These are the only tests in the repository that leave the machine. They are
skipped when RAZORPAY_KEY_ID is absent, and they refuse to run against a key
that is not rzp_test_*. Observations are written to a findings file so ADRs
about provider behavior can cite real evidence rather than documentation.

    RAZORPAY_KEY_ID=rzp_test_... RAZORPAY_KEY_SECRET=... \\
        python3 -m pytest tests/provider -v -m provider_contract
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

import pytest

from reclaim.provider.config import ProviderConfig, load_provider_config
from reclaim.provider.contract import (
    Customer,
    FetchOutcome,
    LinkStatus,
    ProviderOutcome,
)
from reclaim.provider.razorpay import RazorpayAdapter

pytestmark = pytest.mark.provider_contract

FINDINGS_PATH = Path(__file__).resolve().parents[2] / "docs" / "provider-findings.json"

CUSTOMER = Customer(
    name="Contract Test",
    email="contract-test@example.com",
    contact="+919000000000",
)


def _reference() -> str:
    """Same shape as the production idempotency key: "rcv_" + base32(uuid4())[:26]."""
    encoded = base64.b32encode(uuid.uuid4().bytes).decode("ascii").rstrip("=")
    return f"rcv_{encoded[:26]}"


@pytest.fixture(scope="module")
def contract_config() -> ProviderConfig:
    if not os.environ.get("RAZORPAY_KEY_ID"):
        pytest.skip("RAZORPAY_KEY_ID not set; provider contract tests need test-mode keys")
    config = load_provider_config()
    config.require_test_mode()
    return config


@pytest.fixture(scope="module")
def adapter(contract_config: ProviderConfig) -> RazorpayAdapter:
    return RazorpayAdapter(contract_config)


@pytest.fixture(scope="module")
def findings() -> Iterator[dict[str, Any]]:
    """Observations from the live provider, written only if any were actually made.

    A findings file produced without contacting Razorpay would be evidence of
    nothing while looking like evidence of something.
    """
    collected: dict[str, Any] = {}
    yield collected
    observed = {key: value for key, value in collected.items() if key != "recorded_at"}
    if not any(entry.get("observed") for entry in observed.values()):
        return
    collected["recorded_at"] = int(time.time())
    FINDINGS_PATH.write_text(json.dumps(collected, indent=2, sort_keys=True), encoding="utf-8")


@pytest.fixture(scope="module")
def created_link(adapter: RazorpayAdapter, findings: dict[str, Any]) -> dict[str, Any]:
    reference = _reference()
    result = adapter.create_payment_link(
        reference_id=reference,
        amount_minor=10_000,
        currency="INR",
        customer=CUSTOMER,
    )
    findings["create"] = {
        "observed": True,
        "http_status": result.http_status,
        "outcome": result.outcome.value,
        "provider_status_raw": result.provider_status_raw,
    }
    assert result.outcome is ProviderOutcome.ACCEPTED, result.response_body
    return {"reference": reference, "result": result}


# ---- P2 / P4: creation and fetch-by-reference ---------------------------


def test_create_payment_link_is_accepted(created_link: dict[str, Any]) -> None:
    result = created_link["result"]

    assert result.http_status == 200
    assert result.provider_correlation_id.startswith("plink_")
    assert result.link_status is LinkStatus.CREATED


def test_reference_id_is_echoed_back(created_link: dict[str, Any]) -> None:
    """P2: the provider accepts and retains a caller-supplied reference_id."""
    assert created_link["result"].response_body["reference_id"] == created_link["reference"]


def _await_found(adapter: RazorpayAdapter, *, reference_id: str, timeout_seconds: float = 8.0):
    """Poll fetch_by_reference until FOUND or the bound is hit.

    Live observation (this session, diagnostic script, 2026-08-26): a Payment
    Link that Razorpay has already synchronously created (confirmed by its own
    200 response) can still return NOT_FOUND from GET /v1/payment_links for
    roughly 1-3 seconds afterward before consistently resolving to FOUND. This
    is eventual consistency on the fetch/search endpoint specifically, not on
    creation itself. It is not documented and the exact bound is unverified;
    8s is a generous multiple of the observed window, not a Razorpay-stated
    guarantee. See ADR-007's "Live provider evidence" addendum.
    """
    deadline = time.monotonic() + timeout_seconds
    result = adapter.fetch_by_reference(reference_id=reference_id)
    while result.outcome is not FetchOutcome.FOUND and time.monotonic() < deadline:
        time.sleep(0.5)
        result = adapter.fetch_by_reference(reference_id=reference_id)
    return result


def test_fetch_by_reference_finds_the_link(
    adapter: RazorpayAdapter, created_link: dict[str, Any], findings: dict[str, Any]
) -> None:
    """Fetch-by-reference must actually work. The adopt path is unbuildable without it.

    Polls with a bound rather than asserting on a single immediate call: a
    single immediate fetch is exactly what exposed Razorpay's fetch-endpoint
    eventual consistency (see _await_found). The question this test asks is
    whether fetch-by-reference is *supported*, not whether it is instantaneous
    — nothing claims immediacy, so a bounded poll is the correct way to ask it,
    not a weakening of it.
    """
    result = _await_found(adapter, reference_id=created_link["reference"])

    findings["fetch_by_reference"] = {
        "observed": True,
        "http_status": result.http_status,
        "outcome": result.outcome.value,
        "supported": result.outcome is FetchOutcome.FOUND,
    }
    assert result.outcome is FetchOutcome.FOUND
    assert result.provider_correlation_id == created_link["result"].provider_correlation_id


def test_fetch_of_an_unused_reference_is_not_found(
    adapter: RazorpayAdapter, findings: dict[str, Any]
) -> None:
    result = adapter.fetch_by_reference(reference_id=_reference())

    findings["fetch_unused_reference"] = {
        "observed": True,
        "http_status": result.http_status,
        "outcome": result.outcome.value,
        "body": result.response_body,
    }
    assert result.outcome is FetchOutcome.NOT_FOUND


# ---- duplicate reference_id -----------------------------------------------


def test_duplicate_reference_is_classified_as_duplicate(
    adapter: RazorpayAdapter, created_link: dict[str, Any], findings: dict[str, Any]
) -> None:
    """Records the exact duplicate signature verbatim so ADR-007 can cite it."""
    result = adapter.create_payment_link(
        reference_id=created_link["reference"],
        amount_minor=10_000,
        currency="INR",
        customer=CUSTOMER,
    )

    findings["duplicate_reference"] = {
        "observed": True,
        "http_status": result.http_status,
        "error_code": result.error_code,
        "error_description": result.error_description,
        "response_body": result.response_body,
        "outcome": result.outcome.value,
    }

    assert result.outcome is ProviderOutcome.DUPLICATE_REFERENCE
    assert result.provider_correlation_id == created_link["result"].provider_correlation_id


def test_duplicate_create_does_not_produce_a_second_link(
    adapter: RazorpayAdapter, created_link: dict[str, Any]
) -> None:
    """The invariant the whole adopt path exists to protect."""
    result = _await_found(adapter, reference_id=created_link["reference"])

    assert result.outcome is FetchOutcome.FOUND
    assert len(result.response_body["payment_links"]) == 1


# ---- expire_by floor ----------------------------------------------------


def test_expire_by_under_fifteen_minutes_is_rejected(
    adapter: RazorpayAdapter, findings: dict[str, Any]
) -> None:
    """Documents the constraint that makes the expiry-finality observation slow."""
    result = adapter.create_payment_link(
        reference_id=_reference(),
        amount_minor=10_000,
        currency="INR",
        customer=CUSTOMER,
        expire_by=int(time.time()) + 60,
    )

    findings["expire_by_floor"] = {
        "observed": True,
        "http_status": result.http_status,
        "error_description": result.error_description,
        "outcome": result.outcome.value,
    }
    assert result.outcome is ProviderOutcome.REJECTED


# ---- expiry finality (HIGHEST CONSEQUENCE) ---------------------------------


@pytest.mark.provider_contract_slow
def test_expiry_finality_observation(
    adapter: RazorpayAdapter, findings: dict[str, Any]
) -> None:
    """Observes post-expiry state; CANNOT establish financial finality.

    Proving that an expired link can never produce a payment requires a human
    attempting payment through the checkout page after expiry, which no
    automated test can do. This records what is observable and nothing more —
    ADR-006 keeps expiry finality UNVERIFIED regardless of the outcome here.
    """
    if not os.environ.get("RECLAIM_PROVIDER_SLOW"):
        pytest.skip("needs a >=15 minute wait; set RECLAIM_PROVIDER_SLOW=1 to run")

    reference = _reference()
    expire_by = int(time.time()) + 900
    created = adapter.create_payment_link(
        reference_id=reference,
        amount_minor=10_000,
        currency="INR",
        customer=CUSTOMER,
        expire_by=expire_by,
    )
    assert created.outcome is ProviderOutcome.ACCEPTED

    time.sleep(max(0, expire_by - int(time.time())) + 60)
    after = adapter.fetch_by_reference(reference_id=reference)

    findings["expiry_observation"] = {
        "observed": True,
        "short_url": created.short_url,
        "status_after_expiry": after.provider_status_raw,
        "normalized": after.link_status.value if after.link_status else None,
        "financial_finality_established": False,
        "note": (
            "API status only. Whether the stored checkout URL can still take "
            "money after expiry was not and cannot be established automatically."
        ),
    }
    assert after.outcome is FetchOutcome.FOUND


# ---- subscription cycle identifier -----------------------------------------


def test_subscription_cycle_identifier_is_unverified(findings: dict[str, Any]) -> None:
    """Requires a test subscription driven through two consecutive failed
    cycles, plus a redelivery of one cycle's event. That cannot be provoked from
    an API-only contract test, so the safe default in ADR-004 stands (ADR-009).
    """
    findings["subscription_cycle_ref"] = {
        "observed": False,
        "verified": False,
        "safe_default": "current_start",
        "blocker": "needs two consecutive failed cycles on a live test subscription",
    }
    pytest.skip("§19.1c needs a test subscription with two consecutive failed cycles")
