"""R1-R5 and the state machine, through the HTTP routes."""

import pytest

from apps.refunds import rules
from tests.conftest import denied_entries
from tests.refunds.factories import (
    audit_actions,
    ledger_rows,
    make_refund,
    make_transaction,
    refund_state,
)
from tests.refunds.helpers import client_as


def submit(user: str, transaction_id: int, amount_cents: int):
    return client_as(user).post(
        "/refunds",
        data={"transaction_id": transaction_id, "amount_cents": amount_cents, "reason": "x"},
    )


def test_rules_helpers():
    assert rules.idempotency_key(42) == "refund-42"
    assert not rules.requires_admin(50_000)
    assert rules.requires_admin(50_001)
    assert not rules.exceeds_balance(10_000, 6_000, 4_000)
    assert rules.exceeds_balance(10_000, 6_000, 4_001)
    assert set(rules.RESUMABLE_STATUSES) == {"approved", "payment_failed", "payment_unknown"}
    assert set(rules.PAYOUT_OUTCOME_FROM) | {
        "submitted",
        "partially_approved",
        "approved",
        "rejected",
    } == set(rules.REFUND_STATUSES)


# R1
@pytest.mark.parametrize("user", ["bob", "carol", "dave"])
def test_r1_only_agents_submit(database, user):
    tx = make_transaction()
    assert submit(user, tx, 1_000).status_code == 403


def test_r1_agent_submits(database):
    tx = make_transaction()
    response = submit("alice", tx, 1_000)
    assert response.status_code == 201
    assert 'id="refund-status">submitted' in response.text


@pytest.mark.parametrize("amount", [0, -5])
def test_submit_rejects_non_positive_amounts(database, amount):
    assert submit("alice", make_transaction(), amount).status_code == 422


def test_submit_unknown_transaction_is_404(database):
    assert submit("alice", 999_999, 1_000).status_code == 404


# R2
@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_r2_requester_cannot_review_own_refund(database, db_session, verb):
    refund_id = make_refund(requested_by="bob")
    response = client_as("bob").post(f"/refunds/{refund_id}/{verb}")
    assert response.status_code == 403
    assert refund_state(refund_id)[0] == "submitted"
    assert len(denied_entries(db_session)) == 1


# R3
def test_r3_over_refund_blocked(database):
    tx = make_transaction(10_000)
    assert submit("alice", tx, 6_000).status_code == 201
    response = submit("erin", tx, 5_000)
    assert response.status_code == 409
    assert "remaining balance of 4000" in response.text
    assert submit("erin", tx, 4_000).status_code == 201
    assert submit("erin", tx, 1).status_code == 409


def test_r3_rejected_refunds_do_not_count(database):
    tx = make_transaction(10_000)
    make_refund(8_000, status="rejected", transaction_id=tx)
    assert submit("alice", tx, 10_000).status_code == 201


def test_r3_failed_and_unknown_payouts_still_count(database):
    tx = make_transaction(10_000)
    make_refund(5_000, status="payment_failed", transaction_id=tx)
    make_refund(5_000, status="payment_unknown", transaction_id=tx)
    assert submit("alice", tx, 1).status_code == 409


# R4
def test_r4_reviewer_cannot_approve_over_500(database, db_session):
    refund_id = make_refund(50_001)
    response = client_as("bob").post(f"/refunds/{refund_id}/approve")
    assert response.status_code == 403
    assert refund_state(refund_id)[0] == "submitted"
    assert [e.entity_id for e in denied_entries(db_session)] == ["refunds.approve_high_value"]


def test_r4_reviewer_can_approve_exactly_500(database):
    refund_id = make_refund(50_000)
    assert client_as("bob").post(f"/refunds/{refund_id}/approve").status_code == 200
    assert refund_state(refund_id)[0] == "paid"


def test_r4_admin_approves_over_500(database):
    refund_id = make_refund(75_000)
    assert client_as("carol").post(f"/refunds/{refund_id}/approve").status_code == 200
    assert refund_state(refund_id)[0] == "paid"
    assert ledger_rows(refund_id) == 1


def test_reviewer_can_reject_over_500(database):
    refund_id = make_refund(75_000)
    assert client_as("bob").post(f"/refunds/{refund_id}/reject").status_code == 200
    assert refund_state(refund_id)[0] == "rejected"


# State machine: any other transition -> 409
@pytest.mark.parametrize(
    "status", ["approved", "rejected", "paid", "payment_failed", "payment_unknown"]
)
@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_review_only_from_submitted(database, status, verb):
    refund_id = make_refund(status=status)
    assert client_as("carol").post(f"/refunds/{refund_id}/{verb}").status_code == 409
    assert refund_state(refund_id)[0] == status
    assert audit_actions(refund_id) == []


@pytest.mark.parametrize("status", ["submitted", "rejected", "paid"])
def test_resume_only_from_resumable(database, status):
    refund_id = make_refund(status=status)
    assert client_as("bob").post(f"/refunds/{refund_id}/resume-payout").status_code == 409
    assert refund_state(refund_id)[0] == status
    assert ledger_rows(refund_id) == 0


def test_unknown_refund_is_404(database):
    assert client_as("carol").post("/refunds/999999/approve").status_code == 404
    assert client_as("carol").get("/refunds/999999").status_code == 404


def test_views(database):
    refund_id = make_refund()
    agent = client_as("alice")
    assert agent.get("/refunds").status_code == 200
    assert agent.get("/refunds?status=submitted").status_code == 200
    assert agent.get("/refunds/new").status_code == 200
    detail = agent.get(f"/refunds/{refund_id}")
    assert detail.status_code == 200
    assert 'id="actions-allowed"' not in detail.text
    assert 'id="actions-denied"' in detail.text
    bob_detail = client_as("bob").get(f"/refunds/{refund_id}").text
    assert "Approve" in bob_detail.split('id="actions-allowed"')[1].split("</div>")[0]
