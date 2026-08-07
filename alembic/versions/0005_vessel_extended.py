"""add shipyard, hull_number, vessel_type to vessels

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-08
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vessels", sa.Column("shipyard",    sa.String(200), nullable=True))
    op.add_column("vessels", sa.Column("hull_number", sa.String(100), nullable=True))
    op.add_column("vessels", sa.Column("vessel_type", sa.String(50),  nullable=True))


def downgrade() -> None:
    op.drop_column("vessels", "vessel_type")
    op.drop_column("vessels", "hull_number")
    op.drop_column("vessels", "shipyard")
