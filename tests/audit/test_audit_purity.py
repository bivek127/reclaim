"""The reconstruction boundary, enforced structurally rather than behaviourally.

A monkeypatch proves only that today's code path did not query a table. These
tests prove the reconstruction module *cannot*, because it imports nothing that
could and names no table it could reach.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from reclaim.audit import events as events_mod
from reclaim.audit import reconstruct as reconstruct_mod

PRODUCTION_TABLES = (
    "recovery_cases",
    "financial_obligations",
    "recovery_actions",
    "execution_attempts",
    "provider_requests",
    "diagnoses",
    "policy_decisions",
    "human_reviews",
    "webhook_events",
    "verifications",
    "circuit_breaker",
    "sim_runs",
    "sim_outcomes",
)


def _imports(module) -> list[str]:
    tree = ast.parse(inspect.getsource(module))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _non_docstring_strings(module) -> list[str]:
    tree = ast.parse(inspect.getsource(module))
    docs = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and n not in docs
    ]


def test_reconstruct_imports_no_database_driver() -> None:
    """The engine holds nothing it could query with."""
    offenders = [m for m in _imports(reconstruct_mod) if "psycopg" in m]

    assert offenders == [], f"reconstruct.py imports a DB driver: {offenders}"


def test_reconstruct_names_no_production_table() -> None:
    """No SQL, no table name, no lazy 'just one lookup'."""
    blob = " ".join(_non_docstring_strings(reconstruct_mod)).lower()

    offenders = [t for t in PRODUCTION_TABLES if t in blob]
    assert offenders == [], f"reconstruct.py references production tables: {offenders}"


def test_reconstruct_issues_no_sql() -> None:
    blob = " ".join(_non_docstring_strings(reconstruct_mod)).lower()

    for verb in ("select ", "insert ", "update ", "delete ", " join "):
        assert verb not in blob, f"reconstruct.py contains SQL: {verb!r}"


def test_reconstruct_signature_takes_no_connection() -> None:
    params = inspect.signature(reconstruct_mod.reconstruct_case_history).parameters

    assert list(params) == ["events"]
    assert not any(
        "Connection" in str(p.annotation) for p in params.values()
    )


def test_loader_is_the_only_database_surface() -> None:
    """events.py may query; it must query only audit_events."""
    blob = " ".join(_non_docstring_strings(events_mod)).lower()

    assert "audit_events" in blob
    offenders = [t for t in PRODUCTION_TABLES if t in blob]
    assert offenders == [], f"loader reaches beyond audit_events: {offenders}"


def test_audit_package_has_exactly_one_query_site() -> None:
    root = Path(inspect.getfile(events_mod)).parent
    with_sql = [
        p.name
        for p in sorted(root.glob("*.py"))
        if "conn.execute" in p.read_text(encoding="utf-8")
    ]

    assert with_sql == ["events.py"], f"unexpected query sites: {with_sql}"
