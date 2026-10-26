"""Feature-flag rules: direct non-prod toggles, prod change requests, separation of duties."""

import pytest
from sqlalchemy.exc import IntegrityError

from apps.feature_flags import rules
from apps.feature_flags.models import Flag, FlagChangeRequest
from foundation.db import SessionLocal
from tests.conftest import denied_entries
from tests.feature_flags.factories import (
    flag_enabled,
    make_flag,
    make_request,
    request_flag,
    request_state,
)
from tests.refunds.factories import audit_actions, audit_rows
from tests.refunds.helpers import client_as


def test_rules_constants():
    assert rules.ENVIRONMENTS == ("dev", "staging", "prod")
    assert [rules.needs_approval(e) for e in rules.ENVIRONMENTS] == [False, False, True]
    assert rules.REQUEST_STATUSES == ("pending", "approved", "rejected")
    assert rules.DECIDABLE_STATUSES == ("pending",)


@pytest.mark.parametrize("environment", rules.NONPROD_ENVIRONMENTS)
@pytest.mark.parametrize("user", ["dave", "gwen", "carol"])
def test_nonprod_toggle_by_engineer_or_admin_is_direct_and_audited(database, environment, user):
    flag_id = make_flag(environment, enabled=False)

    response = client_as(user).post(f"/flags/{flag_id}/toggle", data={"enabled": "true"})

    assert response.status_code == 303
    assert flag_enabled(flag_id) is True
    [row] = audit_rows(flag_id, entity="flag")
    assert (row.actor_id, row.action) == (user, "toggle")
    assert (row.before, row.after["enabled"]) == ({"enabled": False}, True)


def test_toggle_back_off_is_audited_again(database):
    flag_id = make_flag("staging", enabled=True)
    client = client_as("dave")
    assert client.post(f"/flags/{flag_id}/toggle", data={"enabled": "false"}).status_code == 303
    assert client.post(f"/flags/{flag_id}/toggle", data={"enabled": "true"}).status_code == 303
    assert audit_actions(flag_id, entity="flag") == ["toggle", "toggle"]
    assert flag_enabled(flag_id) is True


@pytest.mark.parametrize("user", ["dave", "carol"])
def test_prod_flag_cannot_be_toggled_directly_even_by_admin(database, db_session, user):
    flag_id = make_flag("prod", enabled=False)

    response = client_as(user).post(f"/flags/{flag_id}/toggle", data={"enabled": "true"})

    assert response.status_code == 403
    assert flag_enabled(flag_id) is False
    assert audit_actions(flag_id, entity="flag") == []
    [denied] = denied_entries(db_session, "flags.toggle_nonprod")
    assert denied.actor_id == user
    assert denied.after["path"] == f"/flags/{flag_id}/toggle"


def test_toggle_to_current_value_is_409(database):
    flag_id = make_flag("dev", enabled=True)
    response = client_as("dave").post(f"/flags/{flag_id}/toggle", data={"enabled": "true"})
    assert response.status_code == 409
    assert audit_actions(flag_id, entity="flag") == []


def test_toggle_unknown_flag_is_404(database):
    assert client_as("dave").post("/flags/999/toggle", data={"enabled": "true"}).status_code == 404


@pytest.mark.parametrize("user", ["alice", "bob"])
def test_non_engineers_cannot_toggle(database, user):
    flag_id = make_flag("dev")
    response = client_as(user).post(f"/flags/{flag_id}/toggle", data={"enabled": "true"})
    assert response.status_code == 403
    assert flag_enabled(flag_id) is False


def test_engineer_requests_prod_change_and_flag_does_not_change(database):
    flag_id = make_flag("prod", enabled=False)

    response = client_as("dave").post(f"/flags/{flag_id}/request-change", data={"enabled": "true"})

    assert response.status_code == 303
    with SessionLocal() as session:
        [req] = session.query(FlagChangeRequest).all()
    assert (req.flag_id, req.desired_enabled, req.requested_by, req.status) == (
        flag_id,
        True,
        "dave",
        "pending",
    )
    assert flag_enabled(flag_id) is False
    [row] = audit_rows(req.id, entity="flag_change_request")
    assert (row.actor_id, row.action, row.after["status"]) == ("dave", "request", "pending")


def test_admin_cannot_request_prod_change(database):
    flag_id = make_flag("prod")
    response = client_as("carol").post(f"/flags/{flag_id}/request-change", data={"enabled": "true"})
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("environment", "enabled", "pending", "detail"),
    [
        ("dev", False, False, "toggle it directly"),
        ("prod", True, False, "already on"),
        ("prod", False, True, "already has pending change request"),
    ],
)
def test_invalid_change_requests_are_409(database, environment, enabled, pending, detail):
    flag_id = make_flag(environment, enabled=enabled)
    if pending:
        make_request(flag_id, requested_by="gwen")

    response = client_as("dave").post(f"/flags/{flag_id}/request-change", data={"enabled": "true"})

    assert response.status_code == 409
    assert detail in response.json()["detail"]
    assert flag_enabled(flag_id) is enabled


@pytest.mark.parametrize("approver", ["dave", "carol"])
def test_different_engineer_or_admin_approves_and_only_then_flag_changes(database, approver):
    request_id = make_request(requested_by="gwen", desired_enabled=True)
    flag_id = request_flag(request_id)
    assert flag_enabled(flag_id) is False

    response = client_as(approver).post(f"/flags/requests/{request_id}/approve")

    assert response.status_code == 303
    assert request_state(request_id) == ("approved", approver)
    assert flag_enabled(flag_id) is True
    assert audit_actions(request_id, entity="flag_change_request") == ["approve"]
    [applied] = audit_rows(flag_id, entity="flag")
    assert (applied.actor_id, applied.action) == (approver, "apply_change")
    assert applied.before == {"enabled": False}
    assert (applied.after["enabled"], applied.after["request_id"]) == (True, request_id)
    assert applied.after["requested_by"] == "gwen"


def test_approval_can_turn_a_prod_flag_off(database):
    flag_id = make_flag("prod", enabled=True)
    request_id = make_request(flag_id, desired_enabled=False, requested_by="gwen")
    assert client_as("dave").post(f"/flags/requests/{request_id}/approve").status_code == 303
    assert flag_enabled(flag_id) is False


@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_self_approval_and_self_rejection_blocked(database, db_session, verb):
    request_id = make_request(requested_by="dave")
    flag_id = request_flag(request_id)

    response = client_as("dave").post(f"/flags/requests/{request_id}/{verb}")

    assert response.status_code == 403
    assert request_state(request_id) == ("pending", None)
    assert flag_enabled(flag_id) is False
    assert audit_actions(request_id, entity="flag_change_request") == []
    [denied] = denied_entries(db_session)
    assert denied.actor_id == "dave"


def test_reject_leaves_flag_unchanged(database):
    request_id = make_request(requested_by="gwen")
    flag_id = request_flag(request_id)

    response = client_as("carol").post(f"/flags/requests/{request_id}/reject")

    assert response.status_code == 303
    assert request_state(request_id) == ("rejected", "carol")
    assert flag_enabled(flag_id) is False
    assert audit_actions(request_id, entity="flag_change_request") == ["reject"]
    assert audit_actions(flag_id, entity="flag") == []


def test_new_request_allowed_after_rejection(database):
    flag_id = make_flag("prod", enabled=False)
    make_request(flag_id, requested_by="gwen", status="rejected")
    response = client_as("dave").post(f"/flags/{flag_id}/request-change", data={"enabled": "true"})
    assert response.status_code == 303


@pytest.mark.parametrize("status", ["approved", "rejected"])
@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_invalid_transitions_are_409(database, status, verb):
    request_id = make_request(requested_by="gwen", status=status)
    flag_id = request_flag(request_id)

    response = client_as("dave").post(f"/flags/requests/{request_id}/{verb}")

    assert response.status_code == 409
    assert request_state(request_id) == (status, "carol")
    assert flag_enabled(flag_id) is False
    assert audit_actions(request_id, entity="flag_change_request") == []


def test_unknown_request_is_404(database):
    assert client_as("dave").post("/flags/requests/999/approve").status_code == 404


@pytest.mark.parametrize(
    "row",
    [
        lambda: Flag(key="x", environment="qa", enabled=True),
        lambda: FlagChangeRequest(
            flag_id=make_flag("prod"), desired_enabled=True, requested_by="dave", status="bogus"
        ),
    ],
    ids=["environment", "request status"],
)
def test_database_rejects_values_not_listed_in_rules(database, row):
    obj = row()
    with SessionLocal() as session, pytest.raises(IntegrityError):
        session.add(obj)
        session.commit()


def test_database_allows_one_pending_request_per_flag(database):
    flag_id = make_flag("prod")
    make_request(flag_id, requested_by="gwen")
    with pytest.raises(IntegrityError):
        make_request(flag_id, requested_by="dave")
