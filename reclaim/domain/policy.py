"""Deterministic policy evaluation and application.

The enforcement boundary between diagnosis and execution. Policy decides what
is allowed from trusted facts and the cause-to-action table; it never calls
providers, never dispatches, and never writes revenue.

`conflicting_history` is an explicit caller-supplied fact used in the
ambiguity formula. There is no schema mapping yet for "successful payment" /
"failed payment" in the trailing 30-day window, so history resolution remains
outside this module until that mapping exists.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

from reclaim.config import PolicyConfig, load_policy_config
from reclaim.domain.leases import claim_next, fenced_transition
from reclaim.domain.states import CaseState

# Closed cause vocabulary.
POLICY_CAUSES: frozenset[str] = frozenset(
    {
        "INSUFFICIENT_FUNDS",
        "CARD_DECLINED_ISSUER",
        "EXPIRED_CARD",
        "INCORRECT_CVV",
        "AUTHENTICATION_FAILED",
        "NETWORK_ERROR_NPCI",
        "BANK_DOWNTIME",
        "MANDATE_REVOKED",
        "RISK_BLOCKED",
        "UNKNOWN",
    }
)

# Table action tokens stored in config — not all are executable action_type values.
TABLE_ACTION_LINK = "CREATE_PAYMENT_LINK"
TABLE_ACTION_ESCALATE = "ESCALATE"
TABLE_ACTION_NO_ACTION = "NO_ACTION"
TABLE_ACTION_RETRY = "RETRY_CHARGE"

VERDICT_ALLOW = "ALLOW"
VERDICT_ESCALATE = "ESCALATE"
VERDICT_NO_ACTION = "NO_ACTION"


class PolicyBlocked(Exception):
    """The case cannot be evaluated or applied as presented."""


@dataclass(frozen=True)
class PolicyFacts:
    """Trusted deterministic inputs for policy evaluation.

    `conflicting_history` must be supplied explicitly by the caller. This
    module does not infer it from the database.
    """

    cause: str
    attempt_count: int
    max_attempts: int
    conflicting_history: bool


@dataclass(frozen=True)
class PolicyDecision:
    """Pure evaluation result — mirrors `policy_decisions` columns."""

    policy_version: str
    lookup_miss: bool
    conflicting_history: bool
    ambiguity_signal: bool
    verdict: str
    selected_action: str | None
    reason_code: str


@dataclass(frozen=True)
class ApplyResult:
    case_id: int
    case_state: CaseState
    applied: bool
    policy_decision_id: int | None = None
    decision: PolicyDecision | None = None
    reason: str = ""


def evaluate(facts: PolicyFacts, config: PolicyConfig) -> PolicyDecision:
    """Pure cause-to-action evaluation. No I/O."""
    lookup_miss = facts.cause not in config.causes
    ambiguity_signal = lookup_miss and facts.conflicting_history

    # Row 1: ambiguity_signal
    if ambiguity_signal:
        return _decision(
            facts,
            config,
            lookup_miss=lookup_miss,
            ambiguity_signal=True,
            verdict=VERDICT_ESCALATE,
            selected_action=None,
            reason_code="policy_escalate_ambiguity",
        )

    # Row 2: budget exhausted
    if facts.attempt_count >= facts.max_attempts:
        return _decision(
            facts,
            config,
            lookup_miss=lookup_miss,
            ambiguity_signal=False,
            verdict=VERDICT_ESCALATE,
            selected_action=None,
            reason_code="policy_escalate_budget",
        )

    # Row 3 / 4: cause in table
    if not lookup_miss:
        table_action = _executable_table_action(config.causes[facts.cause])
        if table_action == TABLE_ACTION_ESCALATE:
            return _decision(
                facts,
                config,
                lookup_miss=False,
                ambiguity_signal=False,
                verdict=VERDICT_ESCALATE,
                selected_action=None,
                reason_code=_escalate_reason(facts.cause),
            )
        if table_action == TABLE_ACTION_NO_ACTION:
            return _decision(
                facts,
                config,
                lookup_miss=False,
                ambiguity_signal=False,
                verdict=VERDICT_NO_ACTION,
                selected_action=None,
                reason_code="policy_no_action_risk",
            )
        if table_action == TABLE_ACTION_LINK:
            return _decision(
                facts,
                config,
                lookup_miss=False,
                ambiguity_signal=False,
                verdict=VERDICT_ALLOW,
                selected_action=TABLE_ACTION_LINK,
                reason_code="policy_allow_create_link",
            )
        # Row 4: table action not viable (should not occur with valid config)
        return _decision(
            facts,
            config,
            lookup_miss=False,
            ambiguity_signal=False,
            verdict=VERDICT_NO_ACTION,
            selected_action=None,
            reason_code="policy_no_viable_action",
        )

    # Row 5: lookup_miss without conflicting history → UNKNOWN default
    return _decision(
        facts,
        config,
        lookup_miss=True,
        ambiguity_signal=False,
        verdict=VERDICT_ALLOW,
        selected_action=TABLE_ACTION_LINK,
        reason_code="policy_allow_unknown_default",
    )


def apply_policy(
    conn: psycopg.Connection,
    case_id: int,
    *,
    facts: PolicyFacts,
    diagnosis_id: int,
    fencing_token: int,
    config: PolicyConfig | None = None,
    worker_id: str | None = None,
) -> ApplyResult:
    """Persist a policy decision and transition the case out of POLICY_EVAL."""
    _assert_diagnosis(conn, case_id=case_id, diagnosis_id=diagnosis_id)

    cfg = config or load_policy_config()
    decision = evaluate(facts, cfg)
    target = _target_state(decision.verdict)

    with conn.transaction():
        if not _claimable(conn, case_id, fencing_token):
            fenced_transition(
                conn,
                case_id,
                CaseState.POLICY_EVAL,
                target,
                fencing_token,
                decision.reason_code,
                worker_id=worker_id,
            )
            return ApplyResult(
                case_id=case_id,
                case_state=CaseState.POLICY_EVAL,
                applied=False,
                decision=decision,
                reason=decision.reason_code,
            )

        decision_id = _insert_policy_decision(
            conn,
            case_id=case_id,
            diagnosis_id=diagnosis_id,
            decision=decision,
        )

        def _settle(inner: psycopg.Connection) -> None:
            _audit_policy_decision(
                inner,
                case_id=case_id,
                diagnosis_id=diagnosis_id,
                decision=decision,
                worker_id=worker_id,
                fencing_token=fencing_token,
            )
            # Every transition into ESCALATED creates a PENDING review.
            # Reuse this policy_decisions row -- no second provenance record.
            if target is CaseState.ESCALATED:
                from reclaim.domain.review import on_entered_escalated

                on_entered_escalated(
                    inner,
                    case_id,
                    reason_code=decision.reason_code,
                    policy_decision_id=decision_id,
                )

        applied = fenced_transition(
            conn,
            case_id,
            CaseState.POLICY_EVAL,
            target,
            fencing_token,
            decision.reason_code,
            worker_id=worker_id,
            side_effects=_settle,
        )
        if not applied:
            raise PolicyBlocked(
                f"policy decision {decision_id} inserted but transition rejected"
            )

    return ApplyResult(
        case_id=case_id,
        case_state=target,
        applied=True,
        policy_decision_id=decision_id,
        decision=decision,
        reason=decision.reason_code,
    )


def evaluate_once(
    conn: psycopg.Connection,
    *,
    conflicting_history: bool,
    worker_id: str = "policy",
    lease_seconds: int | None = None,
    config: PolicyConfig | None = None,
) -> ApplyResult | None:
    """Claim one POLICY_EVAL case and apply policy. None when nothing claimable.

    `conflicting_history` is required — callers must establish it explicitly.
    There is no silent default to "no conflict".
    """
    from reclaim.config import lease_seconds_for

    lease = lease_seconds or lease_seconds_for("policy")
    claim = claim_next(conn, CaseState.POLICY_EVAL, worker_id, lease)
    if claim is None:
        return None

    facts, diagnosis_id = load_policy_inputs(conn, claim.case_id, conflicting_history)
    return apply_policy(
        conn,
        claim.case_id,
        facts=facts,
        diagnosis_id=diagnosis_id,
        fencing_token=claim.fencing_token,
        config=config,
        worker_id=worker_id,
    )


def load_policy_inputs(
    conn: psycopg.Connection,
    case_id: int,
    conflicting_history: bool,
) -> tuple[PolicyFacts, int]:
    """Read trusted case and diagnosis facts. Does not resolve payment history."""
    case_row = conn.execute(
        """
        SELECT attempt_count, max_attempts, state
          FROM recovery_cases WHERE id = %s
        """,
        (case_id,),
    ).fetchone()
    if case_row is None:
        raise PolicyBlocked(f"case {case_id} not found")
    attempt_count, max_attempts, state = case_row
    if state != CaseState.POLICY_EVAL.value:
        raise PolicyBlocked(f"case {case_id} is not in POLICY_EVAL")

    diag = conn.execute(
        """
        SELECT id, cause
          FROM diagnoses
         WHERE case_id = %s
         ORDER BY id DESC
         LIMIT 1
        """,
        (case_id,),
    ).fetchone()
    if diag is None:
        raise PolicyBlocked(f"case {case_id} has no diagnosis to evaluate")

    facts = PolicyFacts(
        cause=str(diag[1]),
        attempt_count=int(attempt_count),
        max_attempts=int(max_attempts),
        conflicting_history=conflicting_history,
    )
    return facts, int(diag[0])


def _assert_diagnosis(
    conn: psycopg.Connection, *, case_id: int, diagnosis_id: int
) -> None:
    row = conn.execute(
        "SELECT 1 FROM diagnoses WHERE id = %s AND case_id = %s",
        (diagnosis_id, case_id),
    ).fetchone()
    if row is None:
        raise PolicyBlocked(
            f"diagnosis {diagnosis_id} is not present for case {case_id}"
        )


def _decision(
    facts: PolicyFacts,
    config: PolicyConfig,
    *,
    lookup_miss: bool,
    ambiguity_signal: bool,
    verdict: str,
    selected_action: str | None,
    reason_code: str,
) -> PolicyDecision:
    if verdict == VERDICT_ALLOW:
        assert selected_action is not None
        selected_action = _executable_table_action(selected_action)
    else:
        selected_action = None
    return PolicyDecision(
        policy_version=config.policy_version,
        lookup_miss=lookup_miss,
        conflicting_history=facts.conflicting_history,
        ambiguity_signal=ambiguity_signal,
        verdict=verdict,
        selected_action=selected_action,
        reason_code=reason_code,
    )


def _executable_table_action(action: str) -> str:
    if action == TABLE_ACTION_RETRY:
        return TABLE_ACTION_LINK
    return action


def _escalate_reason(cause: str) -> str:
    if cause == "CARD_DECLINED_ISSUER":
        return "policy_escalate_issuer"
    if cause == "MANDATE_REVOKED":
        return "policy_escalate_mandate"
    return "policy_escalate"


def _target_state(verdict: str) -> CaseState:
    if verdict == VERDICT_ALLOW:
        return CaseState.ACTION_READY
    if verdict == VERDICT_ESCALATE:
        return CaseState.ESCALATED
    if verdict == VERDICT_NO_ACTION:
        return CaseState.VERIFIED_FAILED
    raise PolicyBlocked(f"unknown verdict {verdict!r}")


def _claimable(conn: psycopg.Connection, case_id: int, fencing_token: int) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM recovery_cases
         WHERE id = %s AND state = %s AND fencing_token = %s
         FOR UPDATE
        """,
        (case_id, CaseState.POLICY_EVAL.value, fencing_token),
    ).fetchone()
    return row is not None


def _insert_policy_decision(
    conn: psycopg.Connection,
    *,
    case_id: int,
    diagnosis_id: int,
    decision: PolicyDecision,
) -> int:
    row = conn.execute(
        """
        INSERT INTO policy_decisions (
            case_id, diagnosis_id, policy_version, lookup_miss,
            conflicting_history, ambiguity_signal, verdict,
            selected_action, reason_code
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            case_id,
            diagnosis_id,
            decision.policy_version,
            decision.lookup_miss,
            decision.conflicting_history,
            decision.ambiguity_signal,
            decision.verdict,
            decision.selected_action,
            decision.reason_code,
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _audit_policy_decision(
    conn: psycopg.Connection,
    *,
    case_id: int,
    diagnosis_id: int,
    decision: PolicyDecision,
    worker_id: str | None,
    fencing_token: int,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_events (
            event_type, obligation_id, case_id, worker_id, fencing_token,
            policy_version, reason_code, detail
        )
        SELECT 'policy_decision', c.obligation_id, %s, %s, %s, %s, %s, %s
          FROM recovery_cases c WHERE c.id = %s
        """,
        (
            case_id,
            worker_id,
            fencing_token,
            decision.policy_version,
            decision.reason_code,
            Jsonb(
                {
                    "verdict": decision.verdict,
                    "selected_action": decision.selected_action,
                    "lookup_miss": decision.lookup_miss,
                    "conflicting_history": decision.conflicting_history,
                    "ambiguity_signal": decision.ambiguity_signal,
                    "diagnosis_id": diagnosis_id,
                }
            ),
            case_id,
        ),
    )
