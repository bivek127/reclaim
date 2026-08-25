"""Provider configuration validation and secret redaction."""

from __future__ import annotations

import pytest

from reclaim.provider.config import ProviderConfig, ProviderConfigError, load_provider_config

SECRET = "super-secret-value"

BASE = {
    "key_id": "rzp_test_abc123",
    "key_secret": SECRET,
    "base_url": "api.razorpay.com",
    "connect_timeout_seconds": 5,
    "create_link_timeout_seconds": 15,
    "fetch_timeout_seconds": 10,
    "payment_link_ttl_seconds": 3600,
}


def _config(**overrides) -> ProviderConfig:
    return ProviderConfig(**{**BASE, **overrides})


def test_valid_config_builds() -> None:
    assert _config().is_test_mode is True


@pytest.mark.parametrize("field", ["key_id", "key_secret", "base_url"])
def test_missing_required_field_raises(field: str) -> None:
    with pytest.raises(ProviderConfigError):
        _config(**{field: ""})


def test_live_key_is_rejected_by_require_test_mode() -> None:
    config = _config(key_id="rzp_live_abc123")

    assert config.is_test_mode is False
    with pytest.raises(ProviderConfigError):
        config.require_test_mode()


def test_test_key_passes_require_test_mode() -> None:
    _config().require_test_mode()


def test_link_ttl_below_the_provider_floor_is_rejected() -> None:
    with pytest.raises(ProviderConfigError) as excinfo:
        _config(payment_link_ttl_seconds=899)

    assert "900" in str(excinfo.value)


def test_link_ttl_at_the_floor_is_accepted() -> None:
    assert _config(payment_link_ttl_seconds=900).payment_link_ttl_seconds == 900


def test_link_ttl_beyond_six_months_is_rejected() -> None:
    with pytest.raises(ProviderConfigError):
        _config(payment_link_ttl_seconds=7 * 30 * 24 * 60 * 60)


def test_timeout_above_the_lease_ceiling_is_rejected() -> None:
    """The execution lease must stay >= 2x the provider HTTP timeout."""
    with pytest.raises(ProviderConfigError) as excinfo:
        _config(create_link_timeout_seconds=31)

    assert "provider_http_timeout_seconds" in str(excinfo.value)


@pytest.mark.parametrize(
    "field",
    ["connect_timeout_seconds", "create_link_timeout_seconds", "fetch_timeout_seconds"],
)
def test_non_positive_timeout_is_rejected(field: str) -> None:
    with pytest.raises(ProviderConfigError):
        _config(**{field: 0})


# ---- redaction ----------------------------------------------------------


def test_repr_does_not_leak_the_secret() -> None:
    assert SECRET not in repr(_config())


def test_str_does_not_leak_the_secret() -> None:
    assert SECRET not in str(_config())


def test_repr_masks_the_key_id() -> None:
    rendered = repr(_config())

    assert "abc123" not in rendered
    assert "rzp_test_***" in rendered


def test_validation_errors_do_not_leak_the_secret() -> None:
    with pytest.raises(ProviderConfigError) as excinfo:
        _config(payment_link_ttl_seconds=1)

    assert SECRET not in str(excinfo.value)


def test_webhook_secret_is_redacted() -> None:
    config = _config(webhook_secret="whsec-value")

    assert "whsec-value" not in repr(config)


# ---- environment loading ------------------------------------------------


def test_load_from_env_reads_the_documented_variable_names() -> None:
    config = load_provider_config(
        env={
            "RAZORPAY_KEY_ID": "rzp_test_env",
            "RAZORPAY_KEY_SECRET": "env-secret",
            "RAZORPAY_WEBHOOK_SECRET": "env-webhook",
        }
    )

    assert config.key_id == "rzp_test_env"
    assert config.key_secret == "env-secret"
    assert config.webhook_secret == "env-webhook"


def test_load_from_env_uses_operational_yaml_timeouts() -> None:
    config = load_provider_config(
        env={"RAZORPAY_KEY_ID": "rzp_test_env", "RAZORPAY_KEY_SECRET": "env-secret"}
    )

    assert config.create_link_timeout_seconds == 15
    assert config.fetch_timeout_seconds == 10


def test_load_from_env_without_credentials_raises() -> None:
    with pytest.raises(ProviderConfigError):
        load_provider_config(env={})


def test_absent_webhook_secret_becomes_none() -> None:
    config = load_provider_config(
        env={"RAZORPAY_KEY_ID": "rzp_test_env", "RAZORPAY_KEY_SECRET": "env-secret"}
    )

    assert config.webhook_secret is None
