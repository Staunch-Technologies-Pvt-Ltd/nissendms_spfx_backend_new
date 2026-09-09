"""add read and modified timestamps to alerts and anomalies

Revision ID: 0014
Revises: 0013
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("folder_alerts", sa.Column("read_at", sa.DateTime(), nullable=True))
    op.add_column("folder_alerts", sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True))
    op.add_column("folder_anomalies", sa.Column("read", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("folder_anomalies", sa.Column("read_at", sa.DateTime(), nullable=True))
    op.add_column("folder_anomalies", sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True))
    op.create_index("ix_folder_anomalies_read", "folder_anomalies", ["read"])


def downgrade() -> None:
    op.drop_index("ix_folder_anomalies_read", table_name="folder_anomalies")
    op.drop_column("folder_anomalies", "updated_at")
    op.drop_column("folder_anomalies", "read_at")
    op.drop_column("folder_anomalies", "read")
    op.drop_column("folder_alerts", "updated_at")
    op.drop_column("folder_alerts", "read_at")