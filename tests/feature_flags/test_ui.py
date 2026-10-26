"""The flags page: nav, sections per environment, per-role buttons, HTMX and readable errors."""

from apps.feature_flags.models import Flag, FlagChangeRequest
from apps.feature_flags.seed import seed
from foundation.db import SessionLocal
from tests.feature_flags.factories import flag_enabled, make_flag, make_request
from tests.refunds.helpers import client_as

HTMX = {"HX-Request": "true"}


def test_nav_link_shown_to_engineers_and_admins_only(database):
    assert 'href="/flags"' in client_as("dave").get("/audit").text
    assert 'href="/flags"' in client_as("carol").get("/refunds").text
    assert 'href="/flags"' not in client_as("bob").get("/refunds").text


def test_root_sends_engineers_to_flags(database):
    assert client_as("dave").get("/").headers["location"] == "/flags"
    assert client_as("carol").get("/").headers["location"] == "/refunds"


def test_page_lists_flags_by_environment_with_pending_requests(database):
    make_flag("dev", key="beta_banner", enabled=True)
    prod = make_flag("prod", key="new_checkout")
    request_id = make_request(prod, requested_by="gwen")

    html = client_as("dave").get("/flags").text

    for env in ("dev", "staging", "prod"):
        assert f'id="env-{env}"' in html
    assert "beta_banner" in html and "new_checkout" in html
    assert f"request #{request_id} pending" in html
    assert f"/flags/requests/{request_id}/approve" in html
    assert "Pending prod requests" in html


def test_buttons_follow_permissions(database):
    dev = make_flag("dev", enabled=False)
    prod = make_flag("prod", enabled=False)
    own = make_request(make_flag("prod", key="own_flag"), requested_by="dave")

    dave = client_as("dave").get("/flags").text
    assert f"/flags/{dev}/toggle" in dave
    assert f"/flags/{prod}/request-change" in dave
    assert f"/flags/requests/{own}/approve" not in dave
    assert "your request: another engineer or an admin must approve it" in dave

    carol = client_as("carol").get("/flags").text
    assert f"/flags/{dev}/toggle" in carol
    assert f"/flags/{prod}/request-change" not in carol
    assert f"/flags/requests/{own}/approve" in carol


def test_environment_filter(database):
    make_flag("dev", key="only_dev")
    make_flag("prod", key="only_prod")
    html = client_as("dave").get("/flags?environment=prod").text
    assert "only_prod" in html and "only_dev" not in html
    assert 'id="env-dev"' not in html


def test_htmx_toggle_returns_board_with_message(database):
    flag_id = make_flag("dev", key="beta_banner", enabled=False)
    response = client_as("dave").post(
        f"/flags/{flag_id}/toggle", data={"enabled": "true"}, headers=HTMX
    )
    assert response.status_code == 200
    assert "dev beta_banner turned on." in response.text
    assert "<html" not in response.text
    assert flag_enabled(flag_id) is True


def test_htmx_errors_are_readable(database):
    request_id = make_request(requested_by="dave")
    forbidden = client_as("dave").post(f"/flags/requests/{request_id}/approve", headers=HTMX)
    assert forbidden.status_code == 403
    assert forbidden.headers["HX-Retarget"] == "#messages"
    assert "You cannot act on your own request" in forbidden.text

    flag_id = make_flag("dev", enabled=True)
    conflict = client_as("dave").post(
        f"/flags/{flag_id}/toggle", data={"enabled": "true"}, headers=HTMX
    )
    assert conflict.status_code == 409
    assert "already on" in conflict.text and "reload" in conflict.text

    denied = client_as("alice").post(
        f"/flags/{flag_id}/toggle", data={"enabled": "false"}, headers=HTMX
    )
    assert "Alice (agent) is not permitted to do this (flags.toggle_nonprod)" in denied.text


def test_seed_creates_six_flags_across_all_environments(database):
    with SessionLocal() as session, session.begin():
        assert seed(session) is True
    with SessionLocal() as session, session.begin():
        assert seed(session) is False
    with SessionLocal() as session:
        flags = session.query(Flag).all()
        pending = session.query(FlagChangeRequest).filter_by(status="pending").all()
    assert len(flags) == 6
    assert {f.environment for f in flags} == {"dev", "staging", "prod"}
    assert [(r.requested_by, r.desired_enabled) for r in pending] == [("dave", True)]
    html = client_as("gwen").get("/flags").text
    assert f"/flags/requests/{pending[0].id}/approve" in html
