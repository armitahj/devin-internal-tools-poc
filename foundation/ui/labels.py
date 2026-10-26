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
