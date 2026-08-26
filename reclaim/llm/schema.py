"""LLM response schema — the security boundary for model output."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CAUSES: frozenset[str] = frozenset(
    {
        "INSUFFICIENT_FUNDS",
        "CARD_DECLINED_ISSUER",
        "EXPIRED_CARD",
        "INCORRECT_CVV",
        "AUTHENTICATION_FAILED",
        "NETWORK_ERROR_NPCI",
        "BANK_DOWNTIME",
        "MANDATE_REVOKED",
        "RISK_BLOCKED",
        "UNKNOWN",
    }
)

ACTIONS: frozenset[str] = frozenset(
    {"RETRY_CHARGE", "CREATE_PAYMENT_LINK", "ESCALATE"}
)

REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"cause", "recommended_action", "reasoning"}
)
ALLOWED_FIELDS: frozenset[str] = REQUIRED_FIELDS | {"confidence"}

REASONING_MAX_LEN = 800


class SchemaViolation(Exception):
    """Model output failed schema validation. No retry."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class InvalidEnum(SchemaViolation):
    """Enum value outside the closed set. No retry."""


@dataclass(frozen=True)
class DiagnosisPayload:
    cause: str
    recommended_action: str
    reasoning: str
    confidence: float | None = None


def validate_payload(data: Any) -> DiagnosisPayload:
    """Validate a parsed JSON object against the diagnosis schema.

    Raises SchemaViolation/InvalidEnum."""
    if not isinstance(data, dict):
        raise SchemaViolation("response is not a JSON object")

    unknown = set(data) - ALLOWED_FIELDS
    if unknown:
        raise SchemaViolation(f"additionalProperties forbidden: {sorted(unknown)}")

    missing = REQUIRED_FIELDS - set(data)
    if missing:
        raise SchemaViolation(f"missing required fields: {sorted(missing)}")

    cause = data["cause"]
    action = data["recommended_action"]
    reasoning = data["reasoning"]

    if not isinstance(cause, str):
        raise SchemaViolation("cause must be a string")
    if not isinstance(action, str):
        raise SchemaViolation("recommended_action must be a string")
    if not isinstance(reasoning, str):
        raise SchemaViolation("reasoning must be a string")
    if len(reasoning) > REASONING_MAX_LEN:
        raise SchemaViolation(f"reasoning exceeds {REASONING_MAX_LEN} characters")

    if cause not in CAUSES:
        raise InvalidEnum(f"invalid cause: {cause!r}")
    if action not in ACTIONS:
        raise InvalidEnum(f"invalid recommended_action: {action!r}")

    confidence: float | None = None
    if "confidence" in data and data["confidence"] is not None:
        raw = data["confidence"]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise SchemaViolation("confidence must be a number")
        confidence = float(raw)
        if confidence < 0.0 or confidence > 1.0:
            raise SchemaViolation("confidence must be between 0 and 1")

    return DiagnosisPayload(
        cause=cause,
        recommended_action=action,
        reasoning=reasoning,
        confidence=confidence,
    )
