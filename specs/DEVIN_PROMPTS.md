Devin session prompts

Run in order. Sessions 3a and 3b can run in parallel once session 2 is merged.

Timings are targets, not forecasts: PRs, CI runs and merges add overhead. 
Hard rule: stop adding features at 1:40. Use the remaining time to run the 
demo end to end, fix anything broken, and finish the README. Session 5 is optional.

For each session, log: start time, end time, ACUs used, number of review comments, 
and anything you had to fix by hand. These numbers go in the README and the video.

---

Session 1 — Repo skeleton and CI (target: 10 min)

Create a new Python 3.12 FastAPI project in this repo per specs/SPEC.md 
("Stack" and "Repo layout" sections only). The shared package must be named 
foundation/, not platform/.
- docker-compose with Postgres 16 and the app; docker compose up serves http://localhost:8000
- SQLAlchemy 2.x + Alembic configured; empty initial migration
- pytest configured to run against a Postgres test database
- GitHub Actions workflow running pytest on every pull request (with a Postgres service)
- A /health route and one passing test for it

Do not build any foundation or app features yet. 
Done when: docker compose up works, pytest passes locally, and the Actions workflow 
passes on your PR. Open one PR titled "Session 1: skeleton + CI" with this prompt 
in the description.

---

Session 2 — Foundation (target: 25 min)

Implement everything under foundation/ per specs/SPEC.md: the "Users and roles", 
"Policy" and "Audit" sections, plus workflow.py and the payments connector.
- auth.current_user reads a user_id cookie; seed the seven users; base template 
- has a user switcher dropdown and a nav
- policy.yaml exactly as in the spec; require(action) FastAPI dependency that 
- marks the route with its action, returns 403 on denial, and writes the denial with 
- audit.record_denied in its own committed session
- audit.record(session, ...) that does not commit
- workflow.transition(session, model, id, from_states, to_state, actor, forbid_actor=None) 
- implemented as a single conditional UPDATE; raises 409 if 0 rows change, 
- 403 if actor == forbid_actor
- PaymentProvider protocol and MockPaymentProvider with modes ok / fail / 
- timeout_after_success, raising PaymentError and PaymentTimeout as specified, 
- backed by the persisted mock_provider_ledger table written in its own session
- Shared UI partials: base layout, table, status filter, summary card

Tests: route-coverage test (introspects routes, fails if any non-public route lacks 
require); policy-matrix test generated from routes × roles; denied-audit-survives-rollback 
test; audit atomicity test; workflow conditional-update test with two concurrent threads; 
mock provider idempotency tests (same key twice → one ledger row, original result returned). 
Done when all tests pass in CI. One PR, prompt in description.

---

Session 3a — Refunds app: domain, rules, payouts (target: 20 min)

Build the refunds domain per specs/SPEC.md "Refunds domain", following 
specs/ADD_AN_APP.md. This session is models, rules, routes and tests — use 
minimal placeholder templates; another session builds the UI. 
Implement rules R1–R7 exactly:
- R3: lock the transaction row with SELECT ... FOR UPDATE, check the balance and 
- insert in one DB transaction.
- R6: commit approval + pending payout row, call the provider outside any DB transaction 
- with key refund-{id}, then record the result in a second transaction. PaymentTimeout 
- - means payment_unknown, never payment_failed.
- R7: resume_payout works from approved, payment_failed and payment_unknown, 
- always reusing the same idempotency key. 
- Seed ~15 transactions and ~30 refunds across all statuses. 
- Tests: every refunds test listed in the spec's "Tests" section, including concurrent 
- submissions against one transaction's balance, concurrent approvals, the crash-after-approval 
- resume test, timeout_after_success resume, and concurrent resumes — each asserting exactly 
- one ledger row where relevant. 
- Done when all tests pass in CI. One PR, prompt in description.

Session 3b — Refunds app: UI (target: 15 min, parallel with 3a)

Build the refunds UI per specs/SPEC.md "UI (refunds)" using the shared partials. 
Dashboard with four summary cards, filterable table, flagged rows (over $500, and payouts 
needing attention); detail page with payout status, permitted actions and audit history; 
submit form for agents; admin-only toggle for the mock payment provider mode. Use HTMX 
for actions. Work against the route signatures in session 3a's PR; rebase once it is merged. 
Add a "Demo script" section to the README listing the click-through steps. 
Done when a reviewer can click through the demo script. One PR, prompt in description.

---

Session 4 — Feature-flag app from the playbook (target: 15 min — TIME THIS)

Build the feature-flags app described in specs/SPEC.md "Feature flags domain". 
Follow specs/ADD_AN_APP.md exactly and reuse the foundation. You may add permission 
entries to foundation/policy.yaml and a nav link to foundation/ui/base.html; any other 
change to foundation/ must be explained in the PR description. 
Seed 6 flags across dev, staging and prod. 
Done when the app is in the nav, prod changes require approval from a different 
user, all changes are audited, and all tests (including route coverage and the policy 
matrix) pass in CI. One PR, prompt in description.

---

Session 5 — Change request (OPTIONAL, target: 10 min, only if before 1:30)

Change request from the refunds team: refunds over $1,000 now require two distinct 
approvers (neither may be the requester). The first approval moves the request to a 
new status partially_approved; the second moves it to approved and triggers payout. 
Make the change in the refunds rules and workflow usage; add tests for: one approver 
is not enough, the same person cannot approve twice, the requester cannot be either 
approver, and concurrent second approvals still pay exactly once. 
Done when all tests pass in CI. One PR, prompt in description.
