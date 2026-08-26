"""Verification races on real, independent PostgreSQL connections.

Every test opens genuinely separate `recovery_verifier` connections in separate
threads. Sequential "then the other worker runs" is not a concurrency test.
"""

from __future__ import annotations

import threading
from typing import Any

import psycopg

from reclaim.domain.leases import claim_case
from reclaim.domain.states import CaseState
from reclaim.domain.verification import verify_case, verify_once
from tests.conftest import TEST_DB_NAME, VERIFIER_PASSWORD, _role_database_url
from tests.domain.verification_helpers import (
    AMOUNT,
    StubVerifyProvider,
    case_row,
    deliver_webhook,
    fetch_paid,
    seed_awaiting_customer,
    verifications_for,
)


def _verifier_dsn() -> str:
    return _role_database_url(TEST_DB_NAME, "recovery_verifier", VERIFIER_PASSWORD)


def _run_parallel(dsn: str, worker, count: int = 2) -> list[Any]:
    results: list[Any] = [None] * count
    barrier = threading.Barrier(count)

    def target(index: int) -> None:
        with psycopg.connect(dsn, autocommit=True) as own:
            barrier.wait()
            try:
                results[index] = ("ok", worker(own, index))
            except Exception as exc:  # noqa: BLE001 - recorded, then asserted
                results[index] = ("err", exc)

    threads = [threading.Thread(target=target, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads), "a worker thread deadlocked"
    return results


def _agreeing(conn: psycopg.Connection, case_id: int) -> list[dict[str, Any]]:
    return [row for row in verifications_for(conn, case_id) if row["agrees"]]


def test_two_verifiers_recognize_revenue_exactly_once(
    conn: psycopg.Connection,
) -> None:
    """Worker A and Worker B both attempt verification; one financial outcome."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)

    def worker(own: psycopg.Connection, index: int):
        return verify_case(
            own,
            ids["case_id"],
            provider=StubVerifyProvider(fetch_paid()),
            fencing_token=0,
            worker_id=f"verifier-{index}",
        )

    results = _run_parallel(_verifier_dsn(), worker)
    assert all(kind == "ok" for kind, _ in results)

    recovered = [result for kind, result in results if kind == "ok" and result.recovered]
    applied = [result for kind, result in results if kind == "ok" and result.applied]
    assert len(recovered) == 1
    assert len(applied) == 1

    row = case_row(conn, ids["case_id"])
    assert row["state"] == "VERIFIED_RECOVERED"
    assert row["revenue"] == AMOUNT
    assert len(_agreeing(conn, ids["case_id"])) == 1
    assert len(verifications_for(conn, ids["case_id"])) == 1


def test_stale_fencing_token_cannot_overwrite_revenue(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """A worker holding an old token cannot beat the current lease holder."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)
    claim = claim_case(
        verifier_conn, ids["case_id"], CaseState.AWAITING_CUSTOMER, "holder", 45
    )
    assert claim is not None
    current = claim.fencing_token

    def worker(own: psycopg.Connection, index: int):
        token = current if index == 0 else 0
        return verify_case(
            own,
            ids["case_id"],
            provider=StubVerifyProvider(fetch_paid()),
            fencing_token=token,
            worker_id=f"fenced-{index}",
        )

    results = _run_parallel(_verifier_dsn(), worker)
    assert all(kind == "ok" for kind, _ in results)

    recovered = [result for kind, result in results if kind == "ok" and result.recovered]
    assert len(recovered) == 1
    assert recovered[0].reason == "verification_agreed"

    row = case_row(conn, ids["case_id"])
    assert row["state"] == "VERIFIED_RECOVERED"
    assert row["revenue"] == AMOUNT
    assert row["fencing_token"] == current
    assert len(_agreeing(conn, ids["case_id"])) == 1

    stale = conn.execute(
        "SELECT count(*) FROM audit_events WHERE case_id = %s "
        "AND reason_code = 'stale_write_rejected'",
        (ids["case_id"],),
    ).fetchone()
    assert stale is not None and stale[0] >= 1


def test_concurrent_duplicate_webhooks_do_not_double_count(
    conn: psycopg.Connection,
) -> None:
    """Two correlated SUCCESS webhooks cannot produce a second recognition."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids, provider_event_id="evt_conc_a")
    deliver_webhook(conn, ids, provider_event_id="evt_conc_b")

    def worker(own: psycopg.Connection, index: int):
        return verify_case(
            own,
            ids["case_id"],
            provider=StubVerifyProvider(fetch_paid()),
            fencing_token=0,
            worker_id=f"dup-{index}",
        )

    results = _run_parallel(_verifier_dsn(), worker)
    assert all(kind == "ok" for kind, _ in results)

    recovered = [result for kind, result in results if kind == "ok" and result.recovered]
    assert len(recovered) == 1
    assert case_row(conn, ids["case_id"])["revenue"] == AMOUNT
    assert len(_agreeing(conn, ids["case_id"])) == 1


def test_second_verification_after_recovered_inserts_no_agreeing_row(
    conn: psycopg.Connection, verifier_conn: psycopg.Connection
) -> None:
    """VERIFIED_RECOVERED cannot be verified again, even by concurrent workers."""
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)
    first = verify_case(
        verifier_conn,
        ids["case_id"],
        provider=StubVerifyProvider(fetch_paid()),
        fencing_token=0,
        worker_id="first",
    )
    assert first.recovered is True

    def worker(own: psycopg.Connection, index: int):
        return verify_case(
            own,
            ids["case_id"],
            provider=StubVerifyProvider(fetch_paid()),
            fencing_token=0,
            worker_id=f"late-{index}",
        )

    results = _run_parallel(_verifier_dsn(), worker)
    assert all(kind == "ok" for kind, _ in results)
    assert all(not result.recovered for kind, result in results if kind == "ok")
    assert all(not result.applied for kind, result in results if kind == "ok")

    assert case_row(conn, ids["case_id"])["revenue"] == AMOUNT
    assert len(_agreeing(conn, ids["case_id"])) == 1
    assert len(verifications_for(conn, ids["case_id"])) == 1


def test_concurrent_verify_once_claims_one_case(
    conn: psycopg.Connection,
) -> None:
    ids = seed_awaiting_customer(conn)
    deliver_webhook(conn, ids)

    def worker(own: psycopg.Connection, index: int):
        return verify_once(
            own,
            provider=StubVerifyProvider(fetch_paid()),
            worker_id=f"once-{index}",
        )

    results = _run_parallel(_verifier_dsn(), worker)
    assert all(kind == "ok" for kind, _ in results)

    outcomes = [result for kind, result in results if kind == "ok"]
    claimed = [result for result in outcomes if result is not None]
    assert len(claimed) == 1
    assert claimed[0].recovered is True
    assert case_row(conn, ids["case_id"])["revenue"] == AMOUNT
    assert len(_agreeing(conn, ids["case_id"])) == 1
