"""Loading the audit trail. The ONLY database access in this package.

Everything downstream of `load_case_audit_trail` is a pure transformation over
the rows returned here. Keeping the single query in its own module is what makes
the purity of `reconstruct.py` structurally checkable rather than a convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

# The whole trail for one case, ordered by occurred_at then id.
#
# Ordering note (forensic, not cosmetic): `occurred_at` defaults to now(), which
# in PostgreSQL is TRANSACTION START time -- every event written inside one
# transaction shares a timestamp and is separated only by `id`. The pair is
# therefore a stable, reproducible STORAGE order. It is not a proof of causal
# order across concurrent workers, whose clocks may disagree. The reconstruction
# preserves stored order and never re-derives causality.
_TRAIL_SQL = """
SELECT id, occurred_at, event_type, obligation_id, case_id, action_id,
       attempt_id, provider_request_id, worker_id, fencing_token,
       prev_state::text, new_state::text, reason_code, model, model_version,
       policy_version, reviewer_ref, provider_correlation_id, detail
  FROM audit_events
 WHERE case_id = %s
 ORDER BY occurred_at, id
"""


@dataclass(frozen=True)
class AuditEvent:
    """One immutable audit row. Mirrors `audit_events` exactly, nothing derived."""

    id: int
    occurred_at: datetime
    event_type: str
    obligation_id: int | None
    case_id: int | None
    action_id: int | None
    attempt_id: int | None
    provider_request_id: int | None
    worker_id: str | None
    fencing_token: int | None
    prev_state: str | None
    new_state: str | None
    reason_code: str | None
    model: str | None
    model_version: str | None
    policy_version: str | None
    reviewer_ref: str | None
    provider_correlation_id: str | None
    detail: dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        """Read a `detail` key. Never raises on a missing key."""
        return (self.detail or {}).get(key, default)


def load_case_audit_trail(
    conn: psycopg.Connection, case_id: int
) -> tuple[AuditEvent, ...]:
    """Every audit row for one case, in stored order.

    This is the entire database surface of reconstruction. Nothing else in
    this package touches a connection.
    """
    rows = conn.execute(_TRAIL_SQL, (case_id,)).fetchall()
    return tuple(
        AuditEvent(
            id=int(r[0]),
            occurred_at=r[1],
            event_type=str(r[2]),
            obligation_id=r[3],
            case_id=r[4],
            action_id=r[5],
            attempt_id=r[6],
            provider_request_id=r[7],
            worker_id=r[8],
            fencing_token=r[9],
            prev_state=r[10],
            new_state=r[11],
            reason_code=r[12],
            model=r[13],
            model_version=r[14],
            policy_version=r[15],
            reviewer_ref=r[16],
            provider_correlation_id=r[17],
            detail=r[18] or {},
        )
        for r in rows
    )
