"""Fixtures for simulator tests."""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from reclaim.config import SimulatorConfig
from tests.db.helpers import insert_case, insert_obligation

_SEQ = itertools.count(1)

BASELINE = 0.20
UPLIFT = 0.15


def sim_config(**overrides: Any) -> SimulatorConfig:
    """A fully-populated config for tests.

    These numbers are TEST FIXTURES with a fixture citation. They are not
    empirical values and must never be copied into config/simulator.yaml, which
    deliberately ships unset -- real values must be externally sourced and cited.
    """
    values: dict[str, Any] = {
        "seed": 12345,
        "n_per_arm": 3,
        "model": "baseline_plus_uplift",
        "organic_baseline_rate": BASELINE,
        "organic_baseline_source": "TEST FIXTURE - not an empirical value",
        "action_params": {"CREATE_PAYMENT_LINK": UPLIFT},
        "action_sources": {"CREATE_PAYMENT_LINK": "TEST FIXTURE - not empirical"},
        "amount_band_bounds": (50_000, 200_000),
        "feature_timezone": "UTC",
        "history_window_days": 30,
    }
    values.update(overrides)
    return SimulatorConfig(**values)


def seed_corpus(
    conn: psycopg.Connection,
    count: int = 5,
    *,
    state: str = "AWAITING_CUSTOMER",
    amount_minor: int = 10_000,
    with_failure_code: str | None = "BAD_REQUEST_ERROR",
) -> list[int]:
    """Real cases created through the ordinary tables, as ingest would leave them."""
    case_ids = []
    for _ in range(count):
        n = next(_SEQ)
        anchor = f"order:ord_s{n}"
        obligation_id = insert_obligation(
            conn,
            anchor_key=f"ord_s{n}",
            anchor_canonical=anchor,
            amount_minor=amount_minor,
            customer_ref=f"cust_s{n}",
            source_event_id=f"evt_s{n}",
        )
        case_ids.append(insert_case(conn, obligation_id, state=state))
        if with_failure_code:
            _webhook(conn, anchor, n, with_failure_code)
    return case_ids


def _webhook(conn: psycopg.Connection, anchor: str, n: int, code: str) -> None:
    conn.execute(
        """
        INSERT INTO webhook_events (
            provider_event_id, event_type, signature_valid, resolution,
            anchor_canonical, payload
        ) VALUES (%s, 'payment.failed', true, 'RESOLVED', %s, %s)
        """,
        (
            f"evt_wh_s{n}",
            anchor,
            Jsonb(
                {
                    "event": "payment.failed",
                    "payload": {
                        "payment": {
                            "entity": {
                                "id": f"pay_{n}",
                                "error_code": code,
                                "amount": 10_000,
                            }
                        }
                    },
                }
            ),
        ),
    )


def add_diagnosis(
    conn: psycopg.Connection,
    case_id: int,
    *,
    cause: str = "INSUFFICIENT_FUNDS",
    confidence: float | None = 0.90,
    reasoning: str | None = "because",
    source: str = "LLM",
    model: str | None = "test-model",
) -> int:
    """Agent-generated diagnosis rows -- I11 says none of this may matter."""
    row = conn.execute(
        """
        INSERT INTO diagnoses (
            case_id, source, model, prompt_version, cause,
            recommended_action, reasoning, confidence
        ) VALUES (%s, %s, %s, 'v1', %s, 'CREATE_PAYMENT_LINK', %s, %s)
        RETURNING id
        """,
        (case_id, source, model, cause, reasoning, confidence),
    ).fetchone()
    assert row is not None
    return int(row[0])


def case_snapshot(conn: psycopg.Connection) -> list[tuple[Any, ...]]:
    """Everything about recovery_cases that a simulator must never change."""
    return conn.execute(
        """
        SELECT id, state::text, attempt_count, recovered_amount_minor,
               worker_id, lease_expires_at::text, fencing_token,
               active_since::text, active_elapsed_ms, updated_at::text
          FROM recovery_cases ORDER BY id
        """
    ).fetchall()


def table_counts(conn: psycopg.Connection) -> dict[str, int]:
    counts = {}
    for table in (
        "recovery_cases",
        "recovery_actions",
        "execution_attempts",
        "provider_requests",
        "verifications",
        "human_reviews",
        "audit_events",
    ):
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
        counts[table] = int(row[0])
    return counts
