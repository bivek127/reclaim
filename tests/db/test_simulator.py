"""Constraint tests for simulator tables."""

from __future__ import annotations

import json

import psycopg
import pytest
from psycopg.errors import CheckViolation

from tests.db.helpers import insert_case, insert_obligation


def test_control_arm_cannot_have_action_type(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    case_id = insert_case(conn, obligation_id)
    run_id = conn.execute(
        """
        INSERT INTO sim_runs (seed, n_per_arm, params)
        VALUES (1, 10, %s::jsonb)
        RETURNING id
        """,
        (json.dumps({"baseline": 0.1}),),
    ).fetchone()[0]
    with pytest.raises(CheckViolation):
        conn.execute(
            """
            INSERT INTO sim_outcomes (
                run_id, arm, case_id, pre_decision_features,
                action_type, resolved, amount_minor, case_state_at_run
            ) VALUES (%s, 'CONTROL', %s, '{}'::jsonb, 'CREATE_PAYMENT_LINK', true, 100,
                      'AWAITING_CUSTOMER')
            """,
            (run_id, case_id),
        )


# ---- I11 hardening (test-only; no simulator exists yet) ------------------
#
# The schema enforces I11 for *storage*: sim_outcomes has no column for an
# agent-generated value. That does not constrain *reads* -- nothing stops a
# future simulator reading diagnoses.cause and folding it into probability,
# and a test that only varies confidence at a fixed seed would pass regardless,
# since cause and confidence vary independently.
#
# A behavioural test cannot be written until the simulator exists. These two
# pin what is checkable now and fail at the moment the bypass is introduced.

AGENT_GENERATED_CONCEPTS = (
    "confidence",
    "reasoning",
    "diagnosis",
    "model",
    "prompt",
    "llm",
)


def _columns(conn: psycopg.Connection, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (table,),
    ).fetchall()
    return {r[0] for r in rows}


def test_sim_outcomes_has_no_agent_generated_column(
    conn: psycopg.Connection,
) -> None:
    """I11 storage closure: the dependency must be unrepresentable."""
    columns = _columns(conn, "sim_outcomes")

    offenders = [
        column
        for column in columns
        for concept in AGENT_GENERATED_CONCEPTS
        if concept in column.lower()
    ]
    assert offenders == [], f"sim_outcomes gained an agent-generated column: {offenders}"


def test_sim_runs_has_no_agent_generated_column(conn: psycopg.Connection) -> None:
    columns = _columns(conn, "sim_runs")

    offenders = [
        column
        for column in columns
        for concept in AGENT_GENERATED_CONCEPTS
        if concept in column.lower()
    ]
    assert offenders == [], f"sim_runs gained an agent-generated column: {offenders}"


def test_no_simulator_module_reads_diagnoses() -> None:
    """I11 read guard -- the bypass the schema cannot catch.

    The simulator must never depend on any agent-generated feature, and
    `diagnoses.cause` is agent-generated whenever `source='LLM'`. The schema
    stops such a value being *stored*; nothing stops it being *read*.

    Checked over the AST rather than raw text, so that a docstring explaining
    *why* diagnoses must not be read does not trip the guard, while a real
    reference -- a SQL string, an attribute, a name -- does. Comments never
    reach the AST; docstrings are identified and skipped explicitly.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "reclaim"
    offenders = []

    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "sim_outcomes" not in source and "sim_runs" not in source:
            continue

        tree = ast.parse(source)
        docstrings = {
            node.body[0].value
            for node in ast.walk(tree)
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            )
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }

        for node in ast.walk(tree):
            hit = False
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                hit = node not in docstrings and "diagnoses" in node.value
            elif isinstance(node, ast.Name):
                hit = "diagnoses" in node.id
            elif isinstance(node, ast.Attribute):
                hit = "diagnoses" in node.attr
            if hit:
                offenders.append(f"{path.relative_to(root)}:{getattr(node, 'lineno', '?')}")

    assert offenders == [], (
        "simulator-facing code references diagnoses, which I11 forbids: "
        f"{sorted(set(offenders))}"
    )
