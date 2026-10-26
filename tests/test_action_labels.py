"""Plain-English permission wording, built from policy.yaml."""

from foundation.policy import load_policy
from foundation.ui.labels import ACTION_LABELS, action_label, allowed_roles, cannot
from tests.refunds.factories import make_refund
from tests.refunds.helpers import client_as


def test_every_policy_action_has_a_label():
    assert set(load_policy()) - set(ACTION_LABELS) == set()


def test_allowed_roles_come_from_policy():
    assert allowed_roles("refunds.approve") == "reviewers and admins"
    assert allowed_roles("refunds.set_provider_mode") == "admins"
    assert allowed_roles("audit.view") == "reviewers, engineers and admins"
    assert allowed_roles("no.such_action") == ""


def test_cannot_sentence():
    assert cannot("Alice (agent)", "refunds.approve") == (
        "Alice (agent) can't approve refunds. Only reviewers and admins can."
    )
    assert cannot("Bob (reviewer)", "refunds.approve_high_value") == (
        "Bob (reviewer) can't approve refunds over $500. Only admins can."
    )
    assert cannot("Erin (agent)", "no.such_action") == (
        "Erin (agent) can't no.such_action. No role can."
    )
    assert action_label("flags.request_prod_change") == "request production flag changes"


def test_denied_attempt_reads_the_same_in_the_audit_log(database):
    refund_id = make_refund()
    assert client_as("alice").post(f"/refunds/{refund_id}/approve").status_code == 403

    audit = client_as("carol").get("/audit").text
    assert (
        f"Denied: POST /refunds/{refund_id}/approve — Agents can&#39;t approve refunds. "
        "Only reviewers and admins can." in audit
    )
    assert "role agent lacks refunds.approve" in audit.split("Raw JSON")[1]
