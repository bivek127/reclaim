"""What the runtime knows about each job, as data rather than as code.

A job is a row: its name, how often it wakes, which shape of loop it needs, and
which existing domain callable it drives. Keeping that declarative means adding
a job is a registration rather than a new loop, and means the set of jobs can be
inspected and tested without starting anything.

Intervals, lease durations and batch sizes are read from configuration at
registration time. Nothing in this module carries a schedule of its own: a
number written here would be a second source of truth competing with
`config/operational.yaml`.

Stage 1 registers no production jobs. The registry exists, is exercised, and is
ready for them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from reclaim.domain.states import CaseState


class JobKind(str, Enum):
    """Which runner shape a job needs."""

    BATCH = "batch"
    PER_CASE = "per_case"


class UnknownJob(LookupError):
    """Raised for a job name that is not registered."""


@dataclass(frozen=True)
class JobSpec:
    """One executable job.

    `connect` is the connection factory the job runs under, which is how the
    verifier's separate database role is expressed -- it is a property of the
    job, not something the runner decides.
    """

    name: str
    kind: JobKind
    interval_seconds: int
    operation: Callable[..., Any]
    connect: Callable[..., Any]
    #: per-case only: which state this job claims, and for how long
    expected_state: CaseState | None = None
    lease_seconds: int | None = None
    #: batch only: how many rows one pass may take
    limit: int | None = None

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError(f"{self.name}: interval_seconds must be positive")
        if self.kind is JobKind.PER_CASE:
            if self.expected_state is None or self.lease_seconds is None:
                raise ValueError(
                    f"{self.name}: a per-case job needs expected_state and lease_seconds"
                )
        elif self.limit is None:
            raise ValueError(f"{self.name}: a batch job needs a limit")


class JobRegistry:
    """A name → JobSpec mapping that refuses duplicates and unknown lookups."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobSpec] = {}

    def register(self, spec: JobSpec) -> JobSpec:
        if spec.name in self._jobs:
            raise ValueError(f"job {spec.name!r} is already registered")
        self._jobs[spec.name] = spec
        return spec

    def get(self, name: str) -> JobSpec:
        try:
            return self._jobs[name]
        except KeyError:
            known = ", ".join(sorted(self._jobs)) or "none registered"
            raise UnknownJob(f"unknown job {name!r}; known jobs: {known}") from None

    def names(self) -> list[str]:
        return sorted(self._jobs)

    def __len__(self) -> int:
        return len(self._jobs)

    def __contains__(self, name: object) -> bool:
        return name in self._jobs


#: The process-wide registry. Stage 1 leaves it empty on purpose: the jobs
#: themselves are later stages, and an entry here would be an unrunnable stub.
JOBS = JobRegistry()
