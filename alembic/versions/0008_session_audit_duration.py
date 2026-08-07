"""add session duration and status to session_audit_log

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("session_audit_log", sa.Column("login_time", sa.DateTime(), nullable=True))
    op.add_column("session_audit_log", sa.Column("logout_time", sa.DateTime(), nullable=True))
    op.add_column("session_audit_log", sa.Column("active_duration", sa.Integer(), nullable=True))
    op.add_column("session_audit_log", sa.Column("active_duration_formatted", sa.String(length=100), nullable=True))
    op.add_column("session_audit_log", sa.Column("status", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("session_audit_log", "status")
    op.drop_column("session_audit_log", "active_duration_formatted")
    op.drop_column("session_audit_log", "active_duration")
    op.drop_column("session_audit_log", "logout_time")
    op.drop_column("session_audit_log", "login_time")
