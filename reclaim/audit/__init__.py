"""Forensic audit read model.

The only supported way to reconstruct a case's history. A timeline UI must
read this table and nothing else -- so this package reads `audit_events` and
nothing else, and the future UI consumes `CaseAuditHistory` rather than
joining production tables itself.
"""

from reclaim.audit.events import AuditEvent, load_case_audit_trail
from reclaim.audit.reconstruct import (
    CaseAuditHistory,
    ReconstructedAction,
    ReconstructedAttempt,
    reconstruct_case_history,
)

__all__ = [
    "AuditEvent",
    "load_case_audit_trail",
    "CaseAuditHistory",
    "ReconstructedAction",
    "ReconstructedAttempt",
    "reconstruct_case_history",
]
