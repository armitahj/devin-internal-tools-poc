# devin-internal-tools-poc

Internal Tools Foundation prototype. See [specs/SPEC.md](specs/SPEC.md).

## Run

```bash
docker compose up        # Postgres 16 (host port 5433) + app at http://localhost:8000
curl localhost:8000/health
```

The app container runs `alembic upgrade head` before starting uvicorn.

## Test

Tests run against Postgres (not SQLite). With the compose `db` service running:

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
docker compose up -d db
.venv/bin/pytest
```

The suite creates a `foundation_test` database if missing and migrates it to head.
Override the target with `TEST_DATABASE_URL`. CI (`.github/workflows/ci.yml`) runs
pytest on every pull request against a Postgres 16 service.

## Demo script

Start from a fresh database (`docker compose down -v && docker compose up --build`), open
http://localhost:8000 and switch users with the **Acting as** dropdown in the header.

1. **As alice (agent), submit a $120 refund.** Refunds → **New refund**, pick a transaction
   with at least $120 still refundable (e.g. *Umbrella*), amount `120`, any reason, **Submit
   refund**. You land on the refund's page: status `submitted`, no payout yet.
2. **Try to approve it as alice — blocked.** Under *Actions*, alice has no permitted actions;
   click **Approve** in the "Not permitted for you" row. A red *Not allowed* message explains
   that alice (agent) lacks `refunds.approve`; the status is unchanged and the attempt appears
   as a `denied` row in the refund's audit history.
3. **As bob (reviewer), approve it — paid.** Switch to bob on the same page and click
   **Approve**. Status `paid`, payout `succeeded`, 1 attempt, 1 payment in the provider ledger.
4. **As carol (admin), set provider mode to `timeout_after_success`; as bob, approve another
   refund — shows `payment_unknown`.** Switch to carol; in the orange **Demo control** box (on
   the dashboard and on every refund page) choose `timeout_after_success` → **Set mode**.
   Switch to bob, filter the dashboard by `submitted`, open any refund of $500 or less and
   click **Approve**: status `payment_unknown`, payout `unknown`, flagged *needs attention*.
   The provider ledger already shows 1 payment: the provider paid but never answered.
5. **As carol, set mode back to `ok`; resume the payout — paid, one payment in the ledger.**
   Switch to carol on that refund, set the mode to `ok`, click **Resume payout**. Status
   `paid`, payout `succeeded` after 2 attempts, and still **1 payment** for key `refund-<id>`:
   the retry reused the idempotency key, so the customer was not paid twice.
6. **Open the audit log and show the full history, including the blocked attempt.** As carol,
   click **Audit log** in the header: the `denied` row for alice's `refunds.approve` attempt is
   highlighted next to the submit, approve, `set_mode` and `payout_*` rows (payout outcomes are
   recorded as `system`, with the human in `triggered_by`). Each refund page shows the same
   history for that refund.

7. **Refunds over $1,000 need two approvers.** As bob, filter by `submitted` and open the
   *Stark Industries* refund ($1,280): click **Approve (1 of 2)** — status
   `partially_approved`, *Approvals: bob (1 of 2)*, no payout. bob's Approve is now disabled
   ("you gave the first approval"), and frank (reviewer) cannot give the second approval
   either, because one approver must be an admin. Switch to carol and click
   **Approve (2 of 2)**: status `paid`, *Approvals: bob, carol (2 of 2)*, 1 payment.

## Layout

- `foundation/` — shared package: auth, policy, audit, workflow, payments connector, UI partials
- `apps/` — FastAPI entrypoint (`apps/main.py`) and per-app packages (`apps/refunds/`)
- `alembic/` — migrations
- `tests/`
