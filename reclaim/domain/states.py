"""Closed case-state set and the allowed transition table."""

from __future__ import annotations

from enum import Enum


class CaseState(str, Enum):
    NEW = "NEW"
    ENRICHING = "ENRICHING"
    DIAGNOSING = "DIAGNOSING"
    POLICY_EVAL = "POLICY_EVAL"
    ACTION_READY = "ACTION_READY"
    EXECUTING = "EXECUTING"
    AWAITING_CUSTOMER = "AWAITING_CUSTOMER"
    ATTEMPT_FAILED = "ATTEMPT_FAILED"
    AMBIGUOUS = "AMBIGUOUS"
    RECONCILING = "RECONCILING"
    HALTED = "HALTED"
    ESCALATED = "ESCALATED"
    VERIFIED_RECOVERED = "VERIFIED_RECOVERED"
    VERIFIED_FAILED = "VERIFIED_FAILED"
    EXPIRED_UNRESOLVED = "EXPIRED_UNRESOLVED"


CASE_STATES: tuple[CaseState, ...] = tuple(CaseState)

TERMINAL_STATES = frozenset(
    {
        CaseState.VERIFIED_RECOVERED,
        CaseState.VERIFIED_FAILED,
        CaseState.EXPIRED_UNRESOLVED,
    }
)

CLOCK_STOPPED_STATES = TERMINAL_STATES | {CaseState.HALTED}

# Explicit allowed edges. Absence is illegal.
ALLOWED_TRANSITIONS: frozenset[tuple[CaseState, CaseState]] = frozenset(
    {
        (CaseState.NEW, CaseState.ENRICHING),
        (CaseState.ENRICHING, CaseState.DIAGNOSING),
        (CaseState.ENRICHING, CaseState.ESCALATED),
        (CaseState.DIAGNOSING, CaseState.POLICY_EVAL),
        (CaseState.DIAGNOSING, CaseState.ESCALATED),
        (CaseState.POLICY_EVAL, CaseState.ACTION_READY),
        (CaseState.POLICY_EVAL, CaseState.ESCALATED),
        (CaseState.POLICY_EVAL, CaseState.VERIFIED_FAILED),
        (CaseState.ACTION_READY, CaseState.EXECUTING),
        (CaseState.ACTION_READY, CaseState.HALTED),
        (CaseState.EXECUTING, CaseState.AWAITING_CUSTOMER),
        (CaseState.EXECUTING, CaseState.ATTEMPT_FAILED),
        (CaseState.EXECUTING, CaseState.AMBIGUOUS),
        (CaseState.AWAITING_CUSTOMER, CaseState.VERIFIED_RECOVERED),
        (CaseState.AWAITING_CUSTOMER, CaseState.ATTEMPT_FAILED),
        (CaseState.AWAITING_CUSTOMER, CaseState.AMBIGUOUS),
        (CaseState.ATTEMPT_FAILED, CaseState.POLICY_EVAL),
        (CaseState.ATTEMPT_FAILED, CaseState.ESCALATED),
        (CaseState.AMBIGUOUS, CaseState.RECONCILING),
        (CaseState.AMBIGUOUS, CaseState.EXPIRED_UNRESOLVED),
        (CaseState.RECONCILING, CaseState.AWAITING_CUSTOMER),
        (CaseState.RECONCILING, CaseState.ATTEMPT_FAILED),
        (CaseState.RECONCILING, CaseState.AMBIGUOUS),
        (CaseState.RECONCILING, CaseState.EXPIRED_UNRESOLVED),
        (CaseState.HALTED, CaseState.ACTION_READY),
        (CaseState.HALTED, CaseState.EXPIRED_UNRESOLVED),
        (CaseState.ESCALATED, CaseState.EXECUTING),
        (CaseState.ESCALATED, CaseState.VERIFIED_FAILED),
        (CaseState.ESCALATED, CaseState.EXPIRED_UNRESOLVED),
    }
)

AUDIT_EVENT_TYPE = "state_transition"


def as_case_state(value: CaseState | str) -> CaseState:
    if isinstance(value, CaseState):
        return value
    return CaseState(value)


def is_allowed(expected: CaseState, new: CaseState) -> bool:
    return (expected, new) in ALLOWED_TRANSITIONS
