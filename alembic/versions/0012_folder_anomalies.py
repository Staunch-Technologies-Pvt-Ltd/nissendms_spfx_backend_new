"""create folder_anomalies table

Revision ID: 0012
Revises: 37f602c5a96a
Create Date: 2026-08-10 13:15:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "37f602c5a96a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = set(inspector.get_table_names())

    if "folder_anomalies" not in tables:
        op.create_table(
            "folder_anomalies",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("drive_item_id", sa.String(256), nullable=False, unique=True),
            sa.Column("parent_drive_item_id", sa.String(256), nullable=True),
            sa.Column("name", sa.String(400), nullable=False),
            sa.Column("item_type", sa.String(20), nullable=False, server_default="folder"),
            sa.Column("anomaly_type", sa.String(50), nullable=False),
            sa.Column("department", sa.String(100), nullable=False),
            sa.Column("vessel_name", sa.String(200), nullable=True),
            sa.Column("spo_path", sa.String(1024), nullable=False),
            sa.Column("resolved", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("detected_at", sa.DateTime(), server_default=sa.func.now()),
        )
        op.create_index("ix_folder_anomalies_drive_item_id", "folder_anomalies", ["drive_item_id"])
        op.create_index("ix_folder_anomalies_anomaly_type", "folder_anomalies", ["anomaly_type"])
        op.create_index("ix_folder_anomalies_resolved", "folder_anomalies", ["resolved"])



def downgrade() -> None:
    op.drop_index("ix_folder_anomalies_resolved", table_name="folder_anomalies")
    op.drop_index("ix_folder_anomalies_anomaly_type", table_name="folder_anomalies")
    op.drop_index("ix_folder_anomalies_drive_item_id", table_name="folder_anomalies")
    op.drop_table("folder_anomalies")
