"""The job registry: names, validation, and lookup."""

from __future__ import annotations

import pytest

from reclaim.domain.states import CaseState
from reclaim.jobs.registry import JOBS, JobKind, JobRegistry, JobSpec, UnknownJob


def batch_spec(name: str = "sweep", **over) -> JobSpec:
    values = dict(
        name=name, kind=JobKind.BATCH, interval_seconds=15,
        operation=lambda conn, limit: None, connect=lambda: None, limit=100,
    )
    values.update(over)
    return JobSpec(**values)


def per_case_spec(name: str = "execute", **over) -> JobSpec:
    values = dict(
        name=name, kind=JobKind.PER_CASE, interval_seconds=5,
        operation=lambda conn, cid, *, fencing_token: None, connect=lambda: None,
        expected_states=(CaseState.ACTION_READY,), lease_seconds=60,
    )
    values.update(over)
    return JobSpec(**values)


def test_an_unknown_job_name_is_refused_by_name(caplog) -> None:
    registry = JobRegistry()
    registry.register(batch_spec("sweep"))

    with pytest.raises(UnknownJob) as excinfo:
        registry.get("sweeeper")

    message = str(excinfo.value)
    assert "sweeeper" in message
    assert "sweep" in message, "the error should name what is available"


def test_a_duplicate_registration_is_refused() -> None:
    """Two jobs under one name would make `--job` ambiguous."""
    registry = JobRegistry()
    registry.register(batch_spec("sweep"))

    with pytest.raises(ValueError, match="already registered"):
        registry.register(batch_spec("sweep"))


def test_a_per_case_job_must_declare_what_it_claims_and_for_how_long() -> None:
    with pytest.raises(ValueError, match="expected_states and lease_seconds"):
        per_case_spec(expected_states=None)
    with pytest.raises(ValueError, match="expected_states and lease_seconds"):
        per_case_spec(lease_seconds=None)


def test_a_batch_job_must_declare_a_limit() -> None:
    """An unbounded batch pass could hold a transaction open arbitrarily long."""
    with pytest.raises(ValueError, match="needs a limit"):
        batch_spec(limit=None)


def test_a_non_positive_interval_is_refused() -> None:
    for bad in (0, -5):
        with pytest.raises(ValueError, match="interval_seconds must be positive"):
            batch_spec(interval_seconds=bad)


def test_the_registry_reports_what_it_holds() -> None:
    registry = JobRegistry()
    assert registry.names() == [] and len(registry) == 0

    registry.register(batch_spec("sweep"))
    registry.register(per_case_spec("execute"))

    assert registry.names() == ["execute", "sweep"]
    assert "sweep" in registry and "nope" not in registry
    assert len(registry) == 2


def test_importing_the_package_registers_nothing_by_itself() -> None:
    """Registration is an explicit call, so importing reads no configuration
    and cannot surprise a test that supplies its own registry."""
    import subprocess
    import sys

    probe = subprocess.run(
        [sys.executable, "-c",
         "import reclaim.jobs as j; print(len(j.JOBS))"],
        capture_output=True, text=True, check=True,
    )
    assert probe.stdout.strip() == "0"
