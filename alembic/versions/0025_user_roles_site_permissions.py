"""add database-backed roles and site permissions

Revision ID: 0025
Revises: 0024
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("user_profiles", sa.Column("role", sa.String(20), nullable=False, server_default="User"))
    op.add_column("user_profiles", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("user_profiles", sa.Column("permissions_updated_at", sa.DateTime(), nullable=True))
    op.create_table(
        "user_site_permissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_email", sa.String(320), sa.ForeignKey("user_profiles.email", ondelete="CASCADE"), nullable=False),
        sa.Column("site_key", sa.String(100), nullable=False),
        sa.Column("can_view", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("can_upload", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("can_tag_on_upload", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("granted_by_email", sa.String(320), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_email", "site_key", name="uq_user_site_permission"),
    )
    op.create_index("ix_user_site_permissions_user_email", "user_site_permissions", ["user_email"])
    op.create_index("ix_user_site_permissions_site_key", "user_site_permissions", ["site_key"])


def downgrade() -> None:
    op.drop_index("ix_user_site_permissions_site_key", table_name="user_site_permissions")
    op.drop_index("ix_user_site_permissions_user_email", table_name="user_site_permissions")
    op.drop_table("user_site_permissions")
    op.drop_column("user_profiles", "permissions_updated_at")
    op.drop_column("user_profiles", "is_active")
    op.drop_column("user_profiles", "role")