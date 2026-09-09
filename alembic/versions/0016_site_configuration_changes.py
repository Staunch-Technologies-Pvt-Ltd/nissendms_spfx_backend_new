"""add site_configuration_changes table for admin site switching audit log

Revision ID: 0016
Revises: 0015
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = set(inspector.get_table_names())
    
    if "site_configuration_changes" not in tables:
        op.create_table(
            "site_configuration_changes",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("changed_by_email", sa.String(320), nullable=False),
            sa.Column("changed_by_name", sa.String(200), nullable=True),
            sa.Column("previous_site", sa.String(100), nullable=False),
            sa.Column("new_site", sa.String(100), nullable=False),
            sa.Column("previous_db_name", sa.String(256), nullable=True),
            sa.Column("previous_drive_id", sa.String(256), nullable=True),
            sa.Column("previous_site_name", sa.String(256), nullable=True),
            sa.Column("new_db_name", sa.String(256), nullable=True),
            sa.Column("new_drive_id", sa.String(256), nullable=True),
            sa.Column("new_site_name", sa.String(256), nullable=True),
            sa.Column("status", sa.String(50), nullable=False, server_default="success"),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_site_configuration_changes_changed_by_email", "site_configuration_changes", ["changed_by_email"])
        op.create_index("ix_site_configuration_changes_previous_site", "site_configuration_changes", ["previous_site"])
        op.create_index("ix_site_configuration_changes_new_site", "site_configuration_changes", ["new_site"])
        op.create_index("ix_site_configuration_changes_created_at", "site_configuration_changes", ["created_at"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = set(inspector.get_table_names())
    
    if "site_configuration_changes" in tables:
        op.drop_index("ix_site_configuration_changes_created_at")
        op.drop_index("ix_site_configuration_changes_new_site")
        op.drop_index("ix_site_configuration_changes_previous_site")
        op.drop_index("ix_site_configuration_changes_changed_by_email")
        op.drop_table("site_configuration_changes")
