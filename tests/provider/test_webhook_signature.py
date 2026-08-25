"""Webhook signature verification — the one VERIFIED provider assumption."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from reclaim.provider.razorpay import RazorpayAdapter, verify_webhook_signature

SECRET = "webhook-secret"
BODY = b'{"event":"payment.failed","payload":{}}'


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_valid_signature_passes() -> None:
    assert verify_webhook_signature(BODY, _sign(BODY), SECRET) is True


def test_tampered_body_fails() -> None:
    signature = _sign(BODY)

    assert verify_webhook_signature(BODY + b" ", signature, SECRET) is False


def test_wrong_secret_fails() -> None:
    assert verify_webhook_signature(BODY, _sign(BODY, "other-secret"), SECRET) is False


def test_empty_signature_fails() -> None:
    assert verify_webhook_signature(BODY, "", SECRET) is False


def test_empty_secret_fails() -> None:
    assert verify_webhook_signature(BODY, _sign(BODY), "") is False


def test_signature_is_hmac_sha256_hex_over_the_raw_body() -> None:
    """Pinned so a future refactor cannot quietly change the algorithm."""
    expected = hmac.new(SECRET.encode("utf-8"), BODY, hashlib.sha256).hexdigest()

    assert len(expected) == 64
    assert verify_webhook_signature(BODY, expected, SECRET) is True


def test_byte_for_byte_body_matters() -> None:
    """Re-serialized JSON must not verify; the raw bytes are what is signed."""
    reserialized = b'{"event": "payment.failed", "payload": {}}'

    assert verify_webhook_signature(reserialized, _sign(BODY), SECRET) is False


# ---- malformed signatures -------------------------------------------------


@pytest.mark.parametrize(
    "garbage",
    [
        "not-a-hex-string-at-all",
        "deadbeef",  # valid hex, wrong length
        "g" * 64,  # right length, not hex
        "🔥" * 64,  # non-ASCII
        " " * 64,
        "\x00" * 64,
        _sign(BODY).upper(),  # right value, wrong case
        _sign(BODY) + "0",  # right value, one char appended
        _sign(BODY)[:-1],  # right value, one char short
    ],
)
def test_malformed_signature_fails_without_raising(garbage: str) -> None:
    """A malformed header must be rejected, never crash the verifier."""
    assert verify_webhook_signature(BODY, garbage, SECRET) is False


def test_signature_with_surrounding_whitespace_fails() -> None:
    """No implicit normalization — the header is compared exactly as received."""
    assert verify_webhook_signature(BODY, f" {_sign(BODY)} ", SECRET) is False


# ---- config wiring (point 2: uses the *configured* secret) ---------------


def test_adapter_method_uses_its_configured_webhook_secret(provider_config) -> None:
    configured = provider_config.__class__(
        **{**provider_config.__dict__, "webhook_secret": SECRET}
    )
    adapter = RazorpayAdapter(configured)

    assert adapter.verify_webhook_signature(BODY, _sign(BODY)) is True


def test_adapter_method_rejects_signature_signed_with_a_different_secret(
    provider_config,
) -> None:
    configured = provider_config.__class__(
        **{**provider_config.__dict__, "webhook_secret": SECRET}
    )
    adapter = RazorpayAdapter(configured)

    assert adapter.verify_webhook_signature(BODY, _sign(BODY, "wrong-secret")) is False


def test_adapter_method_fails_closed_when_no_webhook_secret_is_configured(
    provider_config,
) -> None:
    """Unconfigured must fail closed, not be silently treated as 'verification off'."""
    unconfigured = provider_config.__class__(
        **{**provider_config.__dict__, "webhook_secret": None}
    )
    adapter = RazorpayAdapter(unconfigured)

    assert adapter.verify_webhook_signature(BODY, _sign(BODY, "")) is False


def test_adapter_method_makes_no_network_call(make_adapter) -> None:
    adapter, transport = make_adapter()

    adapter.verify_webhook_signature(BODY, "irrelevant")

    assert transport.calls == []
