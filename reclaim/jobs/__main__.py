"""Process entrypoint: `python -m reclaim.jobs --job <name>`.

One process runs one job. Which jobs share a process is a deployment question,
and answering it here would bake an operational choice into the code.

The entrypoint resolves a name against the registry and starts the matching
runner. It contains no schedule and no domain logic of its own.
"""

from __future__ import annotations

import argparse
import logging
import sys

from reclaim.jobs.jobs import register_all_jobs
from reclaim.jobs.registry import JOBS, JobKind, JobRegistry, JobSpec, UnknownJob
from reclaim.jobs.runner import run_batch, run_per_case


def build_parser(registry: JobRegistry) -> argparse.ArgumentParser:
    known = ", ".join(registry.names()) or "none registered yet"
    parser = argparse.ArgumentParser(
        prog="python -m reclaim.jobs",
        description="Run one background job until interrupted.",
    )
    parser.add_argument("--job", required=True, help=f"job to run. Known: {known}")
    parser.add_argument(
        "--list", action="store_true", help="print registered jobs and exit"
    )
    return parser


def start(spec: JobSpec) -> None:
    """Dispatch a registered job to the runner its kind requires."""
    if spec.kind is JobKind.BATCH:
        run_batch(
            name=spec.name,
            connect=spec.connect,
            operation=spec.operation,
            interval_seconds=spec.interval_seconds,
            limit=spec.limit,  # type: ignore[arg-type]  # validated in JobSpec
        )
    else:
        run_per_case(
            name=spec.name,
            connect=spec.connect,
            operation=spec.operation,
            expected_state=spec.expected_state,  # type: ignore[arg-type]
            worker_id=spec.name,
            lease_seconds=spec.lease_seconds,  # type: ignore[arg-type]
            interval_seconds=spec.interval_seconds,
        )


def main(argv: list[str] | None = None, registry: JobRegistry = JOBS) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Populated here rather than at import, so importing the package neither
    # reads configuration nor pins a registry a test may want to supply itself.
    if registry is JOBS and len(registry) == 0:
        register_all_jobs(registry)

    args = build_parser(registry).parse_args(argv)

    if args.list:
        print("\n".join(registry.names()) or "no jobs registered")
        return 0

    try:
        spec = registry.get(args.job)
    except UnknownJob as exc:
        print(str(exc), file=sys.stderr)
        return 2

    start(spec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
