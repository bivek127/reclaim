"""Unit tests for obligation-anchor resolution."""

from __future__ import annotations

from reclaim.domain.anchors import AnchorKind, EventResolution, resolve_event


def test_payment_failed_resolves_to_order() -> None:
    resolved = resolve_event(
        "payment.failed",
        {
            "payment": {
                "entity": {
                    "order_id": "order_abc",
                    "amount": 10_000,
                    "currency": "INR",
                    "customer_id": "cust_1",
                    "id": "pay_should_not_be_anchor",
                }
            }
        },
    )
    assert resolved.creates_case is True
    assert resolved.resolution is EventResolution.RESOLVED
    assert resolved.anchor is not None
    assert resolved.anchor.kind is AnchorKind.ORDER
    assert resolved.anchor.key == "order_abc"
    assert resolved.anchor.canonical == "order:order_abc"


def test_payment_captured_is_order_but_does_not_create_case() -> None:
    resolved = resolve_event(
        "payment.captured",
        {
            "payment": {
                "entity": {
                    "order_id": "order_abc",
                    "amount": 10_000,
                    "currency": "INR",
                    "customer_id": "cust_1",
                }
            }
        },
    )
    assert resolved.creates_case is False
    assert resolved.resolution is EventResolution.IGNORED
    assert resolved.anchor is not None
    assert resolved.anchor.canonical == "order:order_abc"


def test_subscription_charge_failed_uses_subscription_cycle() -> None:
    resolved = resolve_event(
        "subscription.charge.failed",
        {
            "subscription": {
                "entity": {
                    "id": "sub_123",
                    "current_start": 1_700_000_000,
                }
            },
            "payment": {
                "entity": {
                    "order_id": "order_must_not_be_used",
                    "id": "pay_must_not_be_used",
                    "amount": 5_000,
                    "currency": "INR",
                    "customer_id": "cust_2",
                }
            },
        },
    )
    assert resolved.creates_case is True
    assert resolved.resolution is EventResolution.RESOLVED
    assert resolved.anchor is not None
    assert resolved.anchor.kind is AnchorKind.SUBSCRIPTION_CYCLE
    assert resolved.anchor.canonical == "subcycle:sub_123:1700000000"
    assert "order_must_not_be_used" not in resolved.anchor.canonical
    assert "pay_must_not_be_used" not in resolved.anchor.canonical


def test_payment_link_never_creates_a_case() -> None:
    resolved = resolve_event(
        "payment_link.paid",
        {
            "payment_link": {
                "entity": {
                    "reference_id": "attempt-key-1",
                    "order_id": "order_abc",
                }
            }
        },
    )
    assert resolved.creates_case is False
    assert resolved.resolution is EventResolution.IGNORED
    assert resolved.anchor is None


def test_missing_order_id_is_never_guessed() -> None:
    resolved = resolve_event(
        "payment.failed",
        {
            "payment": {
                "entity": {
                    "id": "pay_only",
                    "amount": 10_000,
                    "currency": "INR",
                    "customer_id": "cust_1",
                }
            }
        },
    )
    assert resolved.creates_case is False
    assert resolved.resolution is EventResolution.UNMAPPABLE
    assert resolved.anchor is None


def test_subscription_missing_cycle_ref_is_unmappable() -> None:
    resolved = resolve_event(
        "subscription.charge.failed",
        {
            "subscription": {"entity": {"id": "sub_123"}},
            "payment": {
                "entity": {
                    "order_id": "order_abc",
                    "amount": 5_000,
                    "currency": "INR",
                    "customer_id": "cust_2",
                }
            },
        },
    )
    assert resolved.resolution is EventResolution.UNMAPPABLE
    assert resolved.anchor is None
