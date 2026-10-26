"""Every state-changing refunds route writes exactly one audit row per committed state change.

The table below must list every POST route under /refunds, so a new state-changing route
without an entry (and therefore without an audit assertion) fails the suite.
"""

import pytest

from apps.main import app
from foundation.connectors.payments import get_payment_provider
from foundation.db import SessionLocal
from foundation.models import AuditLog
from foundation.policy import api_routes
from tests.refunds.factories import make_refund, make_transaction, refund_state
from tests.refunds.helpers import client_as


def _submit():
    tx = make_transaction(10_000)
    return "alice", "/refunds", {"transaction_id": tx, "amount_cents": 1_000, "reason": "x"}


def _approve():
    return "bob", f"/refunds/{make_refund()}/approve", None


def _reject():
    return "bob", f"/refunds/{make_refund()}/reject", None


def _resume():
    return "bob", f"/refunds/{make_refund(status='payment_failed')}/resume-payout", None


def _provider_mode():
    return "carol", "/refunds/provider-mode", {"mode": "fail"}


# path -> (setup, audit actions expected on success: one per committed state change)
STATE_CHANGING_ROUTES = {
    "/refunds": (_submit, ["submit"]),
    # Two committed transactions (R6): the approval, then the payout result.
    "/refunds/{refund_id}/approve": (_approve, ["approve", "payout_succeeded"]),
    "/refunds/{refund_id}/reject": (_reject, ["reject"]),
    "/refunds/{refund_id}/resume-payout": (_resume, ["payout_succeeded"]),
    "/refunds/provider-mode": (_provider_mode, ["set_mode"]),
}


def test_every_state_changing_refunds_route_is_covered():
    posts = {
        r.path for r in api_routes(app) if r.path.startswith("/refunds") and "POST" in r.methods
    }
    assert posts == set(STATE_CHANGING_ROUTES)


@pytest.mark.parametrize("path", sorted(STATE_CHANGING_ROUTES))
def test_state_changing_route_writes_one_audit_row_per_change(database, path):
    setup, expected = STATE_CHANGING_ROUTES[path]
    user, url, data = setup()

    response = client_as(user).post(url, data=data)
    assert response.status_code in (200, 201, 303), response.text

    with SessionLocal() as session:
        rows = session.query(AuditLog).filter(AuditLog.action != "seed").order_by(AuditLog.id)
        rows = rows.all()
    assert [r.action for r in rows] == expected
    assert all(r.actor_id == user for r in rows)
    assert len({r.entity_id for r in rows}) == 1
    if rows[0].entity == "refund":
        status, _ = refund_state(int(rows[0].entity_id))
        assert rows[-1].after["status"] == status
    get_payment_provider().mode = "ok"
