"""'Full matrix green; full invariant table green', made mechanical.

The failure matrix and invariant table each name every test they expect, so
that claim is checkable rather than asserted: this parses the specification
and fails if any named test does not resolve to a real one.

It is deliberately name-based. A row can be behaviourally covered under some
other name and still be untraceable to an auditor grepping the specification
-- which is exactly the gap this closes, and exactly the kind of thing a
forensic contract should not tolerate.

The specification is the input, never the output: this reads it and never edits
it. The specification is not distributed with the repository, so when it is
absent this module skips rather than failing collection.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "SPECIFICATION.md"
TESTS = ROOT / "tests"

# The specification is not distributed with the repository. Parsing it happens
# at import, so without this guard a checkout that lacks the file fails during
# collection -- taking the whole suite down, not just this module.
if not SPEC.exists():
    pytest.skip(
        "the specification document is not present in this checkout, so the "
        "coverage cross-check has no input to read",
        allow_module_level=True,
    )

_TEST_NAME = re.compile(r"`(test_[a-z0-9_]+)`")


def _section(heading: str, stop: str) -> str:
    text = SPEC.read_text(encoding="utf-8")
    start = text.index(heading)
    return text[start : text.index(stop, start)]


def _named_tests(heading: str, stop: str) -> list[str]:
    return sorted(set(_TEST_NAME.findall(_section(heading, stop))))


def _defined_tests() -> set[str]:
    """Every `def test_*` under tests/, found by AST rather than grep.

    A grep would match a name inside a comment or a string; only a real
    function definition counts as coverage.
    """
    defined: set[str] = set()
    for path in TESTS.rglob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test_"):
                    defined.add(node.name)
    return defined


MATRIX_TESTS = _named_tests("## 16.", "## 17.")
INVARIANT_TESTS = _named_tests("## 17.", "## 18.")
DEFINED = _defined_tests()


def test_specification_actually_names_tests() -> None:
    """Guard the guard: if the parse silently found nothing, everything below
    would pass vacuously."""
    assert len(MATRIX_TESTS) >= 25, (
        f"the failure-matrix parse found only {len(MATRIX_TESTS)} named tests"
    )
    assert len(INVARIANT_TESTS) >= 15, (
        f"the invariant-table parse found only {len(INVARIANT_TESTS)} named tests"
    )
    assert len(DEFINED) > 100, f"only {len(DEFINED)} tests discovered"


@pytest.mark.parametrize("name", MATRIX_TESTS, ids=MATRIX_TESTS)
def test_matrix_row_has_a_named_test(name: str) -> None:
    """Every failure-matrix row resolves to a real test."""
    assert name in DEFINED, (
        f"the failure matrix names {name!r} but no such test is defined. "
        "'Full matrix green' means a row nobody can trace by name is not green."
    )


@pytest.mark.parametrize("name", INVARIANT_TESTS, ids=INVARIANT_TESTS)
def test_invariant_has_a_named_test(name: str) -> None:
    """Every invariant resolves to a real test."""
    assert name in DEFINED, (
        f"the invariant table names {name!r} but no such test is defined -- "
        "it is the implementation acceptance contract."
    )
