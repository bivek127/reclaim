"""Deterministic fallback: provider failure codes → closed causes."""

from __future__ import annotations

from reclaim.llm.schema import DiagnosisPayload

# Static map. Unmapped codes become UNKNOWN. Keys are normalised uppercase.
FAILURE_CODE_TO_CAUSE: dict[str, str] = {
    "INSUFFICIENT_FUNDS": "INSUFFICIENT_FUNDS",
    "BAD_REQUEST_ERROR": "UNKNOWN",
    "GATEWAY_ERROR": "NETWORK_ERROR_NPCI",
    "SERVER_ERROR": "BANK_DOWNTIME",
    "PAYMENT_DECLINED": "CARD_DECLINED_ISSUER",
    "CARD_DECLINED": "CARD_DECLINED_ISSUER",
    "EXPIRED_CARD": "EXPIRED_CARD",
    "INCORRECT_CVV": "INCORRECT_CVV",
    "AUTHENTICATION_FAILED": "AUTHENTICATION_FAILED",
    "BANK_DOWNTIME": "BANK_DOWNTIME",
    "NETWORK_ERROR": "NETWORK_ERROR_NPCI",
    "MANDATE_REVOKED": "MANDATE_REVOKED",
    "RISK_BLOCKED": "RISK_BLOCKED",
    "PAYMENT_RISK_BLOCKED": "RISK_BLOCKED",
}

FALLBACK_REASONING = "deterministic fallback from provider failure-code map (§10.3)"


def fallback_cause(failure_codes: tuple[str, ...]) -> str:
    """Most recent mapped code wins; otherwise UNKNOWN."""
    for code in reversed(failure_codes):
        mapped = FAILURE_CODE_TO_CAUSE.get(code.strip().upper())
        if mapped is not None:
            return mapped
    return "UNKNOWN"


def fallback_payload(failure_codes: tuple[str, ...]) -> DiagnosisPayload:
    cause = fallback_cause(failure_codes)
    return DiagnosisPayload(
        cause=cause,
        recommended_action="CREATE_PAYMENT_LINK",
        reasoning=FALLBACK_REASONING,
        confidence=None,
    )
