from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from apps.refunds.rules import PAYOUT_STATUSES, REFUND_STATUSES, status_check_sql
from foundation.db import Base


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (CheckConstraint("amount_cents > 0", name="transactions_amount_positive"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    customer_name: Mapped[str] = mapped_column(String(200))
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RefundRequest(Base):
    __tablename__ = "refund_requests"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="refund_requests_amount_positive"),
        CheckConstraint(
            status_check_sql("status", REFUND_STATUSES), name="refund_requests_status_check"
        ),
        # R8 backstop: the requester is never an approver, and two approvals are distinct.
        CheckConstraint(
            "first_approved_by <> requested_by", name="refund_requests_first_approver_not_requester"
        ),
        CheckConstraint(
            "status NOT IN ('approved', 'paid', 'payment_failed', 'payment_unknown') "
            "OR first_approved_by <> reviewed_by",
            name="refund_requests_distinct_approvers",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), index=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), index=True)
    requested_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    reviewed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    # R8: set by the first of two approvals (refunds over $1,000); the second is reviewed_by.
    first_approved_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Payout(Base):
    __tablename__ = "payouts"
    __table_args__ = (
        CheckConstraint(status_check_sql("status", PAYOUT_STATUSES), name="payouts_status_check"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    refund_id: Mapped[int] = mapped_column(ForeignKey("refund_requests.id"), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(20))
    provider_ref: Mapped[str | None] = mapped_column(String(100))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
