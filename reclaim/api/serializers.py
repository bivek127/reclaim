"""Wire shapes for the console.

Serialization only: no derived business values, no recomputed money, no
inferred state. Minor units and currency codes pass through untouched so the
browser never has to reconstruct a financial fact.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from reclaim.audit import AuditEvent, CaseAuditHistory


def plain(value: Any) -> Any:
    """JSON-safe conversion that preserves numeric precision."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {k: plain(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def audit_event(event: AuditEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "occurred_at": event.occurred_at.isoformat(),
        "event_type": event.event_type,
        "obligation_id": event.obligation_id,
        "case_id": event.case_id,
        "action_id": event.action_id,
        "attempt_id": event.attempt_id,
        "provider_request_id": event.provider_request_id,
        "worker_id": event.worker_id,
        "fencing_token": event.fencing_token,
        "prev_state": event.prev_state,
        "new_state": event.new_state,
        "reason_code": event.reason_code,
        "model": event.model,
        "model_version": event.model_version,
        "policy_version": event.policy_version,
        "reviewer_ref": event.reviewer_ref,
        "provider_correlation_id": event.provider_correlation_id,
        "detail": plain(event.detail),
    }


def case_history(history: CaseAuditHistory) -> dict[str, Any]:
    """The reconstruction, exactly as the audit package returned it.

    `unreconstructable` is carried through rather than hidden: a gap in the
    trail is evidence in its own right and the console shows it as such.
    """
    return {
        "case_id": history.case_id,
        "obligation_id": history.obligation_id,
        "created": history.created,
        "deduplicated": history.deduplicated,
        "timeline": [audit_event(e) for e in history.timeline],
        "state_changes": [plain(s) for s in history.state_changes],
        "actions": [plain(a) for a in history.actions],
        "diagnoses": [plain(d) for d in history.diagnoses],
        "policy_decisions": [plain(d) for d in history.policy_decisions],
        "reviews": [plain(d) for d in history.reviews],
        "verifications": [plain(d) for d in history.verifications],
        "provider_correlation_ids": list(history.provider_correlation_ids),
        "provider_references": list(history.provider_references),
        "workers": list(history.workers),
        "fencing_tokens": list(history.fencing_tokens),
        "stale_writes": [audit_event(e) for e in history.stale_writes],
        "unreconstructable": list(history.unreconstructable),
    }
