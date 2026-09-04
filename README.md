# Reclaim

Payment recovery system for failed charges — built around database-enforced
safety invariants: one case per obligation, idempotent execution, revenue
counted only after independent verification. A deterministic policy engine
decides what to do about a failure; a local LLM is advisory only and cannot
touch money, identifiers, or destinations; every decision is replayable from
an append-only audit trail.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design and the
invariants it enforces, and [docs/ROADMAP.md](docs/ROADMAP.md) for what's
built and what's known to be missing.

## Requirements

PostgreSQL 14+, Python 3.11+, Node 18+.

## Setup

```bash
python3 -m pip install -e ".[dev]"
cp .env.example .env                      # edit connection URLs if not using the defaults
python3 scripts/apply_migrations.py --database-url "$DATABASE_URL" --reset
python3 -m pytest tests -q                 # 1248 passed, 9 skipped without provider credentials
```

Optional: `docker compose up -d` for a local Postgres instead of a system install.

## Running it

The system is several small processes, one job per process — which jobs share
a process is a deployment decision, not something baked into the code. For
local use, one script starts all of them plus the API and the console:

```bash
cd web && npm install && npm run build && cd ..   # console is served as a static build
scripts/run_dev.sh                                  # API :8000, console :4000, every background job
```

Open **http://localhost:4000**. Stop everything with `scripts/run_dev.sh stop`.
Logs land in `/tmp/reclaim-*.log`.

Three of the jobs — `executor`, `reconciler`, `verifier` — need real Razorpay
test-mode credentials (`RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`,
`RAZORPAY_WEBHOOK_SECRET` in `.env`) to actually reach the provider. Without
them, each fails closed per case (logged, no write, no crash) rather than
guessing at an outcome — a missing credential is a configuration problem, not
license to assume what the provider would have said.

## Seeing it work without a live payment gateway

```bash
python3 scripts/seed_dev.py --truncate
```

Populates the database with 13 cases spanning nearly every state in the
machine — recovered, awaiting customer, escalated, ambiguous, halted,
expired-unresolved — by driving the *real* domain functions (ingest,
diagnosis, policy, execution, reconciliation, verification, review) against a
scripted provider stub that never touches the network. The resulting cases,
states, and audit trail are genuine evidence of the system working, not
fixtures shaped to look like one. This is what the console shows by default
and is the fastest way to see the whole system without Razorpay credentials.

## Running the test suite

```bash
python3 -m pytest tests -q                                    # everything
python3 -m pytest tests/provider -m "not provider_contract"   # provider adapter, offline
python3 -m pytest tests/provider -m provider_contract         # needs live test-mode keys
cd web && npx vitest run                                      # console
```

Database behavior — concurrency, transactions, constraints, fencing — is
tested against real PostgreSQL, never a stub or mock: a fake cannot refuse a
write the way a partial unique index does.

## Layout

```
reclaim/
├── reclaim/             Application code
│   ├── domain/          State machine, policy, execution, verification, ...
│   ├── provider/        Razorpay adapter — no database access
│   ├── llm/             Diagnosis schema, prompt boundary, model client
│   ├── jobs/            Background job registry and process entrypoint
│   ├── api/              FastAPI HTTP boundary for the console
│   └── readmodel/       Read-only projections for the console
├── db/migrations/       Versioned SQL schema
├── scripts/             Migration runner, dev seeder, run_dev.sh
├── config/              Policy, operational, and simulator defaults
├── tests/                Mirrors reclaim/, plus tests/db/ for schema constraints
├── web/                 React + TypeScript operator console, thin Express BFF
└── docs/
    ├── ARCHITECTURE.md  System design and invariants
    └── ROADMAP.md       What's done, what's known to be missing
```

## Documentation

| File | Purpose |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design and invariants |
| [docs/ROADMAP.md](docs/ROADMAP.md) | What's done and what's next |
