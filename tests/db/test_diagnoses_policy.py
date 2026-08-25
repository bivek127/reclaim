"""Constraint tests for diagnoses and policy_decisions."""

from __future__ import annotations

import psycopg
import pytest
from psycopg.errors import CheckViolation

from tests.db.helpers import insert_case, insert_diagnosis, insert_obligation, insert_policy_decision


def test_reasoning_length_limit(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    case_id = insert_case(conn, obligation_id)
    with pytest.raises(CheckViolation):
        insert_diagnosis(conn, case_id, reasoning="x" * 801)


def test_fallback_diagnosis_cannot_have_model(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    case_id = insert_case(conn, obligation_id)
    with pytest.raises(CheckViolation):
        insert_diagnosis(
            conn,
            case_id,
            source="DETERMINISTIC_FALLBACK",
            model="should-be-null",
        )


def test_allow_verdict_requires_selected_action(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    case_id = insert_case(conn, obligation_id)
    with pytest.raises(CheckViolation):
        insert_policy_decision(
            conn,
            case_id,
            verdict="ALLOW",
            selected_action=None,
        )


def test_non_allow_verdict_forbids_selected_action(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    case_id = insert_case(conn, obligation_id)
    with pytest.raises(CheckViolation):
        insert_policy_decision(
            conn,
            case_id,
            verdict="ESCALATE",
            selected_action="CREATE_PAYMENT_LINK",
            ambiguity_signal=False,
            lookup_miss=False,
            conflicting_history=False,
        )


def test_ambiguity_signal_definition(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    case_id = insert_case(conn, obligation_id)
    with pytest.raises(CheckViolation):
        insert_policy_decision(
            conn,
            case_id,
            lookup_miss=True,
            conflicting_history=False,
            ambiguity_signal=True,
            verdict="ESCALATE",
            selected_action=None,
        )


def test_ambiguous_policy_never_allows(conn: psycopg.Connection) -> None:
    obligation_id = insert_obligation(conn)
    case_id = insert_case(conn, obligation_id)
    with pytest.raises(CheckViolation):
        insert_policy_decision(
            conn,
            case_id,
            lookup_miss=True,
            conflicting_history=True,
            ambiguity_signal=True,
            verdict="ALLOW",
            selected_action="CREATE_PAYMENT_LINK",
        )
