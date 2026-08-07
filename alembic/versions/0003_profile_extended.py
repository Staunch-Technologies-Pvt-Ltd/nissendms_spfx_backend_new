"""extend user_profiles and add emergency_contacts, folder_permissions, activity_logs

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── extend user_profiles ───────────────────────────────────────────────────
    op.add_column("user_profiles", sa.Column("first_name", sa.String(100), nullable=True))
    op.add_column("user_profiles", sa.Column("last_name", sa.String(100), nullable=True))
    op.add_column("user_profiles", sa.Column("department", sa.String(200), nullable=True))
    op.add_column("user_profiles", sa.Column("manager_name", sa.String(200), nullable=True))
    op.add_column("user_profiles", sa.Column("manager_email", sa.String(320), nullable=True))
    op.add_column(
        "user_profiles",
        sa.Column("two_factor_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("user_profiles", sa.Column("password_changed_at", sa.DateTime(), nullable=True))

    # ── emergency_contacts ─────────────────────────────────────────────────────
    op.create_table(
        "emergency_contacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_email",
            sa.String(320),
            sa.ForeignKey("user_profiles.email", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("relationship_type", sa.String(100), nullable=True),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_email"),
    )
    op.create_index("ix_emergency_contacts_user_email", "emergency_contacts", ["user_email"])

    # ── folder_permissions ─────────────────────────────────────────────────────
    op.create_table(
        "folder_permissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_email",
            sa.String(320),
            sa.ForeignKey("user_profiles.email", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("folder_name", sa.String(400), nullable=False),
        sa.Column("permission_level", sa.String(20), nullable=False),
    )
    op.create_index("ix_folder_permissions_user_email", "folder_permissions", ["user_email"])

    # ── activity_logs ──────────────────────────────────────────────────────────
    op.create_table(
        "activity_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_email",
            sa.String(320),
            sa.ForeignKey("user_profiles.email", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_activity_logs_user_email", "activity_logs", ["user_email"])


def downgrade() -> None:
    op.drop_index("ix_activity_logs_user_email", table_name="activity_logs")
    op.drop_table("activity_logs")
    op.drop_index("ix_folder_permissions_user_email", table_name="folder_permissions")
    op.drop_table("folder_permissions")
    op.drop_index("ix_emergency_contacts_user_email", table_name="emergency_contacts")
    op.drop_table("emergency_contacts")
    op.drop_column("user_profiles", "password_changed_at")
    op.drop_column("user_profiles", "two_factor_enabled")
    op.drop_column("user_profiles", "manager_email")
    op.drop_column("user_profiles", "manager_name")
    op.drop_column("user_profiles", "department")
    op.drop_column("user_profiles", "last_name")
    op.drop_column("user_profiles", "first_name")
