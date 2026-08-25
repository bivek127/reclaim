"""RETRY_CHARGE has no safe provider implementation (ADR-005)."""

from __future__ import annotations

import pytest

from reclaim.provider.contract import RetryChargeUnsupported


def test_retry_charge_raises(make_adapter) -> None:
    adapter, _ = make_adapter()

    with pytest.raises(RetryChargeUnsupported):
        adapter.retry_charge()


def test_retry_charge_raises_whatever_it_is_handed(make_adapter) -> None:
    adapter, _ = make_adapter()

    with pytest.raises(RetryChargeUnsupported):
        adapter.retry_charge(reference_id="rcv_anything", amount_minor=10_000)


def test_retry_charge_makes_no_network_call(make_adapter) -> None:
    adapter, transport = make_adapter()

    with pytest.raises(RetryChargeUnsupported):
        adapter.retry_charge()

    assert transport.calls == []


def test_the_raise_cites_the_open_verification(make_adapter) -> None:
    adapter, _ = make_adapter()

    with pytest.raises(RetryChargeUnsupported) as excinfo:
        adapter.retry_charge()

    assert "19.1a" in str(excinfo.value)


def test_adapter_exposes_no_other_money_moving_operation(make_adapter) -> None:
    """create_payment_link is the only operation that may move money."""
    adapter, _ = make_adapter()

    public = {name for name in dir(adapter) if not name.startswith("_")}

    assert public == {
        "config",
        "create_payment_link",
        "fetch_by_reference",
        "retry_charge",
        "verify_webhook_signature",
    }
