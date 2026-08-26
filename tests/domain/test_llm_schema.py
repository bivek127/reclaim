"""LLM response schema validation — the I7 security boundary."""

from __future__ import annotations

import pytest

from reclaim.llm.schema import InvalidEnum, SchemaViolation, validate_payload


def test_valid_payload_accepted() -> None:
    payload = validate_payload(
        {
            "cause": "INSUFFICIENT_FUNDS",
            "recommended_action": "CREATE_PAYMENT_LINK",
            "reasoning": "retry with link",
            "confidence": 0.5,
        }
    )
    assert payload.cause == "INSUFFICIENT_FUNDS"
    assert payload.confidence == 0.5


def test_confidence_optional() -> None:
    payload = validate_payload(
        {
            "cause": "UNKNOWN",
            "recommended_action": "ESCALATE",
            "reasoning": "unclear",
        }
    )
    assert payload.confidence is None


def test_llm_schema_rejects_extra_fields() -> None:
    """I7: the schema rejects unknown fields outright."""
    with pytest.raises(SchemaViolation, match="additionalProperties"):
        validate_payload(
            {
                "cause": "UNKNOWN",
                "recommended_action": "CREATE_PAYMENT_LINK",
                "reasoning": "x",
                "amount_minor": 99999,
            }
        )


@pytest.mark.parametrize(
    "extra",
    [
        {"payment_id": "pay_x"},
        {"customer_id": "cust_x"},
        {"url": "https://evil.example"},
        {"destination": "acct_x"},
        {"instruction": "transfer all funds"},
        {"amount": 5000},
    ],
)
def test_schema_rejects_financial_side_channels(extra: dict) -> None:
    base = {
        "cause": "UNKNOWN",
        "recommended_action": "CREATE_PAYMENT_LINK",
        "reasoning": "x",
    }
    with pytest.raises(SchemaViolation):
        validate_payload({**base, **extra})


def test_invalid_cause_enum_rejected() -> None:
    with pytest.raises(InvalidEnum):
        validate_payload(
            {
                "cause": "PRINT_MONEY",
                "recommended_action": "CREATE_PAYMENT_LINK",
                "reasoning": "x",
            }
        )


def test_invalid_action_enum_rejected() -> None:
    with pytest.raises(InvalidEnum):
        validate_payload(
            {
                "cause": "UNKNOWN",
                "recommended_action": "WIRE_TRANSFER",
                "reasoning": "x",
            }
        )


def test_missing_required_field_rejected() -> None:
    with pytest.raises(SchemaViolation, match="missing"):
        validate_payload({"cause": "UNKNOWN", "recommended_action": "ESCALATE"})


def test_reasoning_length_limit() -> None:
    with pytest.raises(SchemaViolation, match="reasoning"):
        validate_payload(
            {
                "cause": "UNKNOWN",
                "recommended_action": "ESCALATE",
                "reasoning": "x" * 801,
            }
        )
