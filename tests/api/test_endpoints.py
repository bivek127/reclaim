"""HTTP boundary behaviour, exercised over real HTTP against a real server.

The API is an adapter: it must validate input, delegate to one existing
operation, and translate the outcome without changing its meaning. These tests
pin that contract, including the refusals a reviewer can actually hit.

A real uvicorn process is used rather than an in-process test client so that
ASGI dispatch, connection handling, and status codes are all genuinely
exercised.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Iterator
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json as _json

import psycopg
import pytest

from reclaim.domain.states import CaseState
from tests.domain.review_helpers import seed_escalated
from tests.domain.verification_helpers import seed_awaiting_customer


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class ApiClient:
    """Minimal HTTP client returning (status, body) without raising on 4xx."""

    def __init__(self, base: str) -> None:
        self.base = base

    def _call(self, method: str, path: str, payload: dict | None = None):
        data = _json.dumps(payload).encode() if payload is not None else None
        req = Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urlopen(req, timeout=10) as r:
                return r.status, _json.loads(r.read() or b"null")
        except HTTPError as e:
            return e.code, _json.loads(e.read() or b"null")

    def get(self, path: str):
        return self._call("GET", path)

    def post(self, path: str, payload: dict):
        return self._call("POST", path, payload)


@pytest.fixture(scope="session")
def api_server(migrated_database: str) -> Iterator[ApiClient]:
    import uvicorn

    from reclaim.api import db as api_db

    # Point the adapter at the migrated test database before the app loads.
    api_db.APP_URL = migrated_database
    from reclaim.api.main import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 15
    client = ApiClient(f"http://127.0.0.1:{port}")
    while time.time() < deadline:
        try:
            if client.get("/api/health")[0] == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError("API server did not start")

    yield client
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def client(api_server: ApiClient, conn: psycopg.Connection) -> ApiClient:
    """Server plus a freshly truncated database for each test."""
    return api_server


def test_health_and_meta_expose_domain_vocabulary(client: TestClient) -> None:
    assert client.get("/api/health")[1]["status"] == "ok"
    meta = client.get("/api/meta")[1]
    assert CaseState.VERIFIED_RECOVERED.value in meta["case_states"]
    # Only dispatchable actions may ever be offered to a reviewer.
    assert meta["reviewable_actions"] == ["CREATE_PAYMENT_LINK"]
    assert "RETRY_CHARGE" not in meta["reviewable_actions"]


def test_overview_on_empty_database(client: TestClient) -> None:
    body = client.get("/api/overview")[1]
    assert body["attention_total"] == 0
    assert body["recovered_amount_minor"] == 0


def test_cases_listing_and_filters(client: TestClient,
                                   conn: psycopg.Connection) -> None:
    seed_awaiting_customer(conn, suffix="api1")
    seed_escalated(conn)
    body = client.get("/api/cases")[1]
    assert body["total"] == 2
    attention = client.get("/api/cases?needs_attention=true")[1]
    assert attention["total"] == 1


def test_unknown_state_filter_is_rejected(client: TestClient) -> None:
    status, body = client.get("/api/cases?state=NOT_A_STATE")
    assert status == 400
    assert "NOT_A_STATE" in body["detail"]


def test_case_detail_and_missing_case(client: TestClient,
                                      conn: psycopg.Connection) -> None:
    ids = seed_escalated(conn)
    body = client.get(f"/api/cases/{ids['case_id']}")[1]
    assert body["case"]["state"] == CaseState.ESCALATED.value
    assert body["case"]["amount_minor"] == body["obligation"]["amount_minor"]
    assert client.get("/api/cases/999999")[0] == 404


def test_timeline_is_reconstructed_from_the_audit_trail(
    client: TestClient, conn: psycopg.Connection
) -> None:
    ids = seed_escalated(conn)
    body = client.get(f"/api/cases/{ids['case_id']}/timeline")[1]
    assert body["case_id"] == ids["case_id"]
    assert len(body["timeline"]) >= 1
    # Gaps are reported rather than papered over with a production-table join.
    assert isinstance(body["unreconstructable"], list)
    assert {"prev_state", "new_state", "reason_code"} <= set(
        body["state_changes"][0]
    )


def test_timeline_for_missing_case_is_404(client: TestClient) -> None:
    assert client.get("/api/cases/999999/timeline")[0] == 404


def test_review_queue_and_evidence(client: TestClient,
                                   conn: psycopg.Connection) -> None:
    ids = seed_escalated(conn)
    queue = client.get("/api/reviews?status=PENDING")[1]
    assert queue["total"] == 1
    evidence = client.get(f"/api/reviews/{ids['case_id']}")[1]
    assert evidence["case_state"] == CaseState.ESCALATED.value
    assert evidence["reviewable_actions"] == ["CREATE_PAYMENT_LINK"]


def test_approve_requires_a_dispatchable_action(client: TestClient,
                                                conn: psycopg.Connection) -> None:
    ids = seed_escalated(conn)
    status, _ = client.post(
        f"/api/reviews/{ids['case_id']}/approve",
        {"reviewer_ref": "ops@example.com", "selected_action": "RETRY_CHARGE"},
    )
    assert status == 400


def test_approve_requires_a_reviewer(client: TestClient,
                                     conn: psycopg.Connection) -> None:
    ids = seed_escalated(conn)
    status, _ = client.post(
        f"/api/reviews/{ids['case_id']}/approve",
        {"reviewer_ref": "", "selected_action": "CREATE_PAYMENT_LINK"},
    )
    assert status == 422


def test_reject_moves_case_to_verified_failed(client: TestClient,
                                              conn: psycopg.Connection) -> None:
    ids = seed_escalated(conn)
    status, body = client.post(f"/api/reviews/{ids['case_id']}/reject",
                               {"reviewer_ref": "ops@example.com"})
    assert status == 200
    assert body["case_state"] == CaseState.VERIFIED_FAILED.value
    state = conn.execute(
        "SELECT state::text FROM recovery_cases WHERE id = %s", (ids["case_id"],)
    ).fetchone()[0]
    assert state == CaseState.VERIFIED_FAILED.value


def test_approve_creates_a_proposed_action_and_leaves_the_case_escalated(
    client: TestClient, conn: psycopg.Connection
) -> None:
    """Approval proposes work; the executor remains the only path to money."""
    ids = seed_escalated(conn)
    status, body = client.post(
        f"/api/reviews/{ids['case_id']}/approve",
        {"reviewer_ref": "ops@example.com",
         "selected_action": "CREATE_PAYMENT_LINK"},
    )
    assert status == 200
    assert body["case_state"] == CaseState.ESCALATED.value
    action = conn.execute(
        "SELECT status::text, action_type::text FROM recovery_actions "
        "WHERE case_id = %s AND status = 'PROPOSED'", (ids["case_id"],)
    ).fetchone()
    assert action == ("PROPOSED", "CREATE_PAYMENT_LINK")
    attempts = conn.execute(
        "SELECT count(*) FROM execution_attempts WHERE case_id = %s",
        (ids["case_id"],),
    ).fetchone()[0]
    assert attempts == 0, "review must never create an execution attempt"


def test_deciding_twice_is_refused_with_conflict(client: TestClient,
                                                 conn: psycopg.Connection) -> None:
    """The second decision gets the domain's refusal, not a faked success."""
    ids = seed_escalated(conn)
    first_status, _ = client.post(f"/api/reviews/{ids['case_id']}/reject",
                                  {"reviewer_ref": "ops@example.com"})
    assert first_status == 200
    second_status, second_body = client.post(
        f"/api/reviews/{ids['case_id']}/reject",
        {"reviewer_ref": "ops@example.com"})
    assert second_status == 409
    assert "awaiting review" in second_body["detail"]


def test_decision_releases_the_console_lease(client: TestClient,
                                             conn: psycopg.Connection) -> None:
    """A held lease would block background workers on that case."""
    ids = seed_escalated(conn)
    client.post(f"/api/reviews/{ids['case_id']}/reject",
                {"reviewer_ref": "ops@example.com"})
    holder = conn.execute(
        "SELECT worker_id FROM recovery_cases WHERE id = %s", (ids["case_id"],)
    ).fetchone()[0]
    assert holder is None


def test_review_on_non_escalated_case_conflicts(client: TestClient,
                                                conn: psycopg.Connection) -> None:
    ids = seed_awaiting_customer(conn, suffix="api2")
    status, _ = client.post(f"/api/reviews/{ids['case_id']}/reject",
                            {"reviewer_ref": "ops@example.com"})
    assert status == 409


def test_system_status_endpoint(client: TestClient) -> None:
    body = client.get("/api/system")[1]
    assert body["breaker"]["state"] in {"CLOSED", "OPEN"}
