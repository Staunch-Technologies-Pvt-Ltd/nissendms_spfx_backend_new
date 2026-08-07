"""initial schema: vessels, folders, upload_jobs

Revision ID: 0001
Revises:
Create Date: 2026-06-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "vessels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("imo", sa.String(length=7), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("name"),
        sa.UniqueConstraint("imo"),
    )
    op.create_index("ix_vessels_name", "vessels", ["name"])

    op.create_table(
        "folders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("name", sa.String(length=400), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("drive_item_id", sa.String(length=256), nullable=False),
        sa.Column("month_driven", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "vessel_id",
            sa.Integer(),
            sa.ForeignKey("vessels.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("path"),
    )
    op.create_index("ix_folders_path", "folders", ["path"])
    op.create_index("ix_folders_drive_item_id", "folders", ["drive_item_id"])

    op.create_table(
        "upload_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("filename", sa.String(length=400), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="processing"),
        sa.Column("destination", sa.String(length=1024), nullable=True),
        sa.Column("detected_month", sa.String(length=40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("upload_jobs")
    op.drop_index("ix_folders_drive_item_id", table_name="folders")
    op.drop_index("ix_folders_path", table_name="folders")
    op.drop_table("folders")
    op.drop_index("ix_vessels_name", table_name="vessels")
    op.drop_table("vessels")
