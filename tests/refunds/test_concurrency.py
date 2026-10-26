"""R3 and R5 races, using real threads and separate DB connections."""

import time

from fastapi import HTTPException

from apps.refunds import service
from tests.refunds.factories import (
    audit_actions,
    ledger_rows,
    make_refund,
    make_transaction,
    refund_state,
)
from tests.refunds.helpers import client_as, run_concurrently


def test_r3_concurrent_submissions_exactly_one_succeeds(database, monkeypatch):
    tx = make_transaction(10_000)
    original = service.refunded_so_far

    def slow_refunded_so_far(session, transaction_id):
        # Widen the check-then-insert window: without the row lock both would pass.
        total = original(session, transaction_id)
        time.sleep(0.3)
        return total

    monkeypatch.setattr(service, "refunded_so_far", slow_refunded_so_far)

    def submit(user):
        return lambda: client_as(user).post(
            "/refunds", data={"transaction_id": tx, "amount_cents": 6_000, "reason": "x"}
        )

    results = run_concurrently(submit("alice"), submit("erin"))
    assert sorted(r.status_code for r in results) == [201, 409]
    assert audit_actions() == ["submit"]


def test_r3_concurrent_submissions_within_balance_both_succeed(database):
    tx = make_transaction(10_000)

    def submit(user):
        return lambda: client_as(user).post(
            "/refunds", data={"transaction_id": tx, "amount_cents": 5_000, "reason": "x"}
        )

    results = run_concurrently(submit("alice"), submit("erin"))
    assert [r.status_code for r in results] == [201, 201]


def test_r5_concurrent_approvals_one_200_one_409(database):
    refund_id = make_refund(10_000)
    results = run_concurrently(
        lambda: client_as("bob").post(f"/refunds/{refund_id}/approve"),
        lambda: client_as("frank").post(f"/refunds/{refund_id}/approve"),
    )
    assert sorted(r.status_code for r in results) == [200, 409]
    assert refund_state(refund_id)[0] == "paid"
    assert ledger_rows(refund_id) == 1
    assert audit_actions(refund_id) == ["approve", "payout_succeeded"]


def test_r5_concurrent_approve_and_reject(database):
    refund_id = make_refund(10_000)
    results = run_concurrently(
        lambda: client_as("bob").post(f"/refunds/{refund_id}/approve"),
        lambda: client_as("frank").post(f"/refunds/{refund_id}/reject"),
    )
    assert sorted(r.status_code for r in results) == [200, 409]
    status = refund_state(refund_id)[0]
    assert status in ("paid", "rejected")
    assert ledger_rows(refund_id) == (1 if status == "paid" else 0)


def test_concurrent_service_approvals_raise_409(database):
    from foundation.db import SessionLocal
    from foundation.models import User

    refund_id = make_refund(10_000)

    def approve(user_id):
        def call():
            with SessionLocal() as session:
                service.commit_approval(session, refund_id, session.get(User, user_id))
                return "ok"

        return call

    results = run_concurrently(approve("bob"), approve("frank"))
    assert results.count("ok") == 1
    [error] = [r for r in results if r != "ok"]
    assert isinstance(error, HTTPException) and error.status_code == 409
