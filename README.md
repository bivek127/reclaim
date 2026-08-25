# Reclaim

Payment recovery system for failed charges — built around database-enforced safety invariants (one case per obligation, idempotent execution, verified revenue).

## Quick start

**Requirements:** PostgreSQL 14+, Python 3.11+

```bash
python3 -m pip install -e ".[dev]"
cp .env.example .env   # edit connection URLs
python3 scripts/apply_migrations.py --database-url "$DATABASE_URL" --reset
python3 -m pytest tests/db -v
```

Optional: `docker compose up -d` for a local Postgres instance.

## Layout

```
reclaim/
├── db/migrations/       Versioned SQL schema
├── scripts/             Migration runner
├── tests/db/            Database constraint tests
├── docs/
│   ├── ARCHITECTURE.md
│   └── ROADMAP.md
└── README.md
```

## Documentation

| File | Purpose |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design and invariants |
| [docs/ROADMAP.md](docs/ROADMAP.md) | What's done and what's next |
