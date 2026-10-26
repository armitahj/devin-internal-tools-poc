from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from apps.feature_flags.rules import ENVIRONMENTS, REQUEST_STATUSES, check_in_sql
from foundation.db import Base


class Flag(Base):
    __tablename__ = "flags"
    __table_args__ = (
        UniqueConstraint("key", "environment", name="flags_key_environment_key"),
        CheckConstraint(check_in_sql("environment", ENVIRONMENTS), name="flags_environment_check"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    key: Mapped[str] = mapped_column(String(100))
    environment: Mapped[str] = mapped_column(String(20))
    enabled: Mapped[bool] = mapped_column(Boolean)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class FlagChangeRequest(Base):
    __tablename__ = "flag_change_requests"
    __table_args__ = (
        CheckConstraint(
            check_in_sql("status", REQUEST_STATUSES), name="flag_change_requests_status_check"
        ),
        # Backstop for the locked check in service.request_change: one open request per flag.
        Index(
            "flag_change_requests_one_pending_per_flag",
            "flag_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    flag_id: Mapped[int] = mapped_column(ForeignKey("flags.id"), index=True)
    desired_enabled: Mapped[bool] = mapped_column(Boolean)
    requested_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    reviewed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
