"""Committed refunds test data (the app reads it through its own sessions)."""

from sqlalchemy import func, select

from apps.refunds import rules
from apps.refunds.models import Payout, RefundRequest, Transaction
from foundation.db import SessionLocal
from foundation.models import AuditLog, MockProviderLedger


def make_transaction(amount_cents: int = 10_000, customer: str = "Acme") -> int:
    with SessionLocal() as session, session.begin():
        tx = Transaction(customer_name=customer, amount_cents=amount_cents)
        session.add(tx)
        session.flush()
        return tx.id


def make_refund(
    amount_cents: int = 5_000,
    status: str = "submitted",
    requested_by: str = "alice",
    transaction_id: int | None = None,
    payout_status: str | None = None,
) -> int:
    """A refund in any status; resumable statuses also get their payout row."""
    if transaction_id is None:
        transaction_id = make_transaction(max(amount_cents, 10_000))
    with SessionLocal() as session, session.begin():
        refund = RefundRequest(
            transaction_id=transaction_id,
            amount_cents=amount_cents,
            reason="test",
            status=status,
            requested_by=requested_by,
            reviewed_by=None if status == "submitted" else "carol",
        )
        session.add(refund)
        session.flush()
        if payout_status is None and status in rules.RESUMABLE_STATUSES + ("paid",):
            payout_status = {
                "approved": "pending",
                "payment_failed": "failed",
                "payment_unknown": "unknown",
                "paid": "succeeded",
            }[status]
        if payout_status:
            session.add(
                Payout(
                    refund_id=refund.id,
                    idempotency_key=rules.idempotency_key(refund.id),
                    status=payout_status,
                    attempts=0,
                )
            )
        return refund.id


def refund_state(refund_id: int) -> tuple[str, Payout | None]:
    with SessionLocal() as session:
        refund = session.get(RefundRequest, refund_id)
        payout = session.scalar(select(Payout).where(Payout.refund_id == refund_id))
        return refund.status, payout


def ledger_rows(refund_id: int) -> int:
    with SessionLocal() as session:
        return session.scalar(
            select(func.count())
            .select_from(MockProviderLedger)
            .where(MockProviderLedger.idempotency_key == rules.idempotency_key(refund_id))
        )


def audit_actions(entity_id: object | None = None, entity: str = "refund") -> list[str]:
    with SessionLocal() as session:
        query = select(AuditLog.action).where(AuditLog.entity == entity).order_by(AuditLog.id)
        if entity_id is not None:
            query = query.where(AuditLog.entity_id == str(entity_id))
        return list(session.scalars(query))
