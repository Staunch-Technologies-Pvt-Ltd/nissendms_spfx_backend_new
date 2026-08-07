"""generalize approval_requests to cover admin activity + non-upload actions

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Upload-only columns become optional so non-upload rows (delete,
    # create/update vessel, create folder) can omit them. Existing rows keep
    # their values; the pending-dest-filename unique index is unaffected
    # since NULL is never treated as equal to NULL.
    op.alter_column("approval_requests", "filename", existing_type=sa.String(length=400), nullable=True)
    op.alter_column(
        "approval_requests", "content_type", existing_type=sa.String(length=200), nullable=True
    )
    op.alter_column("approval_requests", "size", existing_type=sa.Integer(), nullable=True)
    op.alter_column(
        "approval_requests",
        "destination_folder_id",
        existing_type=sa.String(length=256),
        nullable=True,
    )
    op.alter_column(
        "approval_requests",
        "destination_path",
        existing_type=sa.String(length=1024),
        nullable=True,
    )
    op.alter_column(
        "approval_requests", "drive_item_id", existing_type=sa.String(length=256), nullable=True
    )

    op.add_column(
        "approval_requests",
        sa.Column("entry_kind", sa.String(length=20), nullable=False, server_default="approval"),
    )
    op.add_column(
        "approval_requests",
        sa.Column("action_type", sa.String(length=40), nullable=False, server_default="upload"),
    )
    op.add_column("approval_requests", sa.Column("department", sa.String(length=100), nullable=True))
    op.add_column(
        "approval_requests",
        sa.Column(
            "vessel_id",
            sa.Integer(),
            sa.ForeignKey("vessels.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("approval_requests", sa.Column("vessel_name", sa.String(length=200), nullable=True))
    op.add_column("approval_requests", sa.Column("target_id", sa.String(length=256), nullable=True))
    op.add_column(
        "approval_requests", sa.Column("target_description", sa.String(length=500), nullable=True)
    )
    op.add_column("approval_requests", sa.Column("payload_json", sa.Text(), nullable=True))
    op.add_column("approval_requests", sa.Column("changes_json", sa.Text(), nullable=True))
    op.add_column("approval_requests", sa.Column("message", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("approval_requests", "message")
    op.drop_column("approval_requests", "changes_json")
    op.drop_column("approval_requests", "payload_json")
    op.drop_column("approval_requests", "target_description")
    op.drop_column("approval_requests", "target_id")
    op.drop_column("approval_requests", "vessel_name")
    op.drop_column("approval_requests", "vessel_id")
    op.drop_column("approval_requests", "department")
    op.drop_column("approval_requests", "action_type")
    op.drop_column("approval_requests", "entry_kind")

    op.alter_column(
        "approval_requests", "drive_item_id", existing_type=sa.String(length=256), nullable=False
    )
    op.alter_column(
        "approval_requests",
        "destination_path",
        existing_type=sa.String(length=1024),
        nullable=False,
    )
    op.alter_column(
        "approval_requests",
        "destination_folder_id",
        existing_type=sa.String(length=256),
        nullable=False,
    )
    op.alter_column("approval_requests", "size", existing_type=sa.Integer(), nullable=False)
    op.alter_column(
        "approval_requests", "content_type", existing_type=sa.String(length=200), nullable=False
    )
    op.alter_column("approval_requests", "filename", existing_type=sa.String(length=400), nullable=False)
