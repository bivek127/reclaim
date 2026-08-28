from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    "ollama_host": "127.0.0.1",
    "ollama_port": 11434,
    "ollama_model": "gemma3:12b",
    "ollama_timeout_seconds": 20,
}

_OPERATIONAL_INT_KEYS = frozenset(
    key for key, value in _OPERATIONAL_DEFAULTS.items() if isinstance(value, int)
)
_OPERATIONAL_STR_KEYS = frozenset(_OPERATIONAL_DEFAULTS) - _OPERATIONAL_INT_KEYS

LEASE_SECONDS = {
    "enrichment": 30,
    "diagnosis": 90,
    "policy": 90,
    "review": 90,
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
                f"cause {cause!r} maps to forbidden action {action!r}; the executor "
                    "has no safe implementation for it"
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


SIMULATOR_PATH = ROOT / "config" / "simulator.yaml"

SIM_MODEL_BASELINE_PLUS_UPLIFT = "baseline_plus_uplift"
SIM_MODEL_DIRECT_RATES = "direct_rates"
SIM_MODELS = frozenset({SIM_MODEL_BASELINE_PLUS_UPLIFT, SIM_MODEL_DIRECT_RATES})

# Values that mean "deliberately unset" rather than a number.
_UNSET = frozenset({"", "null", "none", "~"})


class SimulatorConfigError(ValueError):
    """Invalid, incomplete, or unsourced experiment configuration."""


@dataclass(frozen=True)
class SimulatorConfig:
    """`config/simulator.yaml`. Research values are required AND must be cited.

    Per-action rates must be externally sourced and cited. This dataclass
    cannot be constructed without both the number and its citation, so a run on
    unsourced values fails at load rather than producing an authoritative-looking
    result from an invented parameter.
    """

    seed: int
    n_per_arm: int
    model: str
    organic_baseline_rate: float
    organic_baseline_source: str
    action_params: dict[str, float]
    action_sources: dict[str, str]
    amount_band_bounds: tuple[int, ...]
    feature_timezone: str
    history_window_days: int

    def params_for_run(self) -> dict[str, Any]:
        """Exactly what is persisted to `sim_runs.params`.

        Every number travels with its citation, so a stored run carries its own
        provenance and a reader never has to trust the config file's later state.
        """
        return {
            "model": self.model,
            "organic_baseline": {
                "rate": self.organic_baseline_rate,
                "source": self.organic_baseline_source,
            },
            "action_params": {
                action: {
                    "value": self.action_params[action],
                    "source": self.action_sources[action],
                }
                for action in sorted(self.action_params)
            },
            "feature_encoding": {
                "amount_band_bounds": list(self.amount_band_bounds),
                "timezone": self.feature_timezone,
                "history_window_days": self.history_window_days,
                "weighted": False,
            },
        }


def _sim_required(values: dict[str, str], key: str) -> str:
    raw = values.get(key, "")
    if raw.strip().lower() in _UNSET:
        raise SimulatorConfigError(
            f"{key} is unset in config/simulator.yaml. Rates must be externally "
            "sourced and cited; a run must not proceed on an invented number."
        )
    return raw


def _sim_rate(values: dict[str, str], key: str) -> float:
    # _sim_required raises SimulatorConfigError, which subclasses ValueError --
    # so it must run outside the try, or the precise "unset" message is masked
    # by the float-conversion handler below.
    raw = _sim_required(values, key)
    try:
        rate = float(raw)
    except ValueError as exc:
        raise SimulatorConfigError(f"{key} is not a number") from exc
    if not 0.0 <= rate <= 1.0:
        raise SimulatorConfigError(f"{key}={rate} is not a probability in [0, 1]")
    return rate


def load_simulator_config(path: Path | None = None) -> SimulatorConfig:
    target = path or SIMULATOR_PATH
    if not target.exists():
        raise SimulatorConfigError(f"missing experiment configuration: {target}")

    values = {key: raw for key, raw in _scalar_lines(target)}

    model = _sim_required(values, "model")
    if model not in SIM_MODELS:
        raise SimulatorConfigError(
            f"model {model!r} is not one of {sorted(SIM_MODELS)}"
        )

    try:
        seed = int(_sim_required(values, "seed"))
        n_per_arm = int(_sim_required(values, "n_per_arm"))
        history_window_days = int(_sim_required(values, "history_window_days"))
    except ValueError as exc:
        raise SimulatorConfigError(f"non-integer experiment parameter: {exc}") from exc

    if n_per_arm <= 0:
        raise SimulatorConfigError("n_per_arm must be positive")
    if history_window_days <= 0:
        raise SimulatorConfigError("history_window_days must be positive")

    bounds_raw = _sim_required(values, "amount_band_bounds")
    try:
        bounds = tuple(int(part) for part in bounds_raw.split(",") if part.strip())
    except ValueError as exc:
        raise SimulatorConfigError("amount_band_bounds must be integers") from exc
    if list(bounds) != sorted(bounds) or len(set(bounds)) != len(bounds):
        raise SimulatorConfigError("amount_band_bounds must be strictly ascending")

    action = "CREATE_PAYMENT_LINK"
    return SimulatorConfig(
        seed=seed,
        n_per_arm=n_per_arm,
        model=model,
        organic_baseline_rate=_sim_rate(values, "organic_baseline_rate"),
        organic_baseline_source=_sim_required(values, "organic_baseline_source"),
        action_params={action: _sim_rate(values, "action_param_create_payment_link")},
        action_sources={
            action: _sim_required(values, "action_param_create_payment_link_source")
        },
        amount_band_bounds=bounds,
        feature_timezone=_sim_required(values, "feature_timezone"),
        history_window_days=history_window_days,
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


def load_ollama_config(path: Path | None = None):
    """Ollama host/model/timeout from operational.yaml. Lazy-import client type."""
    from reclaim.llm.client import OllamaConfig

    values = load_operational(path)
    return OllamaConfig(
        host=str(values["ollama_host"]),
        port=int(values["ollama_port"]),
        model=str(values["ollama_model"]),
        timeout_seconds=int(values["ollama_timeout_seconds"]),
    )
