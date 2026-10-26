"""foundation: users, audit_log (append-only), mock_provider_ledger

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-26

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(50), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.CheckConstraint(
            "role IN ('agent', 'reviewer', 'admin', 'engineer')", name="users_role_check"
        ),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("actor_id", sa.String(50)),
        sa.Column("entity", sa.String(100), nullable=False),
        sa.Column("entity_id", sa.String(100), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("before", postgresql.JSONB),
        sa.Column("after", postgresql.JSONB),
    )
    op.create_index("ix_audit_log_entity", "audit_log", ["entity", "entity_id"])
    op.execute(
        """
        CREATE FUNCTION audit_log_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only';
        END;
        $$;
        CREATE TRIGGER audit_log_append_only
            BEFORE UPDATE OR DELETE ON audit_log
            FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();
        """
    )
    op.create_table(
        "mock_provider_ledger",
        sa.Column("idempotency_key", sa.String(200), primary_key=True),
        sa.Column("amount_cents", sa.BigInteger, nullable=False),
        sa.Column("provider_ref", sa.String(100), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("mock_provider_ledger")
    op.execute("DROP TRIGGER audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION audit_log_append_only()")
    op.drop_index("ix_audit_log_entity", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("users")
