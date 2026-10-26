"""refunds: two approvers over $1,000 (partially_approved, first_approved_by)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-26 07:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATUSES = "'submitted', 'approved', 'rejected', 'paid', 'payment_failed', 'payment_unknown'"
NEW_STATUSES = (
    "'submitted', 'partially_approved', 'approved', 'rejected', 'paid', "
    "'payment_failed', 'payment_unknown'"
)


def upgrade() -> None:
    op.add_column(
        "refund_requests", sa.Column("first_approved_by", sa.String(length=50), nullable=True)
    )
    op.create_foreign_key(
        "refund_requests_first_approved_by_fkey",
        "refund_requests",
        "users",
        ["first_approved_by"],
        ["id"],
    )
    op.drop_constraint("refund_requests_status_check", "refund_requests", type_="check")
    op.create_check_constraint(
        "refund_requests_status_check", "refund_requests", f"status IN ({NEW_STATUSES})"
    )
    op.create_check_constraint(
        "refund_requests_first_approver_not_requester",
        "refund_requests",
        "first_approved_by <> requested_by",
    )
    op.create_check_constraint(
        "refund_requests_distinct_approvers",
        "refund_requests",
        "status NOT IN ('approved', 'paid', 'payment_failed', 'payment_unknown') "
        "OR first_approved_by <> reviewed_by",
    )


def downgrade() -> None:
    op.drop_constraint("refund_requests_distinct_approvers", "refund_requests", type_="check")
    op.drop_constraint(
        "refund_requests_first_approver_not_requester", "refund_requests", type_="check"
    )
    op.drop_constraint("refund_requests_status_check", "refund_requests", type_="check")
    op.create_check_constraint(
        "refund_requests_status_check", "refund_requests", f"status IN ({OLD_STATUSES})"
    )
    op.drop_constraint(
        "refund_requests_first_approved_by_fkey", "refund_requests", type_="foreignkey"
    )
    op.drop_column("refund_requests", "first_approved_by")
