"""Refunds UI: dashboard cards and flags, detail page, HTMX actions and readable errors."""

import re

from sqlalchemy import text

from apps.refunds import seed
from apps.refunds.models import RefundRequest
from foundation.connectors.payments import get_payment_provider
from foundation.db import SessionLocal, engine
from foundation.models import AuditLog
from tests.refunds.factories import make_refund, make_transaction, refund_state
from tests.refunds.helpers import client_as

HX = {"HX-Request": "true"}


def card(html: str, label: str) -> str:
    match = re.search(
        rf'summary-label">{re.escape(label)}</div>\s*<div class="summary-value">([^<]*)<', html
    )
    assert match, f"no summary card {label!r}"
    return match.group(1)


def row(html: str, refund_id: int) -> str:
    match = re.search(rf'<tr[^>]*>\s*<td><a href="/refunds/{refund_id}">.*?</tr>', html, re.S)
    assert match, f"no row for refund {refund_id}"
    return match.group(0)


def age(refund_id: int, interval: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE refund_requests SET updated_at = now() - interval '{interval}' "
                "WHERE id = :id"
            ),
            {"id": refund_id},
        )


def section(html: str, element_id: str) -> str:
    return html.split(f'id="{element_id}"', 1)[1].split("</div>", 1)[0]


def test_dashboard_summary_cards(database):
    make_refund(status="submitted")
    make_refund(status="submitted")
    approved_now = make_refund(status="submitted")
    assert client_as("bob").post(f"/refunds/{approved_now}/approve").status_code == 200
    stale = make_refund(status="approved")
    age(stale, "2 minutes")
    make_refund(status="approved")  # approved seconds ago: not yet needing attention
    make_refund(status="payment_failed")
    make_refund(status="payment_unknown")
    old_paid = make_refund(30_000, status="paid")
    age(old_paid, "8 days")

    html = client_as("carol").get("/refunds").text
    assert card(html, "Pending review") == "2"
    assert card(html, "Approved today") == "1"
    assert card(html, "Payouts needing attention") == "3"
    assert card(html, "Refunded, last 7 days") == "$50.00"  # the refund paid just now
    assert "summary-card alert" in html


def test_dashboard_flags_high_value_and_attention_rows(database):
    small = make_refund(5_000)
    big = make_refund(60_000)
    unknown = make_refund(status="payment_unknown")
    html = client_as("bob").get("/refunds").text
    assert "flag-" not in row(html, small)
    assert 'class="flagged"' in row(html, big) and "over $500" in row(html, big)
    assert 'class="flagged attention"' in row(html, unknown)
    assert "needs attention" in row(html, unknown)


def test_dashboard_status_filter(database):
    submitted = make_refund()
    paid = make_refund(status="paid")
    html = client_as("bob").get("/refunds?status=paid").text
    assert f'href="/refunds/{paid}"' in html
    assert f'href="/refunds/{submitted}"' not in html
    assert '<option value="paid" selected>' in html


def test_provider_control_is_admin_only_and_labelled_demo(database):
    assert "Demo control" in client_as("carol").get("/refunds").text
    for user in ("alice", "bob"):
        assert 'id="provider-mode"' not in client_as(user).get("/refunds").text


def test_new_refund_link_only_for_agents(database):
    assert 'href="/refunds/new"' in client_as("alice").get("/refunds").text
    assert 'href="/refunds/new"' not in client_as("bob").get("/refunds").text


def test_detail_shows_transaction_payout_and_history(database):
    tx = make_transaction(20_000, customer="Globex")
    refund_id = make_refund(5_000, transaction_id=tx)
    client_as("bob").post(f"/refunds/{refund_id}/approve")
    html = client_as("bob").get(f"/refunds/{refund_id}").text
    assert "Globex" in html and "$200.00" in html
    assert "$150.00" in html  # still refundable
    assert 'id="payout-status">succeeded' in html
    assert 'id="payout-attempts">1<' in html
    assert 'id="ledger-payments">1 payment for this key' in html
    assert "payout_succeeded" in html and ">system<" in html


def test_detail_actions_reflect_the_current_user(database):
    small = make_refund(5_000, requested_by="alice")
    big = make_refund(60_000, requested_by="alice")

    alice = client_as("alice").get(f"/refunds/{small}").text
    assert 'id="actions-allowed"' not in alice
    assert "reviewers and admins only" in section(alice, "actions-denied")

    bob_small = client_as("bob").get(f"/refunds/{small}").text
    assert "Approve" in section(bob_small, "actions-allowed")
    assert "Reject" in section(bob_small, "actions-allowed")

    bob_big = client_as("bob").get(f"/refunds/{big}").text
    assert "Approve" not in section(bob_big, "actions-allowed")
    assert "admin approval required" in section(bob_big, "actions-denied")

    carol_big = client_as("carol").get(f"/refunds/{big}").text
    assert "Approve" in section(carol_big, "actions-allowed")

    unknown = make_refund(status="payment_unknown")
    bob_unknown = client_as("bob").get(f"/refunds/{unknown}").text
    assert "Resume payout" in section(bob_unknown, "actions-allowed")


def test_htmx_approve_returns_the_panel_fragment(database):
    refund_id = make_refund()
    response = client_as("bob").post(f"/refunds/{refund_id}/approve", headers=HX)
    assert response.status_code == 200
    assert "<html" not in response.text
    assert response.text.lstrip().startswith('<div id="refund-panel">')
    assert "Refund approved; payout paid." in response.text


def test_htmx_403_is_a_readable_message_and_is_in_the_refund_history(database):
    refund_id = make_refund()
    response = client_as("alice").post(f"/refunds/{refund_id}/approve", headers=HX)
    assert response.status_code == 403
    assert response.headers["HX-Retarget"] == "#messages"
    assert "<html" not in response.text and '"detail"' not in response.text
    assert "Not allowed." in response.text
    assert "Alice (agent) is not permitted to do this (refunds.approve)" in response.text
    assert refund_state(refund_id)[0] == "submitted"
    history = client_as("carol").get(f"/refunds/{refund_id}").text.split("Audit history")[1]
    assert "denied" in history and "alice" in history


def test_htmx_409_is_a_readable_message(database):
    refund_id = make_refund()
    client_as("bob").post(f"/refunds/{refund_id}/approve")
    response = client_as("frank").post(f"/refunds/{refund_id}/approve", headers=HX)
    assert response.status_code == 409
    assert response.headers["HX-Retarget"] == "#messages"
    assert "Conflict." in response.text and "reload" in response.text


def test_plain_browser_403_renders_an_error_page(database):
    refund_id = make_refund()
    response = client_as("alice").post(
        f"/refunds/{refund_id}/approve", headers={"accept": "text/html"}
    )
    assert response.status_code == 403
    assert "<html" in response.text and "Not allowed" in response.text


def test_htmx_submit_in_dollars_redirects_to_the_detail_page(database):
    tx = make_transaction(50_000)
    response = client_as("alice").post(
        "/refunds", data={"transaction_id": tx, "amount": "120", "reason": "demo"}, headers=HX
    )
    assert response.status_code == 200
    refund_id = int(response.headers["HX-Redirect"].rsplit("/", 1)[1])
    state, _ = refund_state(refund_id)
    assert state == "submitted"
    with SessionLocal() as session:
        assert session.get(RefundRequest, refund_id).amount_cents == 12_000


def test_htmx_submit_errors_are_readable(database):
    tx = make_transaction(10_000)
    alice = client_as("alice")
    for amount, expected in (
        ("1.234", "at most two decimals"),
        ("abc", "not an amount"),
        ("0", "greater than zero"),
        ("", "Enter an amount"),
    ):
        response = alice.post(
            "/refunds", data={"transaction_id": tx, "amount": amount, "reason": "x"}, headers=HX
        )
        assert response.status_code == 422, amount
        assert expected in response.text
        assert response.headers["HX-Retarget"] == "#messages"
    over = alice.post(
        "/refunds", data={"transaction_id": tx, "amount": "150", "reason": "x"}, headers=HX
    )
    assert over.status_code == 409 and "Conflict." in over.text
    assert "remaining balance of $100.00" in over.text
    assert "cents" not in over.text and "reload" not in over.text
    missing = alice.post("/refunds", data={"transaction_id": tx, "amount": "5"}, headers=HX)
    assert missing.status_code == 422 and "reason: field required" in missing.text


def test_plain_browser_over_balance_error_is_in_dollars_without_reload_hint(database):
    tx = make_transaction(7_200)
    response = client_as("alice").post(
        "/refunds",
        data={"transaction_id": tx, "amount": "80", "reason": "x"},
        headers={"accept": "text/html"},
    )
    assert response.status_code == 409 and "<html" in response.text
    assert "remaining balance of $72.00" in response.text
    assert "reload" not in response.text


def test_htmx_provider_mode_returns_the_control(database):
    response = client_as("carol").post(
        "/refunds/provider-mode", data={"mode": "timeout_after_success"}, headers=HX
    )
    assert response.status_code == 200
    assert 'id="current-mode">timeout_after_success' in response.text
    assert "Mock provider mode set to timeout_after_success." in response.text
    assert get_payment_provider().mode == "timeout_after_success"
    denied = client_as("bob").post("/refunds/provider-mode", data={"mode": "ok"}, headers=HX)
    assert denied.status_code == 403 and "Not allowed." in denied.text


def test_root_redirects_to_refunds(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/refunds"


def test_seed_writes_a_history_for_every_refund(database):
    with SessionLocal() as session, session.begin():
        assert seed.seed(session)
    with SessionLocal() as session:
        rows = session.query(AuditLog).filter(AuditLog.entity == "refund").all()
    assert {r.action for r in rows} >= {
        "submit",
        "approve",
        "reject",
        "payout_succeeded",
        "payout_failed",
        "payout_unknown",
    }
    assert {r.actor_id for r in rows if r.action.startswith("payout_")} == {"system"}
    html = client_as("carol").get("/refunds").text
    assert int(card(html, "Payouts needing attention")) >= 5
