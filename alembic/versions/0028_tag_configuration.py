"""tag configuration: tag_config_items + tag_config_snapshots

Revision ID: 0028
Revises: 0027
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # New tables only; no existing table or stored tag is changed. Default
    # values are seeded by the app on startup (services/tag_config.py
    # seed_template), idempotently, into the "__template__" scope only.
    op.create_table(
        "tag_config_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("site_key", sa.String(100), nullable=False),
        sa.Column("level", sa.String(20), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("name_key", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("folder_name", sa.String(128), nullable=False),
        sa.Column("code", sa.String(40), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("tag_config_items.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(10), nullable=False, server_default="Active"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source", sa.String(10), nullable=False, server_default="Custom"),
        sa.Column("replaced_by", sa.Integer(), nullable=True),
        sa.Column("aliases_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("attributes_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(320), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("modified_by", sa.String(320), nullable=True),
        sa.Column("modified_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("site_key", "level", "parent_id", "name_key", name="uq_tag_config_sibling_name"),
    )
    op.create_index("ix_tag_config_items_site_key", "tag_config_items", ["site_key"])
    op.create_index("ix_tag_config_items_level", "tag_config_items", ["level"])
    op.create_index("ix_tag_config_items_parent_id", "tag_config_items", ["parent_id"])
    op.create_index("ix_tag_config_items_status", "tag_config_items", ["status"])
    op.create_table(
        "tag_config_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("site_key", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("changed_by", sa.String(320), nullable=True),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("level", sa.String(20), nullable=True),
        sa.Column("summary_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
    )
    op.create_index("ix_tag_config_snapshots_site_key", "tag_config_snapshots", ["site_key"])
    op.create_index("ix_tag_config_snapshots_created_at", "tag_config_snapshots", ["created_at"])


def downgrade() -> None:
    op.drop_table("tag_config_snapshots")
    op.drop_table("tag_config_items")
