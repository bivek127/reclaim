"""Diagnosis races on real independent PostgreSQL connections."""

from __future__ import annotations

import threading
from typing import Any

import psycopg

from reclaim.domain.diagnosis import diagnose_case
from reclaim.llm.client import UnreachableLlm
from tests.domain.diagnosis_helpers import case_row, diagnoses_for, seed_diagnosing


def _run_parallel(dsn: str, worker, count: int = 2) -> list[Any]:
    results: list[Any] = [None] * count
    barrier = threading.Barrier(count)

    def target(index: int) -> None:
        with psycopg.connect(dsn, autocommit=True) as own:
            barrier.wait()
            try:
                results[index] = ("ok", worker(own, index))
            except Exception as exc:  # noqa: BLE001
                results[index] = ("err", exc)

    threads = [threading.Thread(target=target, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads)
    return results


def test_concurrent_diagnose_writes_one_row(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    ids = seed_diagnosing(conn)

    def worker(own: psycopg.Connection, index: int):
        return diagnose_case(
            own,
            ids["case_id"],
            llm=UnreachableLlm(),
            fencing_token=0,
            worker_id=f"dx-{index}",
        )

    results = _run_parallel(migrated_database, worker)
    assert all(kind == "ok" for kind, _ in results)
    applied = [r for kind, r in results if kind == "ok" and r.applied]
    rejected = [r for kind, r in results if kind == "ok" and not r.applied]
    assert len(applied) == 1
    assert len(rejected) == 1
    assert len(diagnoses_for(conn, ids["case_id"])) == 1
    assert case_row(conn, ids["case_id"])["state"] == "POLICY_EVAL"
