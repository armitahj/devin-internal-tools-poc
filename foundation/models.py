from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Identity, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from foundation.db import Base

ROLES = ("agent", "reviewer", "admin", "engineer")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(f"role IN ({', '.join(repr(r) for r in ROLES)})", name="users_role_check"),
    )

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20))


class AuditLog(Base):
    """Append-only. A database trigger rejects UPDATE and DELETE."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_entity", "entity", "entity_id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    actor_id: Mapped[str | None] = mapped_column(String(50))
    entity: Mapped[str] = mapped_column(String(100))
    entity_id: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class MockProviderLedger(Base):
    """Stands in for the payment provider's own records; written outside app transactions."""

    __tablename__ = "mock_provider_ledger"

    idempotency_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    provider_ref: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
