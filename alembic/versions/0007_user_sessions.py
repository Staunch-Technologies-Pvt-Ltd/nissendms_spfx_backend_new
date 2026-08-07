"""enterprise session tracking: user_sessions + session_audit_log tables

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-19
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── user_sessions ──────────────────────────────────────────────────────────
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("login_time", sa.DateTime(), nullable=False),
        sa.Column("last_activity", sa.DateTime(), nullable=False),
        sa.Column("expiry_time", sa.DateTime(), nullable=False),
        sa.Column("logout_time", sa.DateTime(), nullable=True),
        sa.Column("last_revalidated_at", sa.DateTime(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("browser", sa.String(100), nullable=True),
        sa.Column("operating_system", sa.String(100), nullable=True),
        sa.Column("device_type", sa.String(20), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),  # supports IPv6
        sa.Column(
            "authentication_method",
            sa.String(50),
            nullable=False,
            server_default="AzureAD",
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="Active",
        ),  # Active | Expired | Logged Out | Revoked
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("uq_user_sessions_session_id", "user_sessions", ["session_id"], unique=True)
    op.create_index("ix_user_sessions_email_status", "user_sessions", ["email", "status"])
    op.create_index("ix_user_sessions_status", "user_sessions", ["status"])

    # ── session_audit_log ──────────────────────────────────────────────────────
    op.create_table(
        "session_audit_log",
        sa.Column("id", sa.String(36), primary_key=True),
        # session_id is kept for reference but NOT a FK so rows survive session deletion
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("event", sa.String(50), nullable=False),
        # session_created | session_logged_out | session_expired | session_revoked
        # | invalid_session_attempt | auth_failure
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("browser", sa.String(100), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_session_audit_email", "session_audit_log", ["email"])
    op.create_index("ix_session_audit_event", "session_audit_log", ["event"])
    op.create_index(
        "ix_session_audit_created_at", "session_audit_log", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_session_audit_created_at", table_name="session_audit_log")
    op.drop_index("ix_session_audit_event", table_name="session_audit_log")
    op.drop_index("ix_session_audit_email", table_name="session_audit_log")
    op.drop_table("session_audit_log")

    op.drop_index("ix_user_sessions_status", table_name="user_sessions")
    op.drop_index("ix_user_sessions_email_status", table_name="user_sessions")
    op.drop_index("uq_user_sessions_session_id", table_name="user_sessions")
    op.drop_table("user_sessions")
