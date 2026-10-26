"""R8: refunds over $1,000 need two distinct approvers, neither of them the requester."""

import pytest
from fastapi import HTTPException

from apps.refunds import rules, service
from foundation.db import SessionLocal
from foundation.models import User
from tests.conftest import denied_entries
from tests.refunds.factories import (
    audit_actions,
    audit_rows,
    ledger_rows,
    make_refund,
    refund_state,
)
from tests.refunds.helpers import client_as, run_concurrently
from tests.refunds.test_ui import card

BIG = 150_000  # $1,500


def approve(user: str, refund_id: int):
    return client_as(user).post(f"/refunds/{refund_id}/approve")


def approvals(refund_id: int) -> tuple[str | None, str | None]:
    from apps.refunds.models import RefundRequest

    with SessionLocal() as session:
        refund = session.get(RefundRequest, refund_id)
        return refund.first_approved_by, refund.reviewed_by


def test_rules_helpers():
    assert not rules.requires_two_approvers(100_000)
    assert rules.requires_two_approvers(100_001)
    assert rules.approval_step(100_000, "submitted") == ("submitted", "approved")
    assert rules.approval_step(100_001, "submitted") == ("submitted", "partially_approved")
    assert rules.approval_step(100_001, "partially_approved") == (
        "partially_approved",
        "approved",
    )
    # R4 with R8: an admin must be among the approvers of anything over $500.
    assert rules.admin_approval_satisfied(50_000, False, None)
    assert not rules.admin_approval_satisfied(60_000, False, None)
    assert rules.admin_approval_satisfied(BIG, False, None)  # first of two
    assert not rules.admin_approval_satisfied(BIG, False, False)
    assert rules.admin_approval_satisfied(BIG, False, True)
    assert rules.admin_approval_satisfied(BIG, True, False)


def test_one_approver_is_not_enough(database):
    refund_id = make_refund(BIG)

    response = approve("bob", refund_id)
    assert response.status_code == 200
    assert "First approval recorded (1 of 2)" in response.text
    status, payout = refund_state(refund_id)
    assert (status, payout) == ("partially_approved", None)
    assert ledger_rows(refund_id) == 0
    assert approvals(refund_id) == ("bob", None)
    assert audit_actions(refund_id) == ["partially_approve"]

    assert approve("carol", refund_id).status_code == 200
    status, payout = refund_state(refund_id)
    assert (status, payout.status) == ("paid", "succeeded")
    assert ledger_rows(refund_id) == 1
    assert approvals(refund_id) == ("bob", "carol")
    assert audit_actions(refund_id) == ["partially_approve", "approve", "payout_succeeded"]
    approve_row = audit_rows(refund_id)[1]
    assert approve_row.actor_id == "carol"
    assert approve_row.before == {"status": "partially_approved", "first_approved_by": "bob"}
    assert approve_row.after["approvers"] == ["bob", "carol"]


@pytest.mark.parametrize("first", ["bob", "carol"])
def test_same_person_cannot_approve_twice(database, db_session, first):
    refund_id = make_refund(BIG)
    assert approve(first, refund_id).status_code == 200

    response = approve(first, refund_id)
    assert response.status_code == 403
    assert refund_state(refund_id) == ("partially_approved", None)
    assert ledger_rows(refund_id) == 0
    assert audit_actions(refund_id) == ["partially_approve"]
    [denied] = denied_entries(db_session, "refunds.approve")
    assert denied.actor_id == first and "already approved" in denied.after["reason"]


def test_same_person_cannot_approve_twice_concurrently(database):
    refund_id = make_refund(BIG, status="partially_approved", first_approved_by="carol")
    results = run_concurrently(*[lambda: approve("carol", refund_id)] * 2)
    assert [r.status_code for r in results] == [403, 403]
    assert refund_state(refund_id) == ("partially_approved", None)


@pytest.mark.parametrize(
    ("requester", "first", "second"),
    [
        ("bob", "bob", None),  # requester as first approver
        ("bob", "carol", "bob"),  # requester as second approver
        ("carol", "bob", "carol"),  # an admin requester cannot supply the admin approval
    ],
)
def test_requester_cannot_be_either_approver(database, db_session, requester, first, second):
    refund_id = make_refund(BIG, requested_by=requester)
    if second is None:
        assert approve(first, refund_id).status_code == 403
        assert refund_state(refund_id)[0] == "submitted"
        assert audit_actions(refund_id) == []
    else:
        assert approve(first, refund_id).status_code == 200
        assert approve(second, refund_id).status_code == 403
        assert refund_state(refund_id) == ("partially_approved", None)
        assert audit_actions(refund_id) == ["partially_approve"]
    assert ledger_rows(refund_id) == 0
    [denied] = denied_entries(db_session)
    assert denied.actor_id == requester and "own" in denied.after["reason"]


def test_db_rejects_the_requester_or_a_repeat_as_approver(database):
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError

    from apps.refunds.models import RefundRequest

    refund_id = make_refund(BIG, status="partially_approved", first_approved_by="bob")
    for values in (
        {"first_approved_by": "alice"},  # alice is the requester
        {"status": "approved", "reviewed_by": "bob"},  # bob already approved
    ):
        with SessionLocal() as session, pytest.raises(IntegrityError):
            session.execute(
                update(RefundRequest).where(RefundRequest.id == refund_id).values(**values)
            )
            session.commit()


@pytest.mark.parametrize(("amount", "approver"), [(100_000, "carol"), (50_000, "bob")])
def test_up_to_1000_still_needs_one_approval(database, amount, approver):
    refund_id = make_refund(amount)
    assert approve(approver, refund_id).status_code == 200
    assert refund_state(refund_id)[0] == "paid"
    assert ledger_rows(refund_id) == 1
    assert approvals(refund_id) == (None, approver)
    assert audit_actions(refund_id) == ["approve", "payout_succeeded"]


def test_two_reviewers_without_an_admin_cannot_approve(database, db_session):
    refund_id = make_refund(BIG)
    assert approve("bob", refund_id).status_code == 200
    assert approve("frank", refund_id).status_code == 403
    assert refund_state(refund_id) == ("partially_approved", None)
    assert [e.entity_id for e in denied_entries(db_session)] == ["refunds.approve_high_value"]


def test_admin_first_then_reviewer_second(database):
    refund_id = make_refund(BIG)
    assert approve("carol", refund_id).status_code == 200
    assert approve("frank", refund_id).status_code == 200
    assert refund_state(refund_id)[0] == "paid"
    assert approvals(refund_id) == ("carol", "frank")


def test_partially_approved_refund_can_be_rejected(database):
    refund_id = make_refund(BIG, status="partially_approved", first_approved_by="bob")
    assert client_as("frank").post(f"/refunds/{refund_id}/reject").status_code == 200
    assert refund_state(refund_id) == ("rejected", None)
    [row] = audit_rows(refund_id)
    assert (row.action, row.before, row.after["status"]) == (
        "reject",
        {"status": "partially_approved"},
        "rejected",
    )
    assert approve("carol", refund_id).status_code == 409


def test_concurrent_first_approvals_one_wins_without_payout(database):
    refund_id = make_refund(BIG)
    results = run_concurrently(
        lambda: approve("bob", refund_id), lambda: approve("frank", refund_id)
    )
    # The loser gets 409 if it raced the same conditional UPDATE, or 403 if it read the
    # winner's committed partial approval (two reviewers without an admin).
    codes = sorted(r.status_code for r in results)
    assert codes in ([200, 403], [200, 409])
    assert refund_state(refund_id) == ("partially_approved", None)
    assert audit_actions(refund_id) == ["partially_approve"]


def test_concurrent_second_approvals_pay_exactly_once(database):
    refund_id = make_refund(BIG, status="partially_approved", first_approved_by="carol")
    results = run_concurrently(
        lambda: approve("bob", refund_id), lambda: approve("frank", refund_id)
    )
    assert sorted(r.status_code for r in results) == [200, 409]
    status, payout = refund_state(refund_id)
    assert (status, payout.status) == ("paid", "succeeded")
    assert ledger_rows(refund_id) == 1
    assert audit_actions(refund_id).count("approve") == 1
    assert audit_actions(refund_id).count("payout_succeeded") == 1


def test_concurrent_second_approvals_in_the_service_pay_exactly_once(database):
    refund_id = make_refund(BIG, status="partially_approved", first_approved_by="carol")

    def approve_as(user_id):
        def call():
            with SessionLocal() as session:
                service.approve(session, refund_id, session.get(User, user_id), _provider())
                return "ok"

        return call

    results = run_concurrently(approve_as("bob"), approve_as("frank"))
    assert results.count("ok") == 1
    [error] = [r for r in results if r != "ok"]
    assert isinstance(error, HTTPException) and error.status_code == 409
    assert refund_state(refund_id)[0] == "paid"
    assert ledger_rows(refund_id) == 1


def _provider():
    from foundation.connectors.payments import get_payment_provider

    return get_payment_provider()


def section(html: str, element_id: str) -> str:
    return html.split(f'id="{element_id}"', 1)[1].split("</div>", 1)[0]


def test_detail_shows_approvers_and_allows_the_second_approval(database):
    submitted = make_refund(BIG)
    bob_submitted = client_as("bob").get(f"/refunds/{submitted}").text
    assert "Approve (1 of 2)" in section(bob_submitted, "actions-allowed")
    assert 'id="approvals">none yet (0 of 2)' in bob_submitted
    assert "two approvers, one an admin" in bob_submitted

    refund_id = make_refund(BIG, status="partially_approved", first_approved_by="bob")
    carol = client_as("carol").get(f"/refunds/{refund_id}").text
    assert 'id="approvals">bob (1 of 2)' in carol
    assert "Approve (2 of 2)" in section(carol, "actions-allowed")

    bob = client_as("bob").get(f"/refunds/{refund_id}").text
    assert "you gave the first approval" in section(bob, "actions-denied")
    assert "Reject" in section(bob, "actions-allowed")

    frank = client_as("frank").get(f"/refunds/{refund_id}").text
    assert "the second approval must be an admin" in section(frank, "actions-denied")

    alice = client_as("alice").get(f"/refunds/{refund_id}").text
    assert 'id="actions-allowed"' not in alice

    html = (
        client_as("carol")
        .post(f"/refunds/{refund_id}/approve", headers={"HX-Request": "true"})
        .text
    )
    assert 'id="refund-status">Paid' in html
    assert 'id="approvals">bob, carol (2 of 2)' in html


def test_dashboard_lists_partially_approved_refunds(database):
    refund_id = make_refund(BIG, status="partially_approved", first_approved_by="bob")
    make_refund()
    html = client_as("carol").get("/refunds").text
    assert "over $1,000: 2 approvers" in html
    assert "bob (1 of 2)" in html
    assert '<option value="partially_approved"' in html
    filtered = client_as("carol").get("/refunds?status=partially_approved").text
    assert f'href="/refunds/{refund_id}"' in filtered
    assert card(html, "Pending review") == "2"
