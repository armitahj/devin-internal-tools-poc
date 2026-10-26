Internal Tools Foundation — Build Spec

Outcome

A support agent's refund request goes from submission → approval → payout → audit, 
with separation of duties, no possibility of double payment, safe recovery from 
payment-provider failures and crashes, and a complete audit trail. A second app 
(feature flags) is then built on the same foundation and inherits every control.

Stack

- Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic
- Postgres 16 via docker-compose (tests run against Postgres, not SQLite, so the 
- locking and concurrency guarantees are real)
- Server-rendered Jinja2 templates + HTMX. No SPA, no build step.
- pytest; GitHub Actions runs pytest on every PR
- One command to run: docker compose up → app at http://localhost:8000

Repo layout

The shared package is named foundation/

foundation/
  auth.py          # current_user dependency (mock: user id from cookie set by switcher)
  policy.py        # loads policy.yaml; require(action) dependency
  policy.yaml      # role → permission map for every app
  audit.py         # record(...) in caller's session; record_denied(...) in its own session
  workflow.py      # transition(...) conditional update + approval rules
  connectors/
    payments.py    # PaymentProvider protocol + MockPaymentProvider
  ui/              # base layout, user switcher, nav, table/filter/summary-card partials
apps/
  refunds/         # models, routes, templates, rules
  feature_flags/
specs/             # this file, ADD_AN_APP.md, DEVIN_PROMPTS.md
tests/

Users and roles (seeded)
User	Role
alice	agent
erin	agent
bob	reviewer
frank	reviewer
carol	admin
dave	engineer
gwen	engineer

Mock auth: a dropdown in the header sets a user_id cookie. auth.current_user 
is the only place that reads it, so replacing it with OIDC/SSO is a one-file change.

Policy

foundation/policy.yaml is the single source of truth:

refunds.view:     [agent, reviewer, admin]
refunds.submit:   [agent]
refunds.approve:  [reviewer, admin]
refunds.reject:   [reviewer, admin]
refunds.resume_payout: [reviewer, admin]
refunds.approve_high_value: [admin]
refunds.set_provider_mode: [admin]
flags.view:       [engineer, admin]
flags.toggle_nonprod: [engineer, admin]
flags.request_prod_change: [engineer]
flags.approve_prod_change: [engineer, admin]
audit.view:       [reviewer, admin, engineer]

Every protected route declares its action via Depends(require("<app>.<verb>")). 
require marks the route (e.g. sets an attribute on the dependency) so tests can 
discover which action each route requires.

Unauthorized → 403. The denial is written with audit.record_denied(...), which uses 
its own session and commits immediately, so the denial record survives even though 
the request itself is rejected and its main transaction is rolled back.

Hiding buttons in the UI is a convenience only; the server is the control.

Public routes (no permission required) are an explicit allowlist: /health, 
/static/*, and the user-switcher endpoint.

Audit

- Table audit_log(id, at, actor_id, entity, entity_id, action, before jsonb, after jsonb)
- audit.record(session, ...) takes the caller's SQLAlchemy session and does NOT commit. 
- The state change and its audit row commit together or not at all.
- audit.record_denied(...) is the only function that writes in its own session (see Policy).
- No update or delete path exists for audit rows (no route, no service function). 
- Optional: a Postgres trigger rejecting UPDATE/DELETE on audit_log.

Refunds domain

Tables:
- transactions(id, customer_name, amount_cents, created_at) — ~15 seeded
- refund_requests(id, transaction_id, amount_cents, reason, status, requested_by, reviewed_by, created_at, updated_at) — ~30 seeded across all statuses

- payouts(id, refund_id UNIQUE, idempotency_key UNIQUE, status [pending|succeeded|failed|unknown], provider_ref, attempts, last_error, updated_at)


State machine:
From	To	Trigger	Who
—	submitted	submit	agent
submitted	approved	approve	reviewer/admin, not requester; admin if > $500; only if ≤ $1,000
submitted	partially_approved	approve (first of two)	reviewer/admin, not requester; only if > $1,000
partially_approved	approved	approve (second of two)	reviewer/admin, not requester, not the first approver; admin unless the first approver was
submitted / partially_approved	rejected	reject	reviewer/admin, not requester
approved / payment_failed / payment_unknown	paid	provider confirms success	system
approved / payment_unknown	payment_failed	provider returns definite failure	system
approved / payment_failed	payment_unknown	provider call times out / outcome unknown	system
approved / payment_failed / payment_unknown	(re-run payout)	resume_payout	reviewer/admin

Any other transition → 409.

Rules:
- R1 Only agents submit.
- R2 Requester cannot approve or reject their own request.
- R3 (balance, concurrency-safe) On submit, in one DB transaction: 
- SELECT ... FROM transactions WHERE id = :tx FOR UPDATE, then sum the amounts of all 
- non-rejected refunds for that transaction, reject if existing + new > original, 
- otherwise insert the new request. All submissions against a transaction must go 
- through this path. Two concurrent submissions that together exceed the balance: 
- exactly one succeeds.
- R4 Amount > $500 requires an admin to approve. For two-approver refunds (R8) at least 
- one of the two approvers must be an admin.
- R5 (approval race) Approval is a conditional update: 
- UPDATE refund_requests SET status='approved', reviewed_by=:u WHERE id=:id AND status='submitted'. 
- 0 rows affected → 409. Two simultaneous approvals: exactly one wins.
- R6 (no double pay) Payout flow:
    1. Transaction A: conditional approve + insert payouts row (status pending, 
    2. idempotency_key = f"refund-{id}") + audit → commit.
    3. Outside any DB transaction: provider.refund(idempotency_key, amount_cents).
    4. Transaction B: conditional update of refund and payout from their current 
    5. unresolved state to the result → paid (success), payment_failed 
    6. (definite failure), or payment_unknown (timeout / lost response); audit → commit. 
    7. The provider call is never inside a DB transaction. The idempotency key is fixed per 
    8. refund and reused on every attempt, so no number of retries, crashes or concurrent 
    9. resumes can create a second payment.
- R8 (two approvers, change request) Amount > $1,000 requires two distinct approvers, 
- neither of them the requester. The first approval is a conditional update 
- submitted → partially_approved that records first_approved_by and creates no payout; 
- the second is a conditional update partially_approved → approved (reviewed_by = second 
- approver) and runs R6 from step 1. Two simultaneous second approvals: exactly one wins, 
- one ledger row. A partially_approved refund can still be rejected.
- R7 (recovery) resume_payout re-runs steps 2–3 with the same idempotency key for any 
- refund in approved (crashed before a result was recorded), payment_failed, or 
- payment_unknown. payment_unknown is never treated as a failure: resuming either 
- confirms the original payment or completes it.

Mock payment provider (MockPaymentProvider):
- Its ledger is a persisted table, mock_provider_ledger(idempotency_key PRIMARY KEY, amount_cents, provider_ref, created_at), written in its own session and committed 
- independently of the app's transactions (standing in for an external provider that 
- enforces idempotency keys on its side). A repeated key returns the original result 
- and never inserts a second row.

- Mode, set via an admin-only toggle on the dashboard:
    - ok — records the payment, returns success
    - fail — records nothing, raises PaymentError (definite failure)
    - timeout_after_success — records the payment, then raises PaymentTimeout 
    - (outcome unknown to the app)
- Documented guarantee: one payment per refund, enforced by the provider-side ledger. 
- This prototype demonstrates the pattern against a mock; a real provider's idempotency 
- behavior must be confirmed for production.

UI (refunds):
- Dashboard: summary cards (pending count, approved today, payouts needing attention 
- [approved older than 1 minute, payment_failed, payment_unknown], $ refunded last 7 days), 
- table with status filter, rows > $500 and payouts needing attention visibly flagged.
- Detail page: refund, transaction, payout status, actions allowed for the current user, 
- audit history.
- Submit form (agents only).

Feature flags domain (app #2, minimal)

- flags(id, key, environment [dev|staging|prod], enabled)
- flag_change_requests(id, flag_id, desired_enabled, requested_by, reviewed_by, status)
- dev/staging: engineer or admin toggles directly (audited).
- prod: engineer requests a change; a different engineer or admin approves; the flag 
- changes only on approval. Uses workflow.transition with the same R2/R5 guarantees.
- One page: table of flags by environment with toggle/request buttons; pending prod requests.

Tests (must pass in CI)

Permissions:
- Route coverage: introspect the FastAPI app's routes. Every route not on the public 
- allowlist must have a require(...) dependency, and every required action must exist in 
- policy.yaml. Fails if any protected route is missing a permission — independent of 
- what policy.yaml says.
- Policy matrix: for every protected route × every role, with fixtures that satisfy 
- business rules: unauthorized roles get 403; authorized roles get anything except 403.
- Denied requests leave a denied audit entry even though the request was rejected.

Refunds:
- R2 self-approval blocked; R4 > $500 needs admin; invalid transitions → 409.
- R3: over-refund blocked; two concurrent submissions against the same transaction 
- that together exceed the balance → exactly one succeeds.
- R5: two concurrent approvals of the same refund → one 200, one 409, one approval audit row.
- R6/R7:
    - ok → paid, exactly one ledger row.
    - fail → payment_failed; resume in ok → paid, exactly one ledger row.
    - timeout_after_success → payment_unknown; resume → paid, still exactly one ledger row.
    - Crash after approval commit, before provider call (simulate by stopping after step 1) 
    - → approved with pending payout; resume → paid, one ledger row.
    - Two concurrent resumes → paid, one ledger row.

Audit:
- Force an exception after the state change but before commit → neither the change nor 
- its audit row persists.

Feature flags:
- Prod change needs approval by a different user; non-prod toggle audited.

Non-goals (state these in the README)

Real SSO, real payment provider, deployment, monitoring/alerting, automatic background 
payout reconciliation (resume is manual in the prototype), secrets management, data 
retention, styling, KYC app, notifications.
