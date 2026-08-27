"""Connection management for the API process.

Two roles, matching the privilege split the schema already enforces:
`recovery_app` for reads and review decisions, `recovery_verifier` only where
revenue recognition is involved. The API never connects as an owner role.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg

APP_URL = os.environ.get(
    "DB_APP_URL",
    "postgresql://recovery_app:recovery_app_test@localhost:5432/reclaim_dev",
)
VERIFIER_URL = os.environ.get(
    "DB_VERIFIER_URL",
    "postgresql://recovery_verifier:recovery_verifier_test@localhost:5432/reclaim_dev",
)


@contextmanager
def app_conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(APP_URL, autocommit=True) as conn:
        yield conn


@contextmanager
def verifier_conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(VERIFIER_URL, autocommit=True) as conn:
        yield conn


def environment_label() -> str:
    """Names the target database so the console can show what it is attached to."""
    tail = APP_URL.rsplit("/", 1)[-1].split("?")[0]
    return tail or "unknown"
