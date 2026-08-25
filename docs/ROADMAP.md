# Roadmap

## Done

- PostgreSQL schema and 18 versioned migrations (`db/migrations/`)
- Database roles: `recovery_app`, `recovery_verifier`
- Constraint enforcement via CHECK constraints, partial unique indexes, and triggers
- Migration runner: `scripts/apply_migrations.py`
- Database tests: `tests/db/` (58 tests against real PostgreSQL)

Run tests: `python3 -m pytest tests/db -v`

## Next

- Webhook ingest and obligation anchor resolution
- Idempotent case creation (one recovery case per financial obligation)

## Later

- State machine and worker orchestration
- Provider integration (Razorpay)
- Execution, reconciliation, and verification
- Policy engine and diagnosis
- Human review workflow
- Simulator and observability UI
