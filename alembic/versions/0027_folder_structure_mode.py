"""folder structure mode: vessels.folder_structure_mode + app_settings

Revision ID: 0027
Revises: 0026
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable, no default: existing vessels read as NULL == "empty_pool",
    # i.e. exactly today's behaviour. No existing row is rewritten.
    op.add_column("vessels", sa.Column("folder_structure_mode", sa.String(20), nullable=True))
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(320), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
    op.drop_column("vessels", "folder_structure_mode")
