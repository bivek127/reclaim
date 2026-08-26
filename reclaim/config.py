from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / "config" / "policy.yaml"
OPERATIONAL_PATH = ROOT / "config" / "operational.yaml"

_DEFAULTS = {
    "max_attempts": 2,
    "ttl_budget_ms": 72 * 60 * 60 * 1000,
    "review_ttl_ms": 24 * 60 * 60 * 1000,
    "breaker_failure_threshold": 5,
    "breaker_reset_seconds": 120,
}

_POLICY_VERSION_DEFAULT = "1.0"

_POLICY_INT_KEYS = frozenset(
    {
        "max_attempts",
        "ttl_budget_ms",
        "review_ttl_ms",
        "breaker_failure_threshold",
        "breaker_reset_seconds",
    }
)

_FORBIDDEN_TABLE_ACTIONS = frozenset({"RETRY_CHARGE"})

_OPERATIONAL_DEFAULTS: dict[str, int | str] = {
    "provider_http_timeout_seconds": 30,
    "provider_base_url": "api.razorpay.com",
    "provider_connect_timeout_seconds": 5,
    "provider_create_link_timeout_seconds": 15,
    "provider_fetch_timeout_seconds": 10,
    "payment_link_ttl_seconds": 3600,
    "reconciliation_interval_seconds": 30,
    "reconciliation_max_polls": 20,
    "reconciliation_max_posts_per_attempt": 3,
    "sweeper_batch_size": 100,
    "sweeper_interval_seconds": 15,
    "ttl_expiry_interval_seconds": 60,
}

_OPERATIONAL_INT_KEYS = frozenset(
    key for key, value in _OPERATIONAL_DEFAULTS.items() if isinstance(value, int)
)
_OPERATIONAL_STR_KEYS = frozenset(_OPERATIONAL_DEFAULTS) - _OPERATIONAL_INT_KEYS

LEASE_SECONDS = {
    "enrichment": 30,
    "diagnosis": 90,
    "policy": 90,
    "execution": 60,
    "reconciliation": 45,
    "verification": 45,
}


@dataclass(frozen=True)
class PolicyConfig:
    """The cause-to-action table plus operational ints from `config/policy.yaml`."""

    policy_version: str
    causes: dict[str, str]
    max_attempts: int
    ttl_budget_ms: int
    review_ttl_ms: int
    breaker_failure_threshold: int
    breaker_reset_seconds: int


class PolicyConfigError(ValueError):
    """Invalid or unsafe business policy configuration."""


def _scalar_lines(path: Path) -> list[tuple[str, str]]:
    """Flat `key: value` pairs. Nested blocks yield keys we simply never ask for."""
    pairs: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped or ":" not in stripped:
            continue
        key, raw = stripped.split(":", 1)
        pairs.append((key.strip(), raw.strip().strip('"').strip("'")))
    return pairs


def _parse_causes(path: Path) -> dict[str, str]:
    """Read the indented `causes:` block without a YAML dependency."""
    causes: dict[str, str] = {}
    in_causes = False
    for line in path.read_text(encoding="utf-8").splitlines():
        content = line.split("#", 1)[0].rstrip()
        if not content.strip():
            continue
        if content.strip() == "causes:":
            in_causes = True
            continue
        if in_causes:
            if line.startswith("  ") and ":" in content:
                key, raw = content.strip().split(":", 1)
                action = raw.strip().strip('"').strip("'")
                causes[key.strip()] = action
            elif not line.startswith(" "):
                break
    return causes


def _validate_causes(causes: dict[str, str]) -> dict[str, str]:
    if not causes:
        raise PolicyConfigError("policy table (causes) is missing or empty")
    for cause, action in causes.items():
        if action in _FORBIDDEN_TABLE_ACTIONS:
            raise PolicyConfigError(
                f"cause {cause!r} maps to forbidden action {action!r} (§19.1a)"
            )
    return dict(causes)


def load_policy_config(path: Path | None = None) -> PolicyConfig:
    target = path or POLICY_PATH
    ints = dict(_DEFAULTS)
    version = _POLICY_VERSION_DEFAULT
    causes: dict[str, str] = {}

    if target.exists():
        for key, raw in _scalar_lines(target):
            if key == "policy_version":
                version = raw
            elif key in _POLICY_INT_KEYS:
                ints[key] = int(raw)
        causes = _validate_causes(_parse_causes(target))
    else:
        raise PolicyConfigError(f"policy config not found: {target}")

    return PolicyConfig(
        policy_version=version,
        causes=causes,
        max_attempts=ints["max_attempts"],
        ttl_budget_ms=ints["ttl_budget_ms"],
        review_ttl_ms=ints["review_ttl_ms"],
        breaker_failure_threshold=ints["breaker_failure_threshold"],
        breaker_reset_seconds=ints["breaker_reset_seconds"],
    )


def load_policy(path: Path | None = None) -> dict[str, int]:
    """Operational ints for lifecycle and executor. Int-only for compatibility."""
    cfg = load_policy_config(path)
    return {
        "max_attempts": cfg.max_attempts,
        "ttl_budget_ms": cfg.ttl_budget_ms,
        "review_ttl_ms": cfg.review_ttl_ms,
        "breaker_failure_threshold": cfg.breaker_failure_threshold,
        "breaker_reset_seconds": cfg.breaker_reset_seconds,
    }


def load_operational(path: Path | None = None) -> dict[str, int | str]:
    target = path or OPERATIONAL_PATH
    values = dict(_OPERATIONAL_DEFAULTS)
    if not target.exists():
        return values

    for key, raw in _scalar_lines(target):
        if key in _OPERATIONAL_INT_KEYS:
            values[key] = int(raw)
        elif key in _OPERATIONAL_STR_KEYS:
            values[key] = raw
    return values


def lease_seconds_for(work: str) -> int:
    return LEASE_SECONDS[work]
