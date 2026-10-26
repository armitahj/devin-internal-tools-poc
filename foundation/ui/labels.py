"""Display labels for internal status values. Only the UI uses these; stored values never change."""

STATUS_LABELS = {
    # refunds
    "submitted": "Submitted",
    "partially_approved": "Awaiting second approval",
    "approved": "Approved",
    "rejected": "Rejected",
    "paid": "Paid",
    "payment_failed": "Payment failed",
    "payment_unknown": "Payment outcome unknown",
    # payouts and flag change requests ("approved"/"rejected" are shared with refunds)
    "pending": "Pending",
    "succeeded": "Succeeded",
    "failed": "Failed",
    "unknown": "Unknown",
}


def status_label(value: str | None) -> str:
    if value is None:
        return ""
    return STATUS_LABELS.get(value, value)


ACTION_LABELS = {
    "refunds.view": "view refunds",
    "refunds.submit": "submit refunds",
    "refunds.approve": "approve refunds",
    "refunds.reject": "reject refunds",
    "refunds.resume_payout": "resume payouts",
    "refunds.approve_high_value": "approve refunds over $500",
    "refunds.set_provider_mode": "change the payment provider mode",
    "flags.view": "view feature flags",
    "flags.toggle_nonprod": "change dev or staging flags",
    "flags.request_prod_change": "request production flag changes",
    "flags.approve_prod_change": "approve production flag changes",
    "audit.view": "view the audit log",
}
ROLE_ORDER = ("agent", "reviewer", "engineer", "admin")


def action_label(action: str) -> str:
    return ACTION_LABELS.get(action, action)


def allowed_roles(action: str) -> str:
    """Roles policy.yaml allows for `action`, in plain English: "reviewers and admins"."""
    from foundation.policy import load_policy

    roles = load_policy().get(action, frozenset())
    ordered = [r for r in ROLE_ORDER if r in roles] + sorted(roles - set(ROLE_ORDER))
    plurals = [f"{role}s" for role in ordered]
    return " and ".join(filter(None, [", ".join(plurals[:-1]), *plurals[-1:]]))


def cannot(who: str, action: str) -> str:
    """'<who> can't approve refunds. Only reviewers and admins can.'"""
    roles = allowed_roles(action)
    only = f"Only {roles} can." if roles else "No role can."
    return f"{who} can't {action_label(action)}. {only}"
