"""Pure-function obligation-anchor resolution from webhook payloads."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class AnchorKind(str, Enum):
    ORDER = "ORDER"
    SUBSCRIPTION_CYCLE = "SUBSCRIPTION_CYCLE"


class EventResolution(str, Enum):
    RESOLVED = "RESOLVED"
    IGNORED = "IGNORED"
    UNMAPPABLE = "UNMAPPABLE"
    MALFORMED = "MALFORMED"


CASE_CREATING_EVENTS = frozenset({"payment.failed", "subscription.charge.failed"})
ORDER_EVENTS = frozenset({"payment.failed", "payment.captured"})


@dataclass(frozen=True)
class Anchor:
    kind: AnchorKind
    key: str
    canonical: str


@dataclass(frozen=True)
class FinancialFacts:
    amount_minor: int
    currency: str
    customer_ref: str


@dataclass(frozen=True)
class ResolvedEvent:
    event_type: str
    resolution: EventResolution
    creates_case: bool
    anchor: Anchor | None
    facts: FinancialFacts | None


def _nested(payload: Any, *path: str) -> Any:
    current = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _entity(payload: dict[str, Any], collection: str) -> dict[str, Any] | None:
    entity = _nested(payload, collection, "entity")
    return entity if isinstance(entity, dict) else None


def order_anchor(order_id: str) -> Anchor:
    return Anchor(kind=AnchorKind.ORDER, key=order_id, canonical=f"order:{order_id}")


def subscription_cycle_anchor(subscription_id: str, billing_cycle_ref: str) -> Anchor:
    return Anchor(
        kind=AnchorKind.SUBSCRIPTION_CYCLE,
        key=f"{subscription_id}:{billing_cycle_ref}",
        canonical=f"subcycle:{subscription_id}:{billing_cycle_ref}",
    )


def _billing_cycle_ref(subscription: dict[str, Any]) -> str | None:
    """Falls back to `current_start` epoch seconds pending field verification
    against the provider's actual subscription payload shape."""
    current_start = subscription.get("current_start")
    if current_start is None:
        return None
    return str(current_start)


def _facts_from_payment(payment: dict[str, Any] | None) -> FinancialFacts | None:
    if payment is None:
        return None
    amount = payment.get("amount")
    customer_ref = payment.get("customer_id") or payment.get("email") or payment.get("contact")
    currency = payment.get("currency") or "INR"
    if not isinstance(amount, int) or amount <= 0:
        return None
    if not isinstance(customer_ref, str) or not customer_ref:
        return None
    if not isinstance(currency, str) or currency != currency.upper():
        return None
    return FinancialFacts(amount_minor=amount, currency=currency, customer_ref=customer_ref)


def resolve_event(event_type: str, payload: dict[str, Any]) -> ResolvedEvent:
    if event_type in ORDER_EVENTS:
        payment = _entity(payload, "payment")
        order_id = payment.get("order_id") if payment else None
        if not isinstance(order_id, str) or not order_id:
            return ResolvedEvent(event_type, EventResolution.UNMAPPABLE, False, None, None)
        creates_case = event_type == "payment.failed"
        resolution = EventResolution.RESOLVED if creates_case else EventResolution.IGNORED
        facts = _facts_from_payment(payment) if creates_case else None
        if creates_case and facts is None:
            return ResolvedEvent(event_type, EventResolution.UNMAPPABLE, False, None, None)
        return ResolvedEvent(
            event_type,
            resolution,
            creates_case,
            order_anchor(order_id),
            facts,
        )

    if event_type == "subscription.charge.failed":
        subscription = _entity(payload, "subscription")
        subscription_id = subscription.get("id") if subscription else None
        cycle_ref = _billing_cycle_ref(subscription) if subscription else None
        if not isinstance(subscription_id, str) or not subscription_id or cycle_ref is None:
            return ResolvedEvent(event_type, EventResolution.UNMAPPABLE, False, None, None)
        payment = _entity(payload, "payment")
        facts = _facts_from_payment(payment)
        if facts is None:
            return ResolvedEvent(event_type, EventResolution.UNMAPPABLE, False, None, None)
        return ResolvedEvent(
            event_type,
            EventResolution.RESOLVED,
            True,
            subscription_cycle_anchor(subscription_id, cycle_ref),
            facts,
        )

    if event_type.startswith("payment_link."):
        return ResolvedEvent(event_type, EventResolution.IGNORED, False, None, None)

    return ResolvedEvent(event_type, EventResolution.IGNORED, False, None, None)
