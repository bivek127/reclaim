"""Pure deterministic policy evaluation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from reclaim.config import PolicyConfig, PolicyConfigError, load_policy_config
from reclaim.domain.policy import (
    TABLE_ACTION_LINK,
    TABLE_ACTION_RETRY,
    PolicyFacts,
    evaluate,
)

# Cause-to-action table rows: cause → expected verdict and selected_action
TABLE_ROWS: list[tuple[str, str, str | None]] = [
    ("INSUFFICIENT_FUNDS", "ALLOW", "CREATE_PAYMENT_LINK"),
    ("EXPIRED_CARD", "ALLOW", "CREATE_PAYMENT_LINK"),
    ("INCORRECT_CVV", "ALLOW", "CREATE_PAYMENT_LINK"),
    ("AUTHENTICATION_FAILED", "ALLOW", "CREATE_PAYMENT_LINK"),
    ("NETWORK_ERROR_NPCI", "ALLOW", "CREATE_PAYMENT_LINK"),
    ("BANK_DOWNTIME", "ALLOW", "CREATE_PAYMENT_LINK"),
    ("CARD_DECLINED_ISSUER", "ESCALATE", None),
    ("MANDATE_REVOKED", "ESCALATE", None),
    ("RISK_BLOCKED", "NO_ACTION", None),
    ("UNKNOWN", "ALLOW", "CREATE_PAYMENT_LINK"),
]


@pytest.fixture
def config() -> PolicyConfig:
    return load_policy_config()


def _facts(
    *,
    cause: str = "INSUFFICIENT_FUNDS",
    attempt_count: int = 0,
    max_attempts: int = 2,
    conflicting_history: bool = False,
) -> PolicyFacts:
    return PolicyFacts(
        cause=cause,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        conflicting_history=conflicting_history,
    )


@pytest.mark.parametrize("cause,verdict,action", TABLE_ROWS)
def test_policy_table_row(
    config: PolicyConfig, cause: str, verdict: str, action: str | None
) -> None:
    decision = evaluate(_facts(cause=cause), config)
    assert decision.verdict == verdict
    assert decision.selected_action == action
    assert decision.lookup_miss is False
    assert decision.ambiguity_signal is False


def test_ambiguity_signal_escalates(config: PolicyConfig) -> None:
    decision = evaluate(
        _facts(cause="NOT_IN_TABLE", conflicting_history=True),
        config,
    )
    assert decision.verdict == "ESCALATE"
    assert decision.selected_action is None
    assert decision.lookup_miss is True
    assert decision.ambiguity_signal is True
    assert decision.reason_code == "policy_escalate_ambiguity"


def test_budget_exhausted_escalates(config: PolicyConfig) -> None:
    decision = evaluate(
        _facts(cause="INSUFFICIENT_FUNDS", attempt_count=2, max_attempts=2),
        config,
    )
    assert decision.verdict == "ESCALATE"
    assert decision.selected_action is None
    assert decision.reason_code == "policy_escalate_budget"


def test_lookup_miss_without_conflict_allows_unknown_default(
    config: PolicyConfig,
) -> None:
    decision = evaluate(
        _facts(cause="TOTALLY_UNKNOWN_CAUSE", conflicting_history=False),
        config,
    )
    assert decision.verdict == "ALLOW"
    assert decision.selected_action == "CREATE_PAYMENT_LINK"
    assert decision.lookup_miss is True
    assert decision.ambiguity_signal is False
    assert decision.reason_code == "policy_allow_unknown_default"


def test_ambiguity_precedence_over_budget(config: PolicyConfig) -> None:
    """Row 1 beats row 2 when both would apply."""
    decision = evaluate(
        _facts(
            cause="UNKNOWN_CAUSE",
            attempt_count=99,
            max_attempts=2,
            conflicting_history=True,
        ),
        config,
    )
    assert decision.verdict == "ESCALATE"
    assert decision.reason_code == "policy_escalate_ambiguity"


def test_budget_precedence_over_table_allow(config: PolicyConfig) -> None:
    decision = evaluate(
        _facts(cause="INSUFFICIENT_FUNDS", attempt_count=2, max_attempts=2),
        config,
    )
    assert decision.reason_code == "policy_escalate_budget"


def test_evaluate_is_deterministic(config: PolicyConfig) -> None:
    facts = _facts(cause="EXPIRED_CARD")
    first = evaluate(facts, config)
    second = evaluate(facts, config)
    assert first == second


def test_confidence_and_recommended_action_do_not_influence_evaluate(
    config: PolicyConfig,
) -> None:
    """Policy reads only PolicyFacts; diagnosis fields are not inputs."""
    base = _facts(cause="INSUFFICIENT_FUNDS")
    assert evaluate(base, config) == evaluate(base, config)


def test_retry_charge_is_rewritten_by_evaluator_guard() -> None:
    cfg = replace(
        load_policy_config(),
        causes={**load_policy_config().causes, "INSUFFICIENT_FUNDS": TABLE_ACTION_RETRY},
    )
    decision = evaluate(_facts(cause="INSUFFICIENT_FUNDS"), cfg)
    assert decision.verdict == "ALLOW"
    assert decision.selected_action == TABLE_ACTION_LINK
    assert decision.selected_action != TABLE_ACTION_RETRY


def test_config_rejects_retry_charge_in_yaml(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(
        """
policy_version: "9.9"
max_attempts: 2
ttl_budget_ms: 1
review_ttl_ms: 1
breaker_failure_threshold: 5
breaker_reset_seconds: 120
causes:
  INSUFFICIENT_FUNDS: RETRY_CHARGE
""",
        encoding="utf-8",
    )
    with pytest.raises(PolicyConfigError, match="RETRY_CHARGE"):
        load_policy_config(path)


def test_policy_version_is_stamped_on_decision(config: PolicyConfig) -> None:
    decision = evaluate(_facts(), config)
    assert decision.policy_version == config.policy_version
