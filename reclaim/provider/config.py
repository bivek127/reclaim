"""Provider credentials and per-operation timeouts.

Secrets come from the environment and are never logged, never placed in a
recorded request body, and never rendered by repr().
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from reclaim.config import load_operational

REDACTED = "<redacted>"

TEST_KEY_PREFIX = "rzp_test_"
LIVE_KEY_PREFIX = "rzp_live_"

# Razorpay rejects expire_by under 15 minutes ahead, and over 6 months ahead.
MIN_LINK_TTL_SECONDS = 900
MAX_LINK_TTL_SECONDS = 6 * 30 * 24 * 60 * 60


class ProviderConfigError(Exception):
    """Configuration is missing or internally inconsistent. Never carries a secret."""


@dataclass(frozen=True)
class ProviderConfig:
    key_id: str
    key_secret: str
    base_url: str
    connect_timeout_seconds: int
    create_link_timeout_seconds: int
    fetch_timeout_seconds: int
    payment_link_ttl_seconds: int
    webhook_secret: str | None = None

    def __post_init__(self) -> None:
        if not self.key_id:
            raise ProviderConfigError("RAZORPAY_KEY_ID is not set")
        if not self.key_secret:
            raise ProviderConfigError("RAZORPAY_KEY_SECRET is not set")
        if not self.base_url:
            raise ProviderConfigError("provider_base_url is not set")

        ceiling = int(load_operational()["provider_http_timeout_seconds"])
        for name, value in (
            ("provider_connect_timeout_seconds", self.connect_timeout_seconds),
            ("provider_create_link_timeout_seconds", self.create_link_timeout_seconds),
            ("provider_fetch_timeout_seconds", self.fetch_timeout_seconds),
        ):
            if value <= 0:
                raise ProviderConfigError(f"{name} must be positive")
            if value > ceiling:
                raise ProviderConfigError(
                    f"{name}={value} exceeds provider_http_timeout_seconds={ceiling}; "
                    "the §4.1 execution-lease relation would no longer hold"
                )

        if self.payment_link_ttl_seconds < MIN_LINK_TTL_SECONDS:
            raise ProviderConfigError(
                f"payment_link_ttl_seconds must be >= {MIN_LINK_TTL_SECONDS} "
                "(the provider rejects a nearer expire_by)"
            )
        if self.payment_link_ttl_seconds > MAX_LINK_TTL_SECONDS:
            raise ProviderConfigError(
                f"payment_link_ttl_seconds must be <= {MAX_LINK_TTL_SECONDS}"
            )

    @property
    def is_test_mode(self) -> bool:
        return self.key_id.startswith(TEST_KEY_PREFIX)

    def require_test_mode(self) -> None:
        """Contract tests call this. A live key must fail loudly, never silently run."""
        if not self.is_test_mode:
            raise ProviderConfigError(
                f"refusing to run against a key that is not {TEST_KEY_PREFIX}*"
            )

    def __repr__(self) -> str:
        return (
            f"ProviderConfig(key_id={self._masked_key_id()!r}, "
            f"key_secret={REDACTED}, base_url={self.base_url!r}, "
            f"connect_timeout_seconds={self.connect_timeout_seconds}, "
            f"create_link_timeout_seconds={self.create_link_timeout_seconds}, "
            f"fetch_timeout_seconds={self.fetch_timeout_seconds}, "
            f"payment_link_ttl_seconds={self.payment_link_ttl_seconds}, "
            f"webhook_secret={REDACTED})"
        )

    __str__ = __repr__

    def _masked_key_id(self) -> str:
        if self.key_id.startswith(TEST_KEY_PREFIX):
            return f"{TEST_KEY_PREFIX}***"
        if self.key_id.startswith(LIVE_KEY_PREFIX):
            return f"{LIVE_KEY_PREFIX}***"
        return "***"


def load_provider_config(
    path: Path | None = None,
    env: dict[str, str] | None = None,
) -> ProviderConfig:
    source = os.environ if env is None else env
    operational = load_operational(path)
    return ProviderConfig(
        key_id=source.get("RAZORPAY_KEY_ID", ""),
        key_secret=source.get("RAZORPAY_KEY_SECRET", ""),
        webhook_secret=source.get("RAZORPAY_WEBHOOK_SECRET") or None,
        base_url=str(operational["provider_base_url"]),
        connect_timeout_seconds=int(operational["provider_connect_timeout_seconds"]),
        create_link_timeout_seconds=int(
            operational["provider_create_link_timeout_seconds"]
        ),
        fetch_timeout_seconds=int(operational["provider_fetch_timeout_seconds"]),
        payment_link_ttl_seconds=int(operational["payment_link_ttl_seconds"]),
    )
