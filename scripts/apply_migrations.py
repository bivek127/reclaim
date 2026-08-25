#!/usr/bin/env python3
"""Apply versioned SQL migrations from db/migrations/."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"


def parse_up_sql(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"-- migrate:up\n(.*?)(?:\n-- migrate:down|\Z)", text, re.DOTALL)
    if not match:
        raise ValueError(f"No -- migrate:up section in {path}")
    return match.group(1).strip()


def ensure_tracking_table(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def applied_versions(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row[0] for row in rows}


def apply_migrations(database_url: str, *, reset: bool = False) -> list[str]:
    applied_now: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        if reset:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
            conn.execute("GRANT ALL ON SCHEMA public TO PUBLIC")

        ensure_tracking_table(conn)
        done = applied_versions(conn)

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = path.stem
            if version in done:
                continue
            sql = parse_up_sql(path)
            print(f"Applying {version}...", file=sys.stderr)
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s)",
                (version,),
            )
            applied_now.append(version)

    return applied_now


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply reclaim database migrations")
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL URL (defaults to DATABASE_URL env var)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop and recreate public schema before applying",
    )
    args = parser.parse_args()

    import os

    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL or --database-url is required", file=sys.stderr)
        return 1

    applied = apply_migrations(database_url, reset=args.reset)
    if applied:
        print(f"Applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("Database is up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
