from types import SimpleNamespace

from sqlalchemy import select

from apps.refunds.models import RefundRequest
from foundation.db import SessionLocal
from foundation.ui.audit_format import describe, raw_json
from tests.refunds.factories import make_transaction
from tests.refunds.helpers import client_as


def entry(action="approve", before=None, after=None, entity_id="1"):
    return SimpleNamespace(action=action, before=before, after=after, entity_id=entity_id)


def test_submit_without_before_shows_just_the_values_with_money_in_dollars():
    lines = describe(entry("submit", after={"amount_cents": 110_000, "status": "submitted"}))
    assert lines == ["Status: submitted", "Amount: $1,100.00"]


def test_changes_show_before_and_after_and_skip_empty_values():
    lines = describe(
        entry(
            "payout_succeeded",
            before={"status": "approved", "payout_status": "pending", "attempts": 0},
            after={
                "status": "paid",
                "payout_status": "succeeded",
                "attempts": 1,
                "error": None,
                "provider_ref": "mock-1",
                "triggered_by": "carol",
            },
        )
    )
    assert lines == [
        "Status: approved → paid",
        "Payout status: pending → succeeded",
        "Attempts: 0 → 1",
        "Triggered by: carol",
        "Provider reference: mock-1",
    ]


def test_second_approval_keeps_the_first_approver():
    lines = describe(
        entry(
            before={"status": "partially_approved", "first_approved_by": "bob"},
            after={"status": "approved", "reviewed_by": "carol", "approvers": ["bob", "carol"]},
        )
    )
    assert "Status: partially_approved → approved" in lines
    assert "First approved by: bob" in lines
    assert "Approvers: bob, carol" in lines


def test_flag_change_uses_friendly_labels():
    lines = describe(
        entry(
            "apply_change",
            before={"enabled": False},
            after={"enabled": True, "key": "new_checkout", "environment": "prod"},
        )
    )
    assert lines == ["Enabled: off → on", "Flag: new_checkout", "Environment: prod"]


def test_denied_row_reads_as_one_sentence():
    denied = entry(
        "denied",
        after={"method": "GET", "path": "/refunds", "reason": "not signed in"},
        entity_id="refunds.view",
    )
    assert describe(denied) == ["Denied: GET /refunds — not signed in"]
    assert describe(entry("denied", entity_id="refunds.view")) == ["Denied: refunds.view"]


def test_raw_json_keeps_both_sides():
    assert '"before": null' in raw_json(entry("submit", after={"status": "submitted"}))


def test_audit_page_and_refund_history_are_readable_with_raw_json_toggle(database):
    tx = make_transaction(10_000)
    created = client_as("alice").post(
        "/refunds", data={"transaction_id": tx, "amount_cents": 7200, "reason": "x"}
    )
    assert created.status_code == 201
    with SessionLocal() as session:
        refund_id = session.scalar(
            select(RefundRequest.id).where(RefundRequest.transaction_id == tx)
        )
    assert client_as("carol").post(f"/refunds/{refund_id}/approve").status_code == 200

    audit = client_as("carol").get("/audit").text
    history = client_as("carol").get(f"/refunds/{refund_id}").text.split("Audit history")[1]
    for html in (audit, history):
        assert "Amount: $72.00" in html
        assert "Status: submitted → approved" in html
        assert "Payout status: pending → succeeded" in html
        assert "Triggered by: carol" in html
        assert "<details" in html and "Raw JSON" in html
        assert "&#34;amount_cents&#34;: 7200" in html
    assert "<th>Before</th>" not in audit
