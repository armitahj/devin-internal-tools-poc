"""Every state-changing /flags route writes the audit rows for its change.

The table must list every POST route under /flags, so a new route without an entry fails.
"""

import pytest

from apps.main import app
from foundation.db import SessionLocal
from foundation.models import AuditLog
from foundation.policy import api_routes
from tests.feature_flags.factories import make_flag, make_request
from tests.refunds.helpers import client_as


def _toggle():
    return "dave", f"/flags/{make_flag('staging')}/toggle", {"enabled": "true"}


def _request():
    return "dave", f"/flags/{make_flag('prod')}/request-change", {"enabled": "true"}


def _approve():
    return "carol", f"/flags/requests/{make_request(requested_by='dave')}/approve", None


def _reject():
    return "gwen", f"/flags/requests/{make_request(requested_by='dave')}/reject", None


# path -> (setup, [(entity, action)] expected on success, all written in one transaction)
STATE_CHANGING_ROUTES = {
    "/flags/{flag_id}/toggle": (_toggle, [("flag", "toggle")]),
    "/flags/{flag_id}/request-change": (_request, [("flag_change_request", "request")]),
    "/flags/requests/{request_id}/approve": (
        _approve,
        [("flag_change_request", "approve"), ("flag", "apply_change")],
    ),
    "/flags/requests/{request_id}/reject": (_reject, [("flag_change_request", "reject")]),
}


def test_every_state_changing_flags_route_is_covered():
    posts = {r.path for r in api_routes(app) if r.path.startswith("/flags") and "POST" in r.methods}
    assert posts == set(STATE_CHANGING_ROUTES)


@pytest.mark.parametrize("path", sorted(STATE_CHANGING_ROUTES))
def test_state_changing_route_writes_its_audit_rows(database, path):
    setup, expected = STATE_CHANGING_ROUTES[path]
    user, url, data = setup()

    response = client_as(user).post(url, data=data)
    assert response.status_code == 303, response.text

    with SessionLocal() as session:
        rows = session.query(AuditLog).order_by(AuditLog.id).all()
    assert [(r.entity, r.action) for r in rows] == expected
    assert {r.actor_id for r in rows} == {user}
