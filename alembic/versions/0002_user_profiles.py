"""add user_profiles table

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("azure_oid", sa.String(length=36), nullable=True),
        sa.Column("job_title", sa.String(length=200), nullable=True),
        sa.Column("phone", sa.String(length=50), nullable=True),
        sa.Column("office_location", sa.String(length=300), nullable=True),
        sa.Column("company_name", sa.String(length=200), nullable=True),
        sa.Column("employee_id", sa.String(length=100), nullable=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("last_login", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_user_profiles_email", "user_profiles", ["email"])


def downgrade() -> None:
    op.drop_index("ix_user_profiles_email", table_name="user_profiles")
    op.drop_table("user_profiles")
