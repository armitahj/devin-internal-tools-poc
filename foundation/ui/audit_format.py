"""Readable audit entries: friendly field changes instead of raw before/after JSON."""

import json
import re

from foundation.audit import DENIED
from foundation.models import AuditLog
from foundation.ui.labels import cannot, status_label

# Display order and friendly names. Unknown keys go last, with a label derived from the key.
LABELS = {
    "status": "Status",
    "payout_status": "Payout status",
    "amount_cents": "Amount",
    "attempts": "Attempts",
    "enabled": "Enabled",
    "desired_enabled": "Desired",
    "key": "Flag",
    "environment": "Environment",
    "mode": "Provider mode",
    "transaction_id": "Transaction",
    "flag_id": "Flag ID",
    "request_id": "Request",
    "requested_by": "Requested by",
    "first_approved_by": "First approved by",
    "approvers": "Approvers",
    "reviewed_by": "Reviewed by",
    "triggered_by": "Triggered by",
    "provider_ref": "Provider reference",
    "idempotency_key": "Idempotency key",
    "error": "Error",
}
ORDER = {key: i for i, key in enumerate(LABELS)}
STATUS_KEYS = {"status", "payout_status"}
ROLE_LACKS = re.compile(r"role (\S+) lacks (\S+)")


def label(key: str) -> str:
    return LABELS.get(key) or key.removesuffix("_cents").replace("_", " ").capitalize()


def value(key: str, v: object) -> str:
    if v is None:
        return "-"
    if key in STATUS_KEYS and isinstance(v, str):
        return status_label(v)
    if isinstance(v, bool):
        return "on" if v else "off"
    if key.endswith("_cents") and isinstance(v, int):
        return f"${v / 100:,.2f}"
    if isinstance(v, list):
        return ", ".join(str(item) for item in v)
    return str(v)


def describe(entry: AuditLog) -> list[str]:
    """One line per field: "Label: before → after" when it changed, else "Label: value"."""
    before, after = entry.before or {}, entry.after or {}
    if entry.action == DENIED:
        request = " ".join(p for p in (after.get("method"), after.get("path")) if p)
        reason = after.get("reason")
        if reason and (lacks := ROLE_LACKS.fullmatch(reason)):
            role, action = lacks.groups()
            reason = cannot(f"{role.capitalize()}s", action)
        return [f"Denied: {request or entry.entity_id}" + (f" — {reason}" if reason else "")]
    lines = []
    for key in sorted({*before, *after}, key=lambda k: (ORDER.get(k, len(ORDER)), k)):
        if key in before and key in after and before[key] != after[key]:
            lines.append(f"{label(key)}: {value(key, before[key])} → {value(key, after[key])}")
        else:
            v = after[key] if key in after else before[key]
            if v is not None:
                lines.append(f"{label(key)}: {value(key, v)}")
    return lines


def raw_json(entry: AuditLog) -> str:
    return json.dumps({"before": entry.before, "after": entry.after}, indent=2, sort_keys=True)
