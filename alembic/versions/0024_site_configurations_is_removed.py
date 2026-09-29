"""add is_removed column to site_configurations

Revision ID: 0024
Revises: 0023
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "site_configurations" in inspector.get_table_names():
        columns = {c["name"] for c in inspector.get_columns("site_configurations")}
        if "is_removed" not in columns:
            op.add_column(
                "site_configurations",
                sa.Column("is_removed", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "site_configurations" in inspector.get_table_names():
        columns = {c["name"] for c in inspector.get_columns("site_configurations")}
        if "is_removed" in columns:
            op.drop_column("site_configurations", "is_removed")
