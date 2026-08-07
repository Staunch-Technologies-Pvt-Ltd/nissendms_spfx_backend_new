"""approval workflow: approval_requests table

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("filename", sa.String(length=400), nullable=False),
        sa.Column(
            "content_type",
            sa.String(length=200),
            nullable=False,
            server_default="application/octet-stream",
        ),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uploaded_by_email", sa.String(length=320), nullable=False),
        sa.Column("uploaded_by_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column(
            "uploaded_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("destination_folder_id", sa.String(length=256), nullable=False),
        sa.Column("destination_path", sa.String(length=1024), nullable=False),
        sa.Column("is_month_upload", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("category", sa.String(length=200), nullable=True),
        sa.Column("detected_month", sa.String(length=40), nullable=True),
        sa.Column("drive_item_id", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("decided_by_email", sa.String(length=320), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("final_path", sa.String(length=1024), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_approval_requests_status", "approval_requests", ["status"])
    op.create_index(
        "ix_approval_requests_uploaded_by_email", "approval_requests", ["uploaded_by_email"]
    )
    # Prevent two concurrently-pending requests from targeting the same file
    # name in the same destination folder (duplicate-upload guard).
    op.create_index(
        "uq_approval_requests_pending_dest_filename",
        "approval_requests",
        ["destination_folder_id", "filename"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_approval_requests_pending_dest_filename", table_name="approval_requests"
    )
    op.drop_index("ix_approval_requests_uploaded_by_email", table_name="approval_requests")
    op.drop_index("ix_approval_requests_status", table_name="approval_requests")
    op.drop_table("approval_requests")
