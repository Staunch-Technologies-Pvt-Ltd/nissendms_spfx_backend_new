"""add persisted site configurations

Revision ID: 0017
Revises: 0016
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "d1bfd7ce7f87"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = "0016"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "site_configurations" in inspector.get_table_names():
        return
    op.create_table(
        "site_configurations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("site_key", sa.String(100), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column("site_name", sa.String(256), nullable=False),
        sa.Column("site_id", sa.String(512), nullable=False),
        sa.Column("drive_id", sa.String(512), nullable=False),
        sa.Column("created_by_email", sa.String(320), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_key"),
    )
    op.create_index("ix_site_configurations_id", "site_configurations", ["id"])
    op.create_index("ix_site_configurations_site_key", "site_configurations", ["site_key"])


def downgrade() -> None:
    if "site_configurations" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_index("ix_site_configurations_site_key", table_name="site_configurations")
        op.drop_index("ix_site_configurations_id", table_name="site_configurations")
        op.drop_table("site_configurations")