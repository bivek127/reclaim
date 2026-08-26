"""Pure reconstruction of a case's history from its audit trail.

This module imports no database driver and holds no connection. It cannot reach
a production table even if a future change wanted to -- there is nothing here to
reach one with. That is the point: the trail ALONE must suffice, and a
reconstruction that could quietly fall back to `recovery_cases` would prove
nothing about the trail.

`CaseAuditHistory.unreconstructable` is deliberate. When the trail cannot
supply a fact the reader needs, this reports it rather than returning None and
letting a reader assume the fact was simply absent from the case. Missing
evidence is itself evidence, and hiding it would defeat the audit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from reclaim.audit.events import AuditEvent

EVENT_STATE_TRANSITION = "state_transition"
EVENT_CASE_CREATED = "case_created"
EVENT_CASE_DEDUPLICATED = "case_deduplicated"
EVENT_DIAGNOSIS = "diagnosis_produced"
EVENT_POLICY = "policy_decision"
EVENT_REQUEST_SENT = "provider_request_sent"
EVENT_RESPONSE_RECEIVED = "provider_response_received"
EVENT_RESPONSE_OBSERVED = "provider_response_observed"
EVENT_VERIFICATION = "verification_recorded"
EVENT_REVIEW = "review_decision"
EVENT_STALE_WRITE = "stale_write_rejected"

PROVIDER_EVENTS = frozenset(
    {
        EVENT_REQUEST_SENT,
        EVENT_RESPONSE_RECEIVED,
        EVENT_RESPONSE_OBSERVED,
        "reconciliation_query_sent",
        "reconciliation_repost_sent",
        "reconciliation_result",
    }
)


@dataclass(frozen=True)
class StateChange:
    at: datetime
    prev_state: str | None
    new_state: str | None
    reason_code: str | None
    worker_id: str | None
    fencing_token: int | None


@dataclass(frozen=True)
class ReconstructedAttempt:
    attempt_id: int
    provider_reference: str | None
    provider_request_ids: tuple[int, ...]
    provider_correlation_ids: tuple[str, ...]
    outcomes: tuple[str, ...]
    first_seen_at: datetime


@dataclass(frozen=True)
class ReconstructedAction:
    action_id: int
    action_type: str | None
    first_seen_at: datetime
    attempts: tuple[ReconstructedAttempt, ...]


@dataclass(frozen=True)
class ReconstructedDecision:
    """A diagnosis, policy decision, review, or verification."""

    kind: str
    at: datetime
    model: str | None = None
    policy_version: str | None = None
    reviewer_ref: str | None = None
    reason_code: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CaseAuditHistory:
    """The complete case reconstruction. Every field came from the trail."""

    case_id: int | None
    obligation_id: int | None
    created: bool
    deduplicated: bool
    timeline: tuple[AuditEvent, ...]
    state_changes: tuple[StateChange, ...]
    actions: tuple[ReconstructedAction, ...]
    diagnoses: tuple[ReconstructedDecision, ...]
    policy_decisions: tuple[ReconstructedDecision, ...]
    reviews: tuple[ReconstructedDecision, ...]
    verifications: tuple[ReconstructedDecision, ...]
    provider_correlation_ids: tuple[str, ...]
    provider_references: tuple[str, ...]
    workers: tuple[str, ...]
    fencing_tokens: tuple[int, ...]
    stale_writes: tuple[AuditEvent, ...]
    unreconstructable: tuple[str, ...]

    @property
    def final_state(self) -> str | None:
        return self.state_changes[-1].new_state if self.state_changes else None


def reconstruct_case_history(events: tuple[AuditEvent, ...] | list[AuditEvent]) -> CaseAuditHistory:
    """audit_events[] -> CaseAuditHistory. Pure; no I/O of any kind."""
    ordered = tuple(events)

    case_id = _first_not_none(e.case_id for e in ordered)
    obligation_id = _first_not_none(e.obligation_id for e in ordered)

    state_changes = tuple(
        StateChange(
            at=e.occurred_at,
            prev_state=e.prev_state,
            new_state=e.new_state,
            reason_code=e.reason_code,
            worker_id=e.worker_id,
            fencing_token=e.fencing_token,
        )
        for e in ordered
        if e.event_type == EVENT_STATE_TRANSITION
    )

    actions = _rebuild_actions(ordered)

    history = CaseAuditHistory(
        case_id=case_id,
        obligation_id=obligation_id,
        created=any(e.event_type == EVENT_CASE_CREATED for e in ordered),
        deduplicated=any(e.event_type == EVENT_CASE_DEDUPLICATED for e in ordered),
        timeline=ordered,
        state_changes=state_changes,
        actions=actions,
        diagnoses=_decisions(ordered, EVENT_DIAGNOSIS, "diagnosis"),
        policy_decisions=_decisions(ordered, EVENT_POLICY, "policy"),
        reviews=_decisions(ordered, EVENT_REVIEW, "review"),
        verifications=_decisions(ordered, EVENT_VERIFICATION, "verification"),
        provider_correlation_ids=_unique(
            e.provider_correlation_id for e in ordered if e.provider_correlation_id
        ),
        provider_references=_unique(
            _reference_of(e) for e in ordered if _reference_of(e)
        ),
        workers=_unique(e.worker_id for e in ordered if e.worker_id),
        fencing_tokens=_unique(
            e.fencing_token for e in ordered if e.fencing_token is not None
        ),
        stale_writes=tuple(e for e in ordered if e.event_type == EVENT_STALE_WRITE),
        unreconstructable=(),
    )
    # Recomputed with the assembled history in hand so the report reflects what
    # a reader can actually recover, not what the raw rows happen to contain.
    return _with_gaps(history)


# ---------------------------------------------------------------------------


def _rebuild_actions(events: tuple[AuditEvent, ...]) -> tuple[ReconstructedAction, ...]:
    """Group provider activity under actions and attempts, preserving order."""
    action_order: list[int] = []
    action_type: dict[int, str | None] = {}
    action_first: dict[int, datetime] = {}
    attempts: dict[int, dict[int, dict[str, Any]]] = {}

    for e in events:
        if e.action_id is None:
            continue
        if e.action_id not in action_type:
            action_order.append(e.action_id)
            action_type[e.action_id] = None
            action_first[e.action_id] = e.occurred_at
            attempts[e.action_id] = {}
        if action_type[e.action_id] is None and e.get("action_type"):
            action_type[e.action_id] = e.get("action_type")

        if e.attempt_id is None:
            continue
        slot = attempts[e.action_id].setdefault(
            e.attempt_id,
            {
                "reference": None,
                "request_ids": [],
                "correlation_ids": [],
                "outcomes": [],
                "first": e.occurred_at,
            },
        )
        if slot["reference"] is None and _reference_of(e):
            slot["reference"] = _reference_of(e)
        if e.provider_request_id is not None and e.provider_request_id not in slot["request_ids"]:
            slot["request_ids"].append(e.provider_request_id)
        if e.provider_correlation_id and e.provider_correlation_id not in slot["correlation_ids"]:
            slot["correlation_ids"].append(e.provider_correlation_id)
        outcome = e.get("provider_outcome") or e.get("fetch_outcome")
        if outcome:
            slot["outcomes"].append(outcome)

    return tuple(
        ReconstructedAction(
            action_id=aid,
            action_type=action_type[aid],
            first_seen_at=action_first[aid],
            attempts=tuple(
                ReconstructedAttempt(
                    attempt_id=tid,
                    provider_reference=slot["reference"],
                    provider_request_ids=tuple(slot["request_ids"]),
                    provider_correlation_ids=tuple(slot["correlation_ids"]),
                    outcomes=tuple(slot["outcomes"]),
                    first_seen_at=slot["first"],
                )
                for tid, slot in attempts[aid].items()
            ),
        )
        for aid in action_order
    )


def _decisions(
    events: tuple[AuditEvent, ...], event_type: str, kind: str
) -> tuple[ReconstructedDecision, ...]:
    return tuple(
        ReconstructedDecision(
            kind=kind,
            at=e.occurred_at,
            model=e.model,
            policy_version=e.policy_version,
            reviewer_ref=e.reviewer_ref,
            reason_code=e.reason_code,
            detail=dict(e.detail or {}),
        )
        for e in events
        if e.event_type == event_type
    )


def _reference_of(e: AuditEvent) -> str | None:
    return e.get("provider_reference") or e.get("reference_id") or e.get("idempotency_key")


def _with_gaps(h: CaseAuditHistory) -> CaseAuditHistory:
    """Name every required item the trail could not supply. Never silently None."""
    gaps: list[str] = []
    if h.case_id is None:
        gaps.append("case_id")
    if h.obligation_id is None:
        gaps.append("obligation_id")
    if not h.created and not h.deduplicated:
        gaps.append("case_created/case_deduplicated")
    if not h.state_changes:
        gaps.append("state_changes")
    for action in h.actions:
        if action.action_type is None:
            gaps.append(f"action_type[action_id={action.action_id}]")
        for attempt in action.attempts:
            if attempt.provider_reference is None:
                gaps.append(f"provider_reference[attempt_id={attempt.attempt_id}]")
    if h.actions and not h.provider_correlation_ids:
        gaps.append("provider_correlation_id")

    return CaseAuditHistory(**{**h.__dict__, "unreconstructable": tuple(gaps)})


def _first_not_none(values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _unique(values: Any) -> tuple[Any, ...]:
    seen: list[Any] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return tuple(seen)
