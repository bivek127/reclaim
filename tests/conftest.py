"""Pytest fixtures for real PostgreSQL constraint tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from urllib.parse import quote, urlparse, urlunparse

import psycopg
import pytest

from scripts.apply_migrations import apply_migrations

ADMIN_URL = os.environ.get(
    "TEST_DATABASE_ADMIN_URL",
    "postgresql://postgres@localhost:5432/postgres",
)
TEST_DB_NAME = os.environ.get("TEST_DATABASE_NAME", "reclaim_test")
APP_PASSWORD = "recovery_app_test"
VERIFIER_PASSWORD = "recovery_verifier_test"

TRUNCATE_SQL = """
TRUNCATE TABLE
  sim_outcomes,
  sim_runs,
  audit_events,
  human_reviews,
  verifications,
  provider_requests,
  execution_attempts,
  recovery_actions,
  policy_decisions,
  diagnoses,
  webhook_events,
  recovery_cases,
  financial_obligations,
  circuit_breaker
RESTART IDENTITY CASCADE
"""


def _database_url(db_name: str) -> str:
    return ADMIN_URL.rsplit("/", 1)[0] + f"/{quote(db_name)}"


def _role_database_url(db_name: str, role: str, password: str) -> str:
    parsed = urlparse(_database_url(db_name))
    host = parsed.hostname or "localhost"
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{role}:{password}@{host}{port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


@pytest.fixture(scope="session")
def migrated_database() -> Iterator[str]:
    admin = psycopg.connect(ADMIN_URL, autocommit=True)
    admin.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
        (TEST_DB_NAME,),
    )
    admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"')
    admin.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    admin.close()

    database_url = _database_url(TEST_DB_NAME)
    apply_migrations(database_url, reset=True)
    yield database_url


@pytest.fixture
def conn(migrated_database: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(migrated_database, autocommit=True) as connection:
        connection.execute(TRUNCATE_SQL)
        connection.execute("INSERT INTO circuit_breaker (id) VALUES (1)")
        yield connection


@pytest.fixture
def app_conn(migrated_database: str) -> Iterator[psycopg.Connection]:
    app_url = _role_database_url(TEST_DB_NAME, "recovery_app", APP_PASSWORD)
    with psycopg.connect(app_url, autocommit=True) as connection:
        yield connection


@pytest.fixture
def verifier_conn(migrated_database: str) -> Iterator[psycopg.Connection]:
    verifier_url = _role_database_url(TEST_DB_NAME, "recovery_verifier", VERIFIER_PASSWORD)
    with psycopg.connect(verifier_url, autocommit=True) as connection:
        yield connection
