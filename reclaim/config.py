from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / "config" / "policy.yaml"
OPERATIONAL_PATH = ROOT / "config" / "operational.yaml"

_DEFAULTS = {
    "max_attempts": 2,
    "ttl_budget_ms": 72 * 60 * 60 * 1000,
}

_POLICY_INT_KEYS = frozenset({"max_attempts", "ttl_budget_ms"})

_OPERATIONAL_DEFAULTS: dict[str, int | str] = {
    "provider_http_timeout_seconds": 30,
    "provider_base_url": "api.razorpay.com",
    "provider_connect_timeout_seconds": 5,
    "provider_create_link_timeout_seconds": 15,
    "provider_fetch_timeout_seconds": 10,
    "payment_link_ttl_seconds": 3600,
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
    "execution": 60,
    "reconciliation": 45,
    "verification": 45,
}


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


def load_policy(path: Path | None = None) -> dict[str, int]:
    target = path or POLICY_PATH
    values = dict(_DEFAULTS)
    if not target.exists():
        return values

    for key, raw in _scalar_lines(target):
        if key in _POLICY_INT_KEYS:
            values[key] = int(raw)
    return values


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
