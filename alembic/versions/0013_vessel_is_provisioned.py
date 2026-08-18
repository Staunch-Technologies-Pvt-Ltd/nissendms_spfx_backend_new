"""Add is_provisioned column to vessels table

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-10 17:55:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {c["name"] for c in inspector.get_columns("vessels")}

    if "is_provisioned" not in columns:
        op.add_column(
            "vessels",
            sa.Column("is_provisioned", sa.Boolean(), nullable=False, server_default="1"),
        )


def downgrade() -> None:
    op.drop_column("vessels", "is_provisioned")
