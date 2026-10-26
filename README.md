# Refunds workflow prototype (Devin internal-tools PoC)

> **Prototype only, not production-ready.** Authentication is **simulated** (pick a demo identity, no
> passwords), all data is **synthetic**, and payments go through a **simulated provider**. No real money moves.

This is one complete internal-tool workflow, built to check whether custom apps built with Devin could
replace an internal-tool platform:

1. A support agent requests a refund against a seeded transaction.
2. A different user with reviewer permissions approves or rejects it.
3. An approved refund is executed by the simulated payment provider.
4. Everyone can see its final status and full audit history.

## Run it

Requirements: Docker and Python 3.10+.

```bash
docker compose up -d                       # PostgreSQL 16 on localhost:5433
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m app.cli reset-db       # create schema + seed synthetic data
.venv/bin/uvicorn app.main:app --port 8000 # http://localhost:8000
.venv/bin/python -m pytest                 # tests (use a separate refunds_test database)
```

Demo identities (simulated):

| User  | Can request | Can review | Purpose |
|-------|-------------|------------|---------|
| alice | yes | no  | support agent |
| ben   | yes | no  | support agent |
| rita  | no  | yes | reviewer |
| sam   | yes | yes | supervisor; shows that self-approval is blocked even with both roles |

Simulated provider behaviour, chosen by the **refund amount's cents**:

| Cents | Behaviour |
|-------|-----------|
| `..13` | Definitive rejection, so the refund ends **FAILED** and the reserved balance is released |
| `..99` | First call times out *before* processing; a retry succeeds |
| `..98` | First call processes the refund, then the response is lost (timeout); a retry/reconcile returns the original result |
| `..97` | Provider succeeds, then the **application crashes** before recording it (demo fault injection); the refund stays in PROCESSING until the recovery command runs |
| other | Success |

### Crash-recovery demo

```bash
.venv/bin/python -m app.cli demo-crash-recovery   # scripted: approve -> provider success -> crash -> recover
.venv/bin/python -m app.cli recover --stale-seconds 60   # operator command: reconcile stuck PROCESSING refunds
```

The scripted demo shows the refund going `APPROVED` → `PROCESSING` (crash, while the provider already
holds 1 refund) → `SUCCEEDED` after recovery, with exactly **one** provider refund for the idempotency key.

## Architecture

A single FastAPI process with server-rendered Jinja2 pages, PostgreSQL storage, and a payment-provider adapter.

```
app/
  main.py               HTTP routes + pages (thin), simulated login, CSRF
  refunds/service.py    the workflow: state machine, invariants, locking, audit (all rules live here)
  payments/adapter.py   PaymentProvider interface: refund(charge_id, amount, currency, idempotency_key)
  payments/simulator.py simulated provider, with its own table and its own DB transactions (like an external system)
  models.py, db.py      SQLAlchemy models (schema via create_all; no migrations yet)
  cli.py                reset-db, recover, demo-crash-recovery
  seed.py               synthetic users and transactions
tests/                  pytest against real PostgreSQL
```

A real provider (for example Stripe) would be another `PaymentProvider` implementation. The workflow code would not change.

### Data model

- `users`: `can_request`, `can_review` flags.
- `payment_transactions`: captured amount (integer cents), currency, provider charge id.
- `refunds`: amount, reason, `status`, `requested_by/at`, `reviewed_by/at/note`, `request_key` (unique, makes form
  submission idempotent), `provider_idempotency_key` (unique, generated once, reused on every provider call),
  `attempts`, `processing_started_at`, `provider_refund_id`, `last_error`, `completed_at`.
- `audit_events`: rows are only appended, never updated. Each row stores refund, actor, action, from/to status, JSON
  details and a `clock_timestamp()` time. It is written in the same DB transaction as the change it records.
- `sim_provider_refunds`: the simulator's own records, keyed by idempotency key.

### State machine

```
REQUESTED --approve--> APPROVED --execute--> PROCESSING --provider ok--------> SUCCEEDED
    |                                          |   ^ --provider rejects----> FAILED (balance released)
    +--reject--> REJECTED                      |   |
                                               +---+ timeout / crash: outcome unknown, stays PROCESSING,
                                                     balance stays reserved; retry (UI) or `cli recover`
```

When the reviewer approves, the app immediately tries to execute the refund. Approval and execution are still
separate states and separate DB transactions. **Refundable balance** = captured − (APPROVED + PROCESSING + SUCCEEDED).
REQUESTED refunds do not reserve balance.

## How the invariants are enforced

| Invariant | Enforcement | Tests |
|-----------|-------------|-------|
| No self-approval / unauthorized approval | The service checks `can_review` and `reviewer != requester`, using the server-side session identity (hiding buttons is not relied on). DB `CHECK (reviewed_by <> requested_by)` as a backstop. Only reviewers can retry. | `test_agent_cannot_approve`, `test_self_approval_blocked_for_dual_role_user`, `test_database_constraint_blocks_self_review`, `test_only_reviewers_can_retry`, HTTP 403 tests |
| Amount ≤ remaining refundable balance | Checked when the refund is requested (early feedback). **Enforced atomically when it is approved**: lock the `payment_transactions` row with `SELECT … FOR UPDATE`, then the refund row, then recompute the balance excluding the refund being approved. The DB also requires amount > 0. | `test_pending_requests_do_not_reserve_but_approval_enforces`, `test_cumulative_partial_refunds_cannot_exceed_balance`, `test_concurrent_approvals_cannot_exceed_balance`, `test_approval_waits_for_transaction_row_lock` |
| No duplicate refunds (concurrency, retries) | (a) Every write takes locks in the same order: transaction row, then refund row. (b) A refund only moves `APPROVED→PROCESSING` under that lock; any other caller gets a Conflict. (c) The provider is always called with the refund's stored idempotency key; the simulator stores each key once and replays the stored result. (d) A unique `request_key` makes repeated form submissions harmless. | `test_concurrent_approve_and_execute_same_refund_refunds_once`, `test_concurrent_execute_retries_refund_once`, `test_duplicate_request_submission_creates_one_refund`, `test_recovery_skips_recent_processing_and_concurrent_reconcile_is_safe` |
| Approval ≠ execution; failures never look like success | Separate states. Only a definitive provider rejection sets FAILED and releases the balance. A timeout or crash leaves the refund PROCESSING ("outcome not confirmed") with the balance still reserved. Only `SUCCEEDED` is shown in green. | `test_definitive_rejection_fails_and_releases_balance`, `test_timeout_keeps_balance_reserved_then_retry_succeeds`, `test_lost_response_timeout_reconciles_without_duplicate` |
| Crash recovery | `python -m app.cli recover` re-drives PROCESSING refunds older than `--stale-seconds` using the **original** idempotency key. | `test_crash_after_provider_success_is_recovered_by_cli` |
| Who did what and when | An audit row is written in the same DB transaction as every state change. Actors are user ids or `system:recovery-cli`. | `test_audit_records_actor_action_and_time_for_each_step`, `test_rejection_is_audited_and_terminal`, `test_full_workflow_over_http` |
| Web security basics | CSRF token (per session, compared in constant time) on **every** POST, including login and logout. GET routes only read; action endpoints accept only POST. Session cookie is signed with `SameSite=Lax`. | `test_state_changes_require_csrf`, `test_login_requires_csrf`, `test_get_routes_are_read_only` |

The concurrency tests use real threads, and each service call opens its own SQLAlchemy session (so its own
PostgreSQL connection). A barrier releases the threads at the same moment.

## Mocked or deferred (not production-ready)

- **Simulated:**
  - Authentication: no SSO or passwords.
  - Payment provider: no webhooks, and no separate status-lookup API (reconcile simply re-sends the request with the same key).
  - All data is synthetic.
- **Deferred:**
  - Real identity and admin screens for managing permissions.
  - Automatic recovery scheduler.
  - Provider webhooks.
  - Migrations: tables are created with `create_all`; no Alembic.
  - Approval limits or multi-level approval, multiple currencies, pagination and search.
  - Rate limiting, deployment, monitoring, data retention and PII policy, CI pipeline.
- **Default secret:** `SESSION_SECRET` falls back to a development value; set it outside local demos.
- **Recovery after the provider forgets a key:** if a real provider expires idempotency keys before
  recovery runs, re-sending could refund twice. Production would need a status lookup by key and an
  alert for refunds stuck in PROCESSING.
