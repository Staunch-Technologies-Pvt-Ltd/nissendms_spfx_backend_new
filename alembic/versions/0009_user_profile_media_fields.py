"""add missing profile media/date fields

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing = {c["name"] for c in inspector.get_columns("user_profiles")}
    if "photo_base64" not in existing:
        op.add_column("user_profiles", sa.Column("photo_base64", sa.Text(), nullable=True))
    if "date_of_joining" not in existing:
        op.add_column("user_profiles", sa.Column("date_of_joining", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_profiles", "date_of_joining")
    op.drop_column("user_profiles", "photo_base64")
