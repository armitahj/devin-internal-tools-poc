"""Refund rules (SPEC "Refunds domain"). Every status column's allowed values live here."""

REFUND_STATUSES = (
    "submitted",
    "partially_approved",
    "approved",
    "rejected",
    "paid",
    "payment_failed",
    "payment_unknown",
)
PAYOUT_STATUSES = ("pending", "succeeded", "failed", "unknown")

# R4: amounts strictly above $500 need an admin.
HIGH_VALUE_THRESHOLD_CENTS = 50_000

# R8: amounts strictly above $1,000 need two distinct approvers, neither the requester.
TWO_APPROVER_THRESHOLD_CENTS = 100_000

# Statuses awaiting a review decision (approve or reject).
REVIEWABLE_STATUSES = ("submitted", "partially_approved")

# R7: refunds whose payout can be (re-)run.
RESUMABLE_STATUSES = ("approved", "payment_failed", "payment_unknown")

# R6: refund status after a provider call -> states it may be reached from.
PAYOUT_OUTCOME_FROM = {
    "paid": ("approved", "payment_failed", "payment_unknown"),
    "payment_failed": ("approved", "payment_unknown"),
    "payment_unknown": ("approved", "payment_failed"),
}
PAYOUT_STATUS_FOR_OUTCOME = {
    "paid": "succeeded",
    "payment_failed": "failed",
    "payment_unknown": "unknown",
}
UNRESOLVED_PAYOUT_STATUSES = ("pending", "failed", "unknown")

SYSTEM_ACTOR = "system"


def idempotency_key(refund_id: int) -> str:
    """R6: fixed per refund and reused on every attempt."""
    return f"refund-{refund_id}"


def requires_admin(amount_cents: int) -> bool:
    """R4."""
    return amount_cents > HIGH_VALUE_THRESHOLD_CENTS


def requires_two_approvers(amount_cents: int) -> bool:
    """R8."""
    return amount_cents > TWO_APPROVER_THRESHOLD_CENTS


def approval_step(amount_cents: int, status: str) -> tuple[str, str]:
    """R8: (from_status, to_status) for the next approval of a refund in `status`.

    `status` must be in REVIEWABLE_STATUSES. Refunds of $1,000 or less go straight to approved;
    larger ones go submitted -> partially_approved -> approved.
    """
    if status == "partially_approved" or not requires_two_approvers(amount_cents):
        return status, "approved"
    return status, "partially_approved"


def admin_approval_satisfied(
    amount_cents: int, approver_is_admin: bool, first_approver_is_admin: bool | None
) -> bool:
    """R4 with R8: a refund over $500 cannot reach approved without an admin among its
    approvers. A first (partial) approval of a two-approver refund may come from any reviewer;
    the second must then be an admin unless the first one was."""
    if not requires_admin(amount_cents) or approver_is_admin:
        return True
    if first_approver_is_admin is None:  # this approval would be the first of two
        return requires_two_approvers(amount_cents)
    return first_approver_is_admin


def dollars(cents: int) -> str:
    """Money for people: 7200 -> "$72.00"."""
    return f"${cents / 100:,.2f}"


def exceeds_balance(original_cents: int, already_refunded_cents: int, requested_cents: int) -> bool:
    """R3: non-rejected refunds against a transaction may not exceed its original amount."""
    return already_refunded_cents + requested_cents > original_cents


def status_check_sql(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"
