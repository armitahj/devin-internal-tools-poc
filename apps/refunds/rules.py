"""Refund rules (SPEC "Refunds domain"). Every status column's allowed values live here."""

REFUND_STATUSES = ("submitted", "approved", "rejected", "paid", "payment_failed", "payment_unknown")
PAYOUT_STATUSES = ("pending", "succeeded", "failed", "unknown")

# R4: amounts strictly above $500 need an admin.
HIGH_VALUE_THRESHOLD_CENTS = 50_000

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


def exceeds_balance(original_cents: int, already_refunded_cents: int, requested_cents: int) -> bool:
    """R3: non-rejected refunds against a transaction may not exceed its original amount."""
    return already_refunded_cents + requested_cents > original_cents


def status_check_sql(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"
