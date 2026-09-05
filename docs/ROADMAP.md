# Roadmap

What's built, what's provider-verified, and what's still open — grouped by
area rather than by build history. For the design each of these implements,
see [docs/ARCHITECTURE.md](ARCHITECTURE.md).

**Legend:** ✅ Complete &nbsp;·&nbsp; 🟡 Partial / provider-dependent &nbsp;·&nbsp; 🔵 Future

---

## Foundations

| Area | Status | What exists |
|---|---|---|
| Database | ✅ | 22 versioned migrations. Two roles — `recovery_app` for ordinary work, `recovery_verifier` for the one column that represents money. Every constraint has a test that violates it and expects a refusal. |
| Obligation & case identity | ✅ | Identity binds to the obligation anchor, never a payment id, case id, or webhook id. A duplicate delivery produces exactly one case. |
| State machine | ✅ | A single `transition()` function is the only thing that changes case state. Every legal edge passes; every illegal pair raises. |
| Leases, fencing, sweeper | ✅ | `FOR UPDATE SKIP LOCKED` claims, fencing-token write-backs, and an independent sweeper for leases nobody ever released. |

Safety lives in the schema wherever it can: partial unique indexes enforce
"one open action per case," a column grant plus a trigger enforce "revenue
only after verification." These aren't application checks a future caller
could forget.

---

## Recovery pipeline

| Area | Status | What exists |
|---|---|---|
| Execution & idempotency | ✅ | Idempotency key persisted before any network call. The provider call sits outside every transaction; every write rides the same fencing check as everything else. |
| Circuit breaker monitor | ✅ | Opens the gate at a configured failure threshold, closes it once the reset window elapses, and resumes any `HALTED` cases in the same tick. Fails closed on every uncertainty. |
| Reconciliation | ✅ | Re-queries the provider under the persisted reference and classifies the evidence — "not found" is never treated as globally equivalent to failure. |
| Deterministic policy | ✅ | A pure `evaluate(facts, config)` plus a transactional apply step. Calls no provider, dispatches nothing, writes no revenue. |

---

## AI diagnosis

| Area | Status | What exists |
|---|---|---|
| LLM diagnosis | ✅ | A strict schema validator rejecting unknown fields, a prompt boundary isolating untrusted data, and a deterministic failure-code fallback. Verified end-to-end **with the model server stopped** — the only real way to know the fallback works. |

The model is advisory and structurally cannot move money — see
[the AI safety boundary](ARCHITECTURE.md#7-ai-safety-boundary).

---

## Safety & verification

| Area | Status | What exists |
|---|---|---|
| Verification & revenue | ✅ | Correlates a webhook with an independent re-query; writes verification, transition, and revenue in one transaction, `recovery_verifier` role only. |
| Human review | ✅ | Approval creates a *proposed* action only — the executor still performs the dispatch, under the same budget, idempotency, and breaker as an automated recovery. |
| Audit & reconstruction | ✅ | A case's full story — obligation, actions, attempts, worker/fencing history, model, policy version, reviewer decision — rebuilds from `audit_events` alone. Anything the trail can't supply is named, never returned as a silent `None`. |
| Simulator | ✅ | Outcome probability is unrepresentable as a function of model confidence — no column exists for it. Ships with no baseline/rate parameters set by design: it refuses to run on invented numbers rather than guess. |

---

## Operator console

| Area | Status | What exists |
|---|---|---|
| Console | ✅ | Seven surfaces: overview, case queue, case investigation, audit timeline, human review, the unmapped-webhook queue, system status. Every figure traces to an API response — an unreachable service is never rendered as an empty result. |

The unmapped-webhook queue is **visibility only**. A webhook that couldn't
be anchored to an obligation is listed for a human to read, full payload
included — there's no write path yet for assigning an anchor by hand (see
[Known gaps](#known-gaps)).

---

## Provider integration (Razorpay)

| Area | Status | What exists |
|---|---|---|
| Adapter | 🟡 | Implemented, contract-verified as far as a test account can reach. No database access — no writes, transitions, leases, or policy decisions happen in this layer. |

Verified live against Razorpay test mode: payment-link creation, fetch by
reference (not immediately consistent after creation — resolves in roughly
1–3 seconds, undocumented, found by testing), duplicate-reference rejection
shape, and the `expire_by` minimum window.

Two assumptions remain genuinely unverified, each with a safe default rather
than a guess:

| Assumption | Safe default |
|---|---|
| An expired payment link can never subsequently be paid | A case past its deadline escalates to a human instead of a second automated action |
| The subscription billing-cycle id is stable across redeliveries | The current anchor field is kept as-is; closing this needs a live subscription with two consecutive failed cycles |

`retry_charge` is not implemented — Razorpay exposes no merchant-facing
charge-retry endpoint that accepts a caller-supplied idempotency key, so
policy never selects it, and the executor refuses it before dispatch even
if it somehow were.

---

## Known gaps

Real, currently unimplemented — not deferred by accident:

| Gap | Status | Why it's not done |
|---|---|---|
| Customer-facing notifier | 🔵 | No field in the schema holds a customer's email or phone — building this means choosing a data source first, not writing missing wiring. Its designed failure behavior is deliberately terminal either way: it can never restart case state. |
| Manual anchoring for unmapped webhooks | 🔵 | The read side is built (see [Operator console](#operator-console)); the write side is genuinely underspecified — letting a human assert a financial anchor by hand needs its own review, not a quick addition. |
| Lift confidence interval | 🔵 | The simulator reports a point estimate on purpose. Naming a statistical method here would invent a contract the design doesn't state. |

---

## Accepted risks

Carried deliberately — the full reasoning and mitigation for each is in
[Accepted risks](ARCHITECTURE.md#12-accepted-risks):

| Risk | Production fix |
|---|---|
| Global, not per-method, circuit breaker | Per-method or per-cause scoping |
| Single shared reviewer credential | Per-reviewer identity + permissions |
| Fixed-interval reconciliation polling | Adaptive/backoff polling |
| Model confidence unused for safety | Calibration study first |
| Dual-action supersede covers the common case | Formal proof or exhaustive interleaving tests |

---

## Tests

```bash
python3 -m pytest tests                                       # everything
python3 -m pytest tests/provider -m "not provider_contract"   # adapter, offline
python3 -m pytest tests/provider -m provider_contract         # needs test-mode keys
cd web && npx vitest run                                      # console
```

Last run (2026-09-05, no Razorpay contract credentials sourced):
**1248 passed, 9 skipped** for the Python suite against real PostgreSQL, and
**206 passed** across 16 files for the console. The nine skips are the
provider contract tests that need live test-mode credentials.

Database behavior — concurrency, transactions, constraints, fencing — is
tested against real PostgreSQL rather than a mock: a fake can't refuse a
write the way a partial unique index does.
