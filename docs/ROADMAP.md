# Roadmap

What is built, what holds it together, and what is deliberately not built.

Claims about safety cite the numbered clauses in
[ARCHITECTURE.md](ARCHITECTURE.md#13-architecture-contract) — "contract 2" means
clause 2 of that list.

---

## Done

### Database
Status: complete.

- 18 versioned migrations in `db/migrations/`, applied by
  `scripts/apply_migrations.py`.
- Two roles with different powers: `recovery_app` for ordinary work,
  `recovery_verifier` for the one column that represents money
  (`recovered_amount_minor`).
- Every constraint has a test that violates it and expects the database to
  refuse — `tests/db/`.

Safety lives in the schema wherever it can. Partial unique indexes enforce "one
open action per case" and "one open attempt per action" (contract 4); a column
grant plus a trigger enforce "revenue only after verification" (contract 6).
These are not application checks that a future caller can forget.

### Obligation and case identity
Status: complete.

- `reclaim/domain/anchors.py` — resolves the obligation anchor (order or
  subscription-cycle id) from a provider payload.
- `reclaim/domain/lifecycle.py` — idempotent case creation on that anchor.
- `reclaim/ingest/webhook.py` — ingest as a domain function; no HTTP server.

Identity binds to the anchor, never to a payment id, case id, or webhook event
id (contract 1). A duplicate delivery produces one case.

### State machine
Status: complete.

- `reclaim/domain/transitions.py` — a single `transition()` is the only thing
  that changes case state, and it writes exactly one audit row per transition.
- `reclaim/domain/states.py` — the allowed edges as an explicit frozenset.
- `tests/domain/test_transitions.py` — every legal edge passes and **every**
  illegal pair raises. The test derives its sets from the frozenset itself, so
  it cannot drift from the implementation.

### Leases, fencing, and the sweeper
Status: complete.

- `reclaim/domain/leases.py` — `claim_case` / `claim_next` using
  `FOR UPDATE SKIP LOCKED`. `fenced_transition()` wraps `transition()`; a write
  carrying a stale fencing token is a no-op and is audited as
  `stale_write_rejected` (contract 9).
- `reclaim/domain/sweeper.py` — releases expired leases, routes an abandoned
  `EXECUTING` case to `AMBIGUOUS`, and expires TTL budgets.
- Lease durations in `config/operational.yaml`; the execution lease is at least
  twice the provider HTTP timeout, so a lease cannot lapse mid-call.

Time spent `HALTED` does not consume a case's TTL (contract 8).

---

---

## Tests

```bash
python3 -m pytest tests                                       # everything
python3 -m pytest tests/provider -m "not provider_contract"   # adapter, offline
python3 -m pytest tests/provider -m provider_contract         # needs test-mode keys
```

Database behaviour, concurrency, transactions, constraints, and fencing are
tested against real PostgreSQL rather than a stub.
