"""The `--job` entrypoint, and where the runtime's schedule comes from."""

from __future__ import annotations

import pathlib
import re

import pytest

from reclaim.config import OPERATIONAL_PATH, load_operational
from reclaim.domain.states import CaseState
from reclaim.jobs.__main__ import main, start
from reclaim.jobs.registry import JobKind, JobRegistry, JobSpec

INTERVAL_KEYS = [
    "case_worker_interval_seconds",
    "executor_interval_seconds",
    "verifier_interval_seconds",
    "review_expiry_interval_seconds",
    "action_deadline_expiry_interval_seconds",
    "breaker_monitor_interval_seconds",
]


# ---- configuration --------------------------------------------------------


def test_every_job_interval_is_read_from_the_config_file() -> None:
    """A schedule written in Python would compete with operational.yaml."""
    text = OPERATIONAL_PATH.read_text()
    for key in INTERVAL_KEYS:
        assert f"{key}:" in text, f"{key} is missing from operational.yaml"

    values = load_operational()
    assert values["case_worker_interval_seconds"] == 5
    assert values["executor_interval_seconds"] == 5
    assert values["verifier_interval_seconds"] == 15
    assert values["review_expiry_interval_seconds"] == 60
    assert values["action_deadline_expiry_interval_seconds"] == 60
    assert values["breaker_monitor_interval_seconds"] == 10


def test_an_edited_config_file_changes_the_intervals(tmp_path: pathlib.Path) -> None:
    """Proves the values are loaded, not merely duplicated as defaults."""
    edited = tmp_path / "operational.yaml"
    edited.write_text("executor_interval_seconds: 11\n")

    assert load_operational(edited)["executor_interval_seconds"] == 11


def test_the_loop_primitives_carry_no_schedule() -> None:
    """runner.py and registry.py take an interval; they must not know any.

    Registration is where configuration is read, so naming a key there is
    correct. Naming one in the loop itself would mean the runner had opinions
    about how often a job should run.
    """
    root = pathlib.Path(__file__).resolve().parents[2] / "reclaim" / "jobs"
    for module in ("runner.py", "registry.py"):
        text = (root / module).read_text()
        for key in INTERVAL_KEYS:
            assert key not in text, f"{module} names a config key"


def test_no_runtime_module_assigns_a_literal_interval() -> None:
    """An interval literal would be a schedule competing with the config file."""
    root = pathlib.Path(__file__).resolve().parents[2] / "reclaim" / "jobs"
    pattern = re.compile(r"interval_seconds\s*=\s*\d+")
    for module in root.glob("*.py"):
        found = pattern.findall(module.read_text())
        assert not found, f"{module.name} hardcodes an interval: {found}"


# ---- entrypoint -----------------------------------------------------------


def spec(name: str, kind: JobKind, started: list[str]) -> JobSpec:
    common = dict(
        name=name, kind=kind, interval_seconds=5,
        connect=lambda: None, operation=lambda *a, **k: started.append(name),
    )
    if kind is JobKind.PER_CASE:
        return JobSpec(**common, expected_states=(CaseState.NEW,), lease_seconds=30)
    return JobSpec(**common, limit=100)


def test_the_entrypoint_runs_the_job_it_was_asked_for(monkeypatch) -> None:
    started: list[str] = []
    registry = JobRegistry()
    registry.register(spec("sweep", JobKind.BATCH, started))
    registry.register(spec("execute", JobKind.PER_CASE, started))

    chosen: list[str] = []
    monkeypatch.setattr(
        "reclaim.jobs.__main__.start", lambda s: chosen.append(s.name)
    )

    assert main(["--job", "execute"], registry=registry) == 0
    assert chosen == ["execute"]


def test_an_unknown_job_exits_non_zero_without_starting_anything(capsys) -> None:
    started: list[str] = []
    registry = JobRegistry()
    registry.register(spec("sweep", JobKind.BATCH, started))

    assert main(["--job", "nope"], registry=registry) == 2
    assert started == [], "nothing ran"
    assert "nope" in capsys.readouterr().err


def test_listing_jobs_starts_nothing(capsys) -> None:
    started: list[str] = []
    registry = JobRegistry()
    registry.register(spec("sweep", JobKind.BATCH, started))

    assert main(["--job", "sweep", "--list"], registry=registry) == 0
    assert "sweep" in capsys.readouterr().out
    assert started == []


def test_start_dispatches_each_kind_to_its_own_runner(monkeypatch) -> None:
    """A batch job must not be driven through the claiming loop, or vice versa."""
    calls: list[str] = []
    monkeypatch.setattr(
        "reclaim.jobs.__main__.run_batch", lambda **kw: calls.append("batch")
    )
    monkeypatch.setattr(
        "reclaim.jobs.__main__.run_per_case", lambda **kw: calls.append("per_case")
    )

    start(spec("sweep", JobKind.BATCH, []))
    start(spec("execute", JobKind.PER_CASE, []))

    assert calls == ["batch", "per_case"]


# ---- the runtime introduces no scheduler ----------------------------------


def test_the_runtime_depends_on_no_task_framework() -> None:
    """PostgreSQL's SKIP LOCKED is the queue; nothing else is required."""
    banned = (
        "celery", "redis", "kafka", "apscheduler", "rq", "dramatiq",
        "huey", "kubernetes", "sqlalchemy", "asyncio", "threading",
        "multiprocessing", "schedule",
    )
    root = pathlib.Path(__file__).resolve().parents[2] / "reclaim" / "jobs"
    for module in root.glob("*.py"):
        text = module.read_text().lower()
        for name in banned:
            assert f"import {name}" not in text, f"{module.name} imports {name}"
            assert f"from {name}" not in text, f"{module.name} imports from {name}"


# ---- the runtime never owns breaker state ---------------------------------


def test_no_runtime_module_writes_the_breaker_table() -> None:
    """`set_breaker_state` must stay the single mutation path.

    Checked structurally as well as behaviourally: a SELECT would already be a
    layering violation, and an UPDATE would be a second authority.
    """
    root = pathlib.Path(__file__).resolve().parents[2] / "reclaim" / "jobs"
    forbidden = re.compile(
        r"update\s+circuit_breaker|insert\s+into\s+circuit_breaker"
        r"|from\s+circuit_breaker|reset_after\s*=",
        re.IGNORECASE,
    )
    for module in root.glob("*.py"):
        text = module.read_text()
        # Strip comments and docstrings: prose may name the table it must not touch.
        code = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith(("#", "*"))
        )
        found = forbidden.findall(code)
        assert not found, f"{module.name} touches circuit_breaker directly: {found}"


def test_no_runtime_module_executes_sql() -> None:
    """Orchestration calls domain functions; it does not query."""
    root = pathlib.Path(__file__).resolve().parents[2] / "reclaim" / "jobs"
    for module in root.glob("*.py"):
        code = "\n".join(
            line for line in module.read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "conn.execute(" not in code, f"{module.name} runs SQL"
        assert "cursor(" not in code, f"{module.name} opens a cursor"


def test_the_case_worker_is_not_registered() -> None:
    """Deliberately absent: no domain function defines the NEW-to-POLICY_EVAL
    sequence, and `conflicting_history` has no schema mapping."""
    from reclaim.jobs.jobs import register_all_jobs
    from reclaim.jobs.registry import JobRegistry

    names = register_all_jobs(JobRegistry()).names()
    assert "case-worker" not in names
    assert names == [
        "action-deadline-expiry",
        "breaker-monitor",
        "executor",
        "reconciler",
        "review-expiry",
        "sweeper",
        "ttl-expiry",
        "verifier",
    ]

    root = pathlib.Path(__file__).resolve().parents[2] / "reclaim" / "jobs"
    registered = (root / "jobs.py").read_text() + (root / "percase.py").read_text()
    for absent in ("diagnose_case", "apply_policy", "ingest_webhook", "notifier"):
        assert absent not in registered, f"the runtime wires {absent}"
