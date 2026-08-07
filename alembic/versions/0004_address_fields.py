"""add granular address fields to user_profiles

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("user_profiles", sa.Column("office_name",   sa.String(200), nullable=True))
    op.add_column("user_profiles", sa.Column("address_line1", sa.String(300), nullable=True))
    op.add_column("user_profiles", sa.Column("address_line2", sa.String(300), nullable=True))
    op.add_column("user_profiles", sa.Column("area_locality", sa.String(200), nullable=True))
    op.add_column("user_profiles", sa.Column("landmark",      sa.String(200), nullable=True))
    op.add_column("user_profiles", sa.Column("city",          sa.String(100), nullable=True))
    op.add_column("user_profiles", sa.Column("state",         sa.String(100), nullable=True))
    op.add_column("user_profiles", sa.Column("postal_code",   sa.String(20),  nullable=True))
    op.add_column("user_profiles", sa.Column("country",       sa.String(100), nullable=True))


def downgrade() -> None:
    for col in ("office_name", "address_line1", "address_line2", "area_locality",
                "landmark", "city", "state", "postal_code", "country"):
        op.drop_column("user_profiles", col)
