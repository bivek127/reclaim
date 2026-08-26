# Roadmap

What is built, what holds it together, and what is deliberately not built.

Claims about safety cite the numbered clauses in
[ARCHITECTURE.md](ARCHITECTURE.md#13-architecture-contract) — "contract 2" means
clause 2 of that list.

---

## Done

### Database
Status: complete.

- 20 versioned migrations in `db/migrations/`, applied by
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

### Provider adapter (Razorpay)
Status: implemented and contract-verified as far as a test account can reach.

`reclaim/provider/`:

- `contract.py` — provider-agnostic vocabulary (`ProviderOutcome`,
  `FetchOutcome`, `LinkStatus`, `ErrorClass`, and the `PaymentProvider`
  protocol). No Razorpay string crosses this module.
- `transport.py` — stdlib `http.client` with `connect()` as its own step, so a
  request that wrote zero bytes is *observed* as such rather than inferred. No
  internal retries; retry orchestration belongs to the executor.
- `razorpay.py` — `create_payment_link`, `fetch_by_reference`,
  `verify_webhook_signature`, status normalization, error classification.
  `retry_charge` raises: no merchant-facing charge-retry endpoint exists.
- `config.py` — credentials from the environment, timeouts from config, refusal
  to run against a live key, and secret redaction in `repr`, `str`, and
  exception text.

The adapter performs **no database access**: no writes, no transitions, no
leases, no policy, no reconciliation decisions.

Verified against Razorpay test mode (2026-08-26), 7 of 9 contract tests passing
live:

- Fetch by `reference_id` works — but the search endpoint is **not immediately
  consistent** after creation, resolving in roughly 1–3 seconds. This is not
  documented by Razorpay; it was found by running the suite.
- Duplicate-reference rejection returns HTTP 400 with a specific message. The
  adapter deliberately does **not** classify on that text — an undocumented
  string is not a contract.
- `expire_by` under 15 minutes is rejected.
- Test mode rate-limits tightly enough that repeated runs hit HTTP 429; the
  adapter classified those as rate-limited rather than as rejection.

Two assumptions remain unverified because credentials cannot settle them, and
both have a safe default:

| Assumption | Safe default |
|---|---|
| An expired payment link means the customer will never pay | Expiry exposes no terminal-failure signal. A case past its deadline escalates to a human instead. Closing this needs a human abandoning a real checkout page. |
| Subscription cycle ids are stable | The existing anchor is retained unchanged. Closing this needs a live subscription with two consecutive failed cycles. |

### Execution and idempotency
Status: complete, verified against real PostgreSQL.

- `reclaim/domain/execution.py` — commit, then call the provider, then commit
  the outcome. The idempotency key is persisted **before** any network call
  (contract 2), so a crash can never leave a charge the system cannot recognise.
- `reclaim/domain/breaker.py` — reads the dispatch gate and counts consecutive
  failures. It never writes the breaker's state.

Boundaries held:

- The provider call sits outside every transaction.
- Every row write rides in `transition()`'s side effects, so a stale token
  writes nothing. There is no second state machine and no second lease
  mechanism.
- An unknown outcome becomes `AMBIGUOUS` and never a failure (contract 3).
- One open action per case and one open attempt per action, enforced by the
  partial unique indexes rather than by application checks (contract 4).

### Reconciliation
Status: complete, verified against real PostgreSQL.

`reclaim/domain/reconciliation.py` claims an ambiguous case, re-queries the
provider under the persisted reference, classifies the evidence, and settles
under fencing. Two rounds, each prepare → commit → network → settle.

The evidence model is the point of the component — "not found" is not globally
equivalent to failure:

| Provider says | Local attempt was | Result |
|---|---|---|
| Found, any status | any | adopt it; the case is waiting on the customer |
| Not found | the request provably went out | confirmed failure |
| Not found | the request may never have left | bounded re-send under the *same* key |
| No usable answer | any | stay ambiguous and poll again — silence is not evidence |

The re-send adds no action row, no attempt row, and no budget: it retries the
same mechanism under the same key, so the one-open-mechanism invariant holds. A
new idempotency key is never minted during reconciliation. No link status is
treated as proof of non-payment.

### Verification and revenue
Status: complete, verified against real PostgreSQL.

`reclaim/domain/verification.py` correlates the provider's webhook with an
**independent** query it makes itself, compares status, amount (integer minor
units taken from our own attempt, never from the provider) and currency, then
writes the verification, the transition, and the revenue in one transaction as
`recovery_verifier`.

| Evidence | Result |
|---|---|
| Webhook success **and** independent query agrees | recovered; revenue recognised once |
| Provider success, no correlated webhook | stays waiting; no verification row |
| The two sources disagree, either direction | ambiguous, no revenue |
| Timeout, 5xx, rate limit, auth failure, malformed | no row, no transition, retry |

One source never produces a recovery (contract 6). `recovery_app` provably
cannot write the revenue column — the migration grants that column to the
verifier role only, and a test asserts the application role is refused.
Concurrent verifiers on separate connections recognise revenue exactly once.

---

## Not built, on purpose

**The breaker monitor.** Opening and resetting the circuit breaker belongs to a
monitor job that does not exist. The executor reads the gate and counts
failures, but never writes the breaker's state.

*Consequence, stated plainly:* the circuit breaker cannot open or reset in a
running system. Failures accumulate and nothing acts on them.

---

## Tests

```bash
python3 -m pytest tests                                       # everything
python3 -m pytest tests/provider -m "not provider_contract"   # adapter, offline
python3 -m pytest tests/provider -m provider_contract         # needs test-mode keys
```

Database behaviour, concurrency, transactions, constraints, and fencing are
tested against real PostgreSQL rather than a stub.
