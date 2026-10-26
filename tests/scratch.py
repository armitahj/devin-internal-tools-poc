"""Test-only table for exercising workflow/audit without depending on an app."""

from sqlalchemy import Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ScratchBase(DeclarativeBase):
    pass


class Ticket(ScratchBase):
    __tablename__ = "scratch_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(30))
    requested_by: Mapped[str] = mapped_column(String(50))
    reviewed_by: Mapped[str | None] = mapped_column(String(50))
