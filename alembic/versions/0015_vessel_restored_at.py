"""add restored_at timestamp to vessels

Revision ID: 0015
Revises: 0014
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = set(inspector.get_table_names())
    if "vessels" not in tables:
        return
    columns = {c["name"] for c in inspector.get_columns("vessels")}
    if "restored_at" not in columns:
        op.add_column("vessels", sa.Column("restored_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = set(inspector.get_table_names())
    if "vessels" not in tables:
        return
    columns = {c["name"] for c in inspector.get_columns("vessels")}
    if "restored_at" in columns:
        op.drop_column("vessels", "restored_at")
