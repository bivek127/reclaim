"""Human-review races on real independent PostgreSQL connections."""

from __future__ import annotations

import threading
from typing import Any

import psycopg

from reclaim.domain.review import approve_review, expire_reviews, reject_review
from tests.domain.review_helpers import (
    actions_for,
    case_row,
    reviews_for,
    seed_escalated,
)


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


def test_two_concurrent_approvals(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    ids = seed_escalated(conn)

    def worker(own: psycopg.Connection, index: int):
        return approve_review(
            own,
            ids["case_id"],
            selected_action="CREATE_PAYMENT_LINK",
            reviewer_ref=f"rev-{index}",
            fencing_token=0,
            worker_id=f"rev-{index}",
        )

    results = _run_parallel(migrated_database, worker)
    applied = [
        r for kind, r in results if kind == "ok" and getattr(r, "applied", False)
    ]
    # Exactly one successful approve; the other is blocked / stale / ReviewBlocked.
    assert len(applied) == 1
    assert len(actions_for(conn, ids["case_id"])) == 1
    assert actions_for(conn, ids["case_id"])[0]["status"] == "PROPOSED"
    assert reviews_for(conn, ids["case_id"])[0]["status"] == "APPROVED"


def test_review_approval_races_ttl_expiry(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    """Approve + expire concurrently — exactly one wins."""
    ids = seed_escalated(conn)
    conn.execute(
        "UPDATE human_reviews SET review_expires_at = now() - interval '1 second' "
        "WHERE case_id = %s",
        (ids["case_id"],),
    )

    def worker(own: psycopg.Connection, index: int):
        if index == 0:
            try:
                return ("approve", approve_review(
                    own,
                    ids["case_id"],
                    selected_action="CREATE_PAYMENT_LINK",
                    reviewer_ref="alice",
                    fencing_token=0,
                ))
            except Exception as exc:  # noqa: BLE001
                return ("approve_err", exc)
        return ("expire", expire_reviews(own))

    results = _run_parallel(migrated_database, worker)
    assert all(kind == "ok" for kind, _ in results)

    payloads = [r for _, r in results]
    state = case_row(conn, ids["case_id"])["state"]
    rev = reviews_for(conn, ids["case_id"])[0]
    actions = actions_for(conn, ids["case_id"])

    if state == "EXPIRED_UNRESOLVED":
        assert rev["status"] == "EXPIRED"
        assert actions == []
        # Expiry won — approve must not have created an action after expiry.
    elif state == "ESCALATED":
        assert rev["status"] == "APPROVED"
        assert len(actions) == 1
        assert actions[0]["status"] == "PROPOSED"
    else:
        raise AssertionError(f"unexpected terminal race state {state} payloads={payloads}")


def test_approve_vs_reject_race(
    conn: psycopg.Connection, migrated_database: str
) -> None:
    ids = seed_escalated(conn)

    def worker(own: psycopg.Connection, index: int):
        if index == 0:
            try:
                return approve_review(
                    own,
                    ids["case_id"],
                    selected_action="CREATE_PAYMENT_LINK",
                    reviewer_ref="alice",
                    fencing_token=0,
                )
            except Exception as exc:  # noqa: BLE001
                return exc
        try:
            return reject_review(
                own, ids["case_id"], reviewer_ref="bob", fencing_token=0
            )
        except Exception as exc:  # noqa: BLE001
            return exc

    results = _run_parallel(migrated_database, worker)
    state = case_row(conn, ids["case_id"])["state"]
    assert state in {"ESCALATED", "VERIFIED_FAILED"}
    if state == "ESCALATED":
        assert reviews_for(conn, ids["case_id"])[0]["status"] == "APPROVED"
        assert len(actions_for(conn, ids["case_id"])) == 1
    else:
        assert reviews_for(conn, ids["case_id"])[0]["status"] == "REJECTED"
        assert actions_for(conn, ids["case_id"]) == []
