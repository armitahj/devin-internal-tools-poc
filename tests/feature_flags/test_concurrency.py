"""Races on prod approvals, prod requests and non-prod toggles, with real threads."""

import time

from apps.feature_flags import service
from apps.feature_flags.models import FlagChangeRequest
from foundation.db import SessionLocal
from tests.feature_flags.factories import flag_enabled, make_flag, make_request, request_flag
from tests.refunds.factories import audit_actions
from tests.refunds.helpers import client_as, run_concurrently


def test_two_simultaneous_approvals_exactly_one_succeeds(database):
    request_id = make_request(requested_by="dave")
    flag_id = request_flag(request_id)

    results = run_concurrently(
        lambda: client_as("gwen").post(f"/flags/requests/{request_id}/approve"),
        lambda: client_as("carol").post(f"/flags/requests/{request_id}/approve"),
    )

    assert sorted(r.status_code for r in results) == [303, 409]
    assert flag_enabled(flag_id) is True
    assert audit_actions(request_id, entity="flag_change_request") == ["approve"]
    assert audit_actions(flag_id, entity="flag") == ["apply_change"]


def test_simultaneous_approve_and_reject_exactly_one_decides(database):
    request_id = make_request(requested_by="dave")
    results = run_concurrently(
        lambda: client_as("gwen").post(f"/flags/requests/{request_id}/approve"),
        lambda: client_as("carol").post(f"/flags/requests/{request_id}/reject"),
    )
    assert sorted(r.status_code for r in results) == [303, 409]
    assert len(audit_actions(request_id, entity="flag_change_request")) == 1


def test_two_simultaneous_requests_for_one_flag_open_one(database, monkeypatch):
    flag_id = make_flag("prod", enabled=False)
    original = service.pending_request

    def slow_pending_request(session, flag_id):
        # Widen the check-then-insert window: without the row lock both would pass the check.
        found = original(session, flag_id)
        time.sleep(0.3)
        return found

    monkeypatch.setattr(service, "pending_request", slow_pending_request)

    def request(user):
        return lambda: client_as(user).post(
            f"/flags/{flag_id}/request-change", data={"enabled": "true"}
        )

    results = run_concurrently(request("dave"), request("gwen"))

    assert sorted(r.status_code for r in results) == [303, 409]
    with SessionLocal() as session:
        assert session.query(FlagChangeRequest).count() == 1
    assert audit_actions(entity="flag_change_request") == ["request"]


def test_two_simultaneous_identical_toggles_one_applies(database):
    flag_id = make_flag("dev", enabled=False)

    def toggle(user):
        return lambda: client_as(user).post(f"/flags/{flag_id}/toggle", data={"enabled": "true"})

    results = run_concurrently(toggle("dave"), toggle("carol"))

    assert sorted(r.status_code for r in results) == [303, 409]
    assert flag_enabled(flag_id) is True
    assert audit_actions(flag_id, entity="flag") == ["toggle"]
