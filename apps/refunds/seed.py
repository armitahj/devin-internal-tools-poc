"""Seed ~15 transactions and ~30 refunds across every status (skipped if data exists)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.refunds import rules
from apps.refunds.models import Payout, RefundRequest, Transaction
from foundation import audit
from foundation.models import MockProviderLedger

CUSTOMERS = [
    ("Acme Corp", 12_000),
    ("Globex", 25_000),
    ("Initech", 48_000),
    ("Umbrella", 150_000),
    ("Hooli", 9_500),
    ("Stark Industries", 220_000),
    ("Wayne Enterprises", 64_000),
    ("Wonka Industries", 18_000),
    ("Soylent", 31_000),
    ("Tyrell Corp", 175_000),
    ("Cyberdyne", 7_800),
    ("Vandelay Imports", 42_000),
    ("Pied Piper", 56_000),
    ("Dunder Mifflin", 15_500),
    ("Oceanic Airlines", 99_000),
]

# (status, minutes since last update). 30 refunds, two per transaction.
REFUNDS = [
    ("submitted", 5),
    ("submitted", 30),
    ("submitted", 90),
    ("submitted", 200),
    ("submitted", 600),
    ("submitted", 1500),
    ("submitted", 3000),
    ("submitted", 45),
    ("approved", 0),
    ("approved", 15),
    ("approved", 240),
    ("rejected", 300),
    ("rejected", 2000),
    ("rejected", 4000),
    ("rejected", 7000),
    ("rejected", 120),
    ("paid", 10),
    ("paid", 100),
    ("paid", 400),
    ("paid", 1200),
    ("paid", 2500),
    ("paid", 5000),
    ("paid", 8000),
    ("paid", 12000),
    ("paid", 20000),
    ("payment_failed", 20),
    ("payment_failed", 900),
    ("payment_unknown", 8),
    ("payment_unknown", 360),
    ("payment_unknown", 3500),
]
REASONS = ["Duplicate charge", "Service outage credit", "Damaged item", "Cancelled order"]
AGENTS = ["alice", "erin"]
REVIEWERS = ["bob", "frank", "carol"]


def seed(session: Session) -> bool:
    if session.scalar(select(func.count()).select_from(Transaction)):
        return False
    now = datetime.now(UTC)
    txs = [
        Transaction(customer_name=name, amount_cents=amount, created_at=now - timedelta(days=30))
        for name, amount in CUSTOMERS
    ]
    session.add_all(txs)
    session.flush()

    for i, (status, minutes) in enumerate(REFUNDS):
        tx = txs[i % len(txs)]
        amount = tx.amount_cents * 2 // 5  # two refunds per tx, each 40%: never over balance
        reviewer = None
        if status != "submitted":
            reviewer = "carol" if rules.requires_admin(amount) else REVIEWERS[i % 3]
        updated = now - timedelta(minutes=minutes)
        refund = RefundRequest(
            transaction_id=tx.id,
            amount_cents=amount,
            reason=REASONS[i % len(REASONS)],
            status=status,
            requested_by=AGENTS[i % 2],
            reviewed_by=reviewer,
            created_at=updated - timedelta(hours=1),
            updated_at=updated,
        )
        session.add(refund)
        session.flush()
        audit.record(
            session,
            actor_id="seed",
            entity="refund",
            entity_id=refund.id,
            action="seed",
            after={"status": status},
        )
        if status in ("submitted", "rejected"):
            continue
        key = rules.idempotency_key(refund.id)
        provider_recorded = status in ("paid", "payment_unknown")
        provider_ref = f"mock_seed_{refund.id:04d}" if provider_recorded else None
        if provider_recorded:
            session.add(
                MockProviderLedger(
                    idempotency_key=key, amount_cents=amount, provider_ref=provider_ref
                )
            )
        session.add(
            Payout(
                refund_id=refund.id,
                idempotency_key=key,
                status={
                    "approved": "pending",
                    "paid": "succeeded",
                    "payment_failed": "failed",
                    "payment_unknown": "unknown",
                }[status],
                provider_ref=provider_ref if status == "paid" else None,
                attempts=0 if status == "approved" else 1,
                last_error={
                    "payment_failed": "Provider declined refund",
                    "payment_unknown": "No response from provider",
                }.get(status),
                updated_at=updated,
            )
        )
    return True
