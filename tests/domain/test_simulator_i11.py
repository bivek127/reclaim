"""I11: the simulator cannot depend on agent-generated data.

Varying confidence and asserting identical outcomes under a fixed seed is
insufficient on its own: confidence and `diagnoses.cause` vary independently,
so it would pass even if the simulator read `cause` and folded it into
probability. The cause-invariance tests below close that gap.
"""

from __future__ import annotations

import inspect

import psycopg
import pytest

from reclaim.domain import simulator
from reclaim.domain.simulator import extract_features, fingerprint, run_simulation
from tests.domain.simulator_helpers import (
    add_diagnosis,
    seed_corpus,
    sim_config,
)


def _run(conn, **cfg):
    return run_simulation(conn, config=sim_config(**cfg))


# ---- outcomes are independent of confidence -------------------------------


def test_sim_outcome_independent_of_confidence(conn: psycopg.Connection) -> None:
    ids = seed_corpus(conn, 4)
    for case_id in ids:
        add_diagnosis(conn, case_id, confidence=0.10)
    low = _run(conn)

    conn.execute("UPDATE diagnoses SET confidence = 0.99")
    high = _run(conn)

    assert fingerprint(low.outcomes) == fingerprint(high.outcomes)


# ---- outcomes are independent of the diagnosed cause -----------------------


def test_sim_outcome_independent_of_diagnosed_cause(
    conn: psycopg.Connection,
) -> None:
    """Never a function of any agent-generated feature.

    `diagnoses.cause` is agent-generated whenever source='LLM', so the simulator
    must not read it -- even though "failure cause code" is a permitted feature.
    The permitted code comes from the provider's webhook payload instead.
    """
    ids = seed_corpus(conn, 4)
    for case_id in ids:
        add_diagnosis(conn, case_id, cause="INSUFFICIENT_FUNDS")
    before = _run(conn)

    conn.execute("UPDATE diagnoses SET cause = 'CARD_DECLINED_ISSUER'")
    after = _run(conn)

    assert fingerprint(before.outcomes) == fingerprint(after.outcomes)


def test_sim_outcome_independent_of_reasoning(conn: psycopg.Connection) -> None:
    ids = seed_corpus(conn, 4)
    for case_id in ids:
        add_diagnosis(conn, case_id, reasoning="one")
    before = _run(conn)

    conn.execute("UPDATE diagnoses SET reasoning = 'a completely different story'")
    after = _run(conn)

    assert fingerprint(before.outcomes) == fingerprint(after.outcomes)


def test_sim_outcome_independent_of_model_version(
    conn: psycopg.Connection,
) -> None:
    ids = seed_corpus(conn, 4)
    for case_id in ids:
        add_diagnosis(conn, case_id, model="model-a")
    before = _run(conn)

    conn.execute("UPDATE diagnoses SET model = 'model-b', model_version = 'v9'")
    after = _run(conn)

    assert fingerprint(before.outcomes) == fingerprint(after.outcomes)


def test_sim_outcome_independent_of_diagnosis_source(
    conn: psycopg.Connection,
) -> None:
    """LLM vs deterministic fallback must be equally irrelevant."""
    ids = seed_corpus(conn, 4)
    for case_id in ids:
        add_diagnosis(conn, case_id, source="LLM")
    before = _run(conn)

    conn.execute(
        "UPDATE diagnoses SET source = 'DETERMINISTIC_FALLBACK', model = NULL"
    )
    after = _run(conn)

    assert fingerprint(before.outcomes) == fingerprint(after.outcomes)


def test_outcomes_identical_with_and_without_any_diagnosis(
    conn: psycopg.Connection,
) -> None:
    """The strongest form: a diagnosis existing at all changes nothing."""
    seed_corpus(conn, 4)
    without = _run(conn)

    for row in conn.execute("SELECT id FROM recovery_cases ORDER BY id").fetchall():
        add_diagnosis(conn, int(row[0]))
    with_diagnoses = _run(conn)

    assert fingerprint(without.outcomes) == fingerprint(with_diagnoses.outcomes)


# ---- structural enforcement, not just behaviour --------------------------


def test_extract_features_cannot_reach_the_database(conn: psycopg.Connection) -> None:
    """I11 layer 3: the extractor takes no connection, so it cannot query.

    This is the enforcement mechanism. The behavioural tests above prove the
    current code obeys I11; this proves a future change could not quietly
    disobey it without visibly widening the signature.
    """
    params = inspect.signature(extract_features).parameters

    assert list(params) == ["case", "config"]
    annotations = [str(p.annotation) for p in params.values()]
    assert not any("Connection" in a for a in annotations)


def test_case_record_carries_no_agent_generated_field() -> None:
    """The only input to feature extraction is a whitelisted record."""
    forbidden = ("confidence", "reasoning", "model", "prompt", "diagnos", "llm")
    fields = [f.lower() for f in simulator.CaseRecord.__dataclass_fields__]

    offenders = [f for f in fields for token in forbidden if token in f]
    assert offenders == [], f"CaseRecord leaked agent-generated fields: {offenders}"


def test_simulator_module_never_references_diagnoses() -> None:
    """The static guard, scoped to this module specifically."""
    source = inspect.getsource(simulator)

    assert "diagnoses" not in source.replace(
        "`diagnoses.cause` is", ""
    ).replace("diagnoses.cause`", "").replace("reach `diagnoses`", ""), (
        "simulator.py references diagnoses outside of explanatory prose"
    )


def test_features_contain_only_permitted_keys(conn: psycopg.Connection) -> None:
    """Exactly four pre-decision features, never more."""
    seed_corpus(conn, 3)
    result = _run(conn)

    for outcome in result.outcomes:
        assert set(outcome.features) == {
            "failure_cause_code",
            "amount_band",
            "customer_payment_history",
            "hour_of_day",
        }


def test_failure_cause_comes_from_the_provider_webhook(
    conn: psycopg.Connection,
) -> None:
    """The permitted code is provider-sourced and pre-decision."""
    seed_corpus(conn, 2, with_failure_code="GATEWAY_ERROR")

    result = _run(conn, n_per_arm=2)

    assert {o.features["failure_cause_code"] for o in result.outcomes} == {
        "GATEWAY_ERROR"
    }


def test_missing_webhook_code_falls_back_to_unknown(
    conn: psycopg.Connection,
) -> None:
    seed_corpus(conn, 2, with_failure_code=None)

    result = _run(conn, n_per_arm=2)

    assert {o.features["failure_cause_code"] for o in result.outcomes} == {"UNKNOWN"}
