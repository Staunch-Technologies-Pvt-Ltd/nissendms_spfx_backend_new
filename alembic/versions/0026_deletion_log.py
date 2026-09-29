"""add deletion_log table (deletion attribution + live notifications)

Revision ID: 0026
Revises: 0025
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "deletion_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("drive_item_id", sa.String(256), nullable=True),
        sa.Column("item_name", sa.String(400), nullable=False),
        sa.Column("item_type", sa.String(20), nullable=False),
        sa.Column("classification", sa.String(20), nullable=False),
        sa.Column("original_path", sa.String(1024), nullable=True),
        sa.Column("vessel_name", sa.String(200), nullable=True),
        sa.Column("category", sa.String(200), nullable=True),
        sa.Column("sub_category", sa.String(200), nullable=True),
        sa.Column("site_name", sa.String(256), nullable=True),
        sa.Column("site_key", sa.String(100), nullable=True),
        sa.Column("deleted_by_email", sa.String(320), nullable=True),
        sa.Column("deleted_by_name", sa.String(200), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("source", sa.String(20), nullable=False, server_default="app"),
        sa.Column("deleted_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_deletion_log_drive_item_id", "deletion_log", ["drive_item_id"])
    op.create_index("ix_deletion_log_item_type", "deletion_log", ["item_type"])
    op.create_index("ix_deletion_log_classification", "deletion_log", ["classification"])
    op.create_index("ix_deletion_log_vessel_name", "deletion_log", ["vessel_name"])
    op.create_index("ix_deletion_log_site_key", "deletion_log", ["site_key"])
    op.create_index("ix_deletion_log_deleted_by_email", "deletion_log", ["deleted_by_email"])
    op.create_index("ix_deletion_log_deleted_at", "deletion_log", ["deleted_at"])
    op.create_index("ix_deletion_log_read", "deletion_log", ["read"])
    # Dedup key used by _record_deletion's upsert-by-item logic. Native-SPO
    # backfill rows may have drive_item_id NULL (unresolvable), so this is a
    # plain index, not a unique constraint (Postgres allows unlimited NULLs
    # to coexist under a unique index anyway, but being explicit keeps the
    # reconciliation job's "insert if no match" logic simple and honest).
    op.create_index(
        "ix_deletion_log_item_site", "deletion_log", ["drive_item_id", "site_key"]
    )


def downgrade() -> None:
    op.drop_index("ix_deletion_log_item_site", table_name="deletion_log")
    op.drop_index("ix_deletion_log_read", table_name="deletion_log")
    op.drop_index("ix_deletion_log_deleted_at", table_name="deletion_log")
    op.drop_index("ix_deletion_log_deleted_by_email", table_name="deletion_log")
    op.drop_index("ix_deletion_log_site_key", table_name="deletion_log")
    op.drop_index("ix_deletion_log_vessel_name", table_name="deletion_log")
    op.drop_index("ix_deletion_log_classification", table_name="deletion_log")
    op.drop_index("ix_deletion_log_item_type", table_name="deletion_log")
    op.drop_index("ix_deletion_log_drive_item_id", table_name="deletion_log")
    op.drop_table("deletion_log")
