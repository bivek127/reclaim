# Reclaim

**A payment recovery system that cannot silently double-charge, guess at an
uncertain outcome, or count revenue before it's confirmed.**

When a customer's payment fails, most systems either do nothing or retry
blindly. Both are risky: doing nothing loses recoverable revenue, and naive
retries can charge a customer twice. Reclaim recovers failed payments through
a state machine and a set of database-enforced invariants that make the
dangerous outcomes structurally impossible, not just discouraged by
convention.

📖 [Architecture](docs/ARCHITECTURE.md) · 🗺️ [Roadmap](docs/ROADMAP.md)

---

## The problem

A failed payment looks simple from the outside — retry it. In practice, a
recovery system has to get several genuinely hard things right at once:

- **Duplicate recovery.** The same failure can arrive as multiple webhooks,
  or as different event types. Treat them as separate and you open two
  recovery workflows for one problem.
- **Uncertain provider outcomes.** A network timeout is not a failure — it's
  an unknown. Guess wrong and you either abandon a payment that actually
  succeeded, or retry one that already went through.
- **Retry amplification.** A flaky model call or a webhook redelivery storm
  must never translate into a flurry of real charge attempts.
- **Unsafe execution.** A crash between "the provider accepted the charge"
  and "we recorded that" is a real failure mode, not an edge case worth
  ignoring.
- **Revenue counted too early.** A webhook alone is someone's claim, not
  proof. Counting it as recovered before confirming it independently
  overstates results.

Reclaim is built around closing each of these, structurally — see
[Safety by design](#safety-by-design) below.

---

## How it works

```
Webhook → Recovery Case → Diagnosis → Policy → Action → Execution → Verification → Recovered Revenue
```

| Stage | What happens |
|---|---|
| **Webhook** | A failed-payment event arrives and is deduplicated onto a stable obligation anchor — never a payment id, never a webhook id. |
| **Recovery Case** | One case per obligation. This is the unit of work everything else attaches to. |
| **Diagnosis** | A cause is identified — by a local LLM if available, by a deterministic fallback if not. Advisory only. |
| **Policy** | A deterministic table decides the action for that cause. The model never decides whether money moves. |
| **Action** | The chosen intervention (e.g. a new payment link) for this case at this point in time. |
| **Execution** | The action is dispatched to the provider, idempotently. |
| **Verification** | An independent re-query confirms — or contradicts — what the webhook claimed. |
| **Recovered Revenue** | Counted only once verification agrees. |

The full state machine — including how ambiguity, retries, and human review
fit in — is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#state-machine).

---

## Safety by design

| Principle | What it means |
|---|---|
| **One obligation → one case** | Prevents duplicate recovery workflows for the same failure. |
| **Idempotent execution** | A retry of the same attempt reuses its persisted key — a crash can never turn one charge into two. |
| **Unknown = ambiguous** | An uncertain provider outcome becomes an explicit `AMBIGUOUS` state, never a guess. |
| **Deterministic policy** | A fixed table decides the action for a cause. AI has no vote on whether money moves. |
| **AI is advisory only** | The model's response schema has no field for an amount, an identifier, or a destination. |
| **Verify before counting** | Revenue is recognised only after an independent re-query agrees with the webhook — and only one database role can write it. |

---

## The AI boundary

```
LLM → Diagnosis → Deterministic Policy → Controlled Execution
```

The model reads a failure and proposes a cause and an action. That's the
entire extent of its power. It **cannot**:

- choose or influence a monetary amount
- supply a payment or customer identifier
- supply a destination
- execute anything itself

Every field capable of doing those things is filled in by trusted code from
the database, never from the model's response — so even a successful prompt
injection has nothing to act on. If the model is unreachable, a deterministic
lookup table takes over and the pipeline continues unchanged. This fallback
is exercised by the test suite with the model server stopped — not just
assumed to work.

---

## State machine

At a glance, a case moves through: diagnosis → a policy decision → dispatch
→ either a confirmed outcome or an explicit `AMBIGUOUS` state that gets
reconciled → independent verification → a terminal state.

The complete machine — every legal transition, why `AMBIGUOUS` exists as its
own state, and what happens at each edge case — is documented in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#state-machine).

---

## Demo

The operator console shows the whole lifecycle: a case arriving, its
diagnosis, the policy decision, the dispatched action, verification, and a
full audit trail — plus a human review queue and an unmapped-webhook queue
for anything that couldn't be anchored automatically.

Two ways to see it working, and it's worth being precise about the
difference:

- **`scripts/seed_dev.py`** drives 13 cases through the real domain
  functions — diagnosis, policy, execution, reconciliation, verification,
  review — against a scripted provider stub that never touches the network.
  Genuine states and a genuine audit trail, zero external dependency.
- **`scripts/demo_webhook.py`** sends one correctly-signed webhook to the
  running system and lets it process live: real local LLM diagnosis, real
  policy evaluation, and — with real Razorpay test-mode credentials
  configured — a real payment link created against Razorpay's test
  environment. No real money moves; Razorpay test mode is a full sandbox.

Nothing here is claimed as verified against Razorpay beyond what's actually
been run — see [docs/ROADMAP.md](docs/ROADMAP.md) for exactly what's been
checked against the live provider and what still carries a safe default.

---

## Running locally

### Requirements

PostgreSQL 14+, Python 3.11+, Node 18+.

### Setup

```bash
python3 -m pip install -e ".[dev]"
cp .env.example .env                 # edit connection URLs if not using the defaults
python3 scripts/apply_migrations.py --database-url "$DATABASE_URL" --reset
cd web && npm install && npm run build && cd ..
```

Optional: `docker compose up -d` for a local Postgres instead of a system install.

### Start the application

```bash
scripts/run_dev.sh          # API :8000, console :4000, every background job
scripts/run_dev.sh stop     # stop everything it started
```

Open **http://localhost:4000**. Logs land in `/tmp/reclaim-*.log`.

Three jobs — `executor`, `reconciler`, `verifier` — need real Razorpay
test-mode credentials in `.env` to reach the provider. Without them, each
fails closed per case (logged, no write, no crash) rather than guessing at
an outcome.

### Run the demo

```bash
python3 scripts/seed_dev.py --truncate    # 13 cases across nearly every state
python3 scripts/demo_webhook.py            # watch one process live, end to end
```

### Run tests

```bash
python3 -m pytest tests -q                                    # 1248 passed, 9 skipped without provider credentials
python3 -m pytest tests/provider -m provider_contract         # needs live test-mode keys
cd web && npx vitest run                                      # 206 passed
```

Database behavior — concurrency, transactions, constraints, fencing — is
tested against real PostgreSQL, never a mock: a fake can't refuse a write the
way a partial unique index does.

---

## Project structure

```
reclaim/
├── reclaim/
│   ├── domain/       state machine, policy, execution, verification, ...
│   ├── provider/     Razorpay adapter — no database access
│   ├── llm/          diagnosis schema, prompt boundary, model client
│   ├── jobs/         background job registry and process entrypoint
│   ├── api/          FastAPI HTTP boundary for the console
│   └── readmodel/    read-only projections for the console
├── db/migrations/    versioned SQL schema
├── scripts/          migration runner, demo seeder, run_dev.sh
├── config/           policy, operational, and simulator defaults
├── tests/            mirrors reclaim/, plus tests/db/ for constraints
├── web/              React + TypeScript console, thin Express BFF
└── docs/
    ├── ARCHITECTURE.md   system design and invariants
    └── ROADMAP.md        what's done, what's next
```

## Documentation

| | |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | The technical deep dive — domain model, state machine, concurrency, AI boundary, verification, accepted risks |
| [docs/ROADMAP.md](docs/ROADMAP.md) | What's built, what's provider-verified, what's still open |
