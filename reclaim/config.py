from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / "config" / "policy.yaml"

_DEFAULTS = {
    "max_attempts": 2,
    "ttl_budget_ms": 72 * 60 * 60 * 1000,
}


def load_policy(path: Path | None = None) -> dict[str, int]:
    target = path or POLICY_PATH
    values = dict(_DEFAULTS)
    if not target.exists():
        return values

    text = target.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped or ":" not in stripped:
            continue
        key, raw = stripped.split(":", 1)
        key = key.strip()
        raw = raw.strip().strip('"').strip("'")
        if key in {"max_attempts", "ttl_budget_ms"}:
            values[key] = int(raw)
    return values
