"""Background job runtime: polling loops over the existing domain operations.

This package schedules; it does not decide. Every state change belongs to the
domain function a runner calls, and every lease belongs to the domain's own
claim and release primitives.
"""

from reclaim.jobs.breaker import monitor_breaker
from reclaim.jobs.jobs import (
    ACTION_DEADLINE_EXPIRY,
    BREAKER_MONITOR,
    CASE_WORKER,
    DIAGNOSIS,
    POLICY,
    REVIEW_EXPIRY,
    SWEEPER,
    TTL_EXPIRY,
    EXECUTOR,
    RECONCILER,
    VERIFIER,
    register_all_jobs,
    register_batch_jobs,
    register_per_case_jobs,
)
from reclaim.jobs.registry import JOBS, JobKind, JobRegistry, JobSpec, UnknownJob
from reclaim.jobs.runner import Tick, at_most, run_batch, run_per_case

__all__ = [
    "ACTION_DEADLINE_EXPIRY",
    "BREAKER_MONITOR",
    "CASE_WORKER",
    "DIAGNOSIS",
    "JOBS",
    "POLICY",
    "REVIEW_EXPIRY",
    "SWEEPER",
    "TTL_EXPIRY",
    "JobKind",
    "JobRegistry",
    "JobSpec",
    "Tick",
    "UnknownJob",
    "at_most",
    "monitor_breaker",
    "run_batch",
    "EXECUTOR",
    "RECONCILER",
    "VERIFIER",
    "register_all_jobs",
    "register_batch_jobs",
    "register_per_case_jobs",
    "run_per_case",
]
