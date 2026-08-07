"""vessel folder pool: pool_slots, replenish_jobs, folders.pool_slot_id

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pool_slots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="building"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_pool_slots_slug", "pool_slots", ["slug"])
    op.create_index("ix_pool_slots_status", "pool_slots", ["status"])

    op.create_table(
        "replenish_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("triggering_slot_id", sa.Integer(),
                  sa.ForeignKey("pool_slots.id", ondelete="SET NULL"), nullable=True),
        sa.Column("new_slot_id", sa.Integer(),
                  sa.ForeignKey("pool_slots.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_replenish_jobs_status", "replenish_jobs", ["status"])

    op.add_column(
        "folders",
        sa.Column("pool_slot_id", sa.Integer(),
                  sa.ForeignKey("pool_slots.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_folders_pool_slot_id", "folders", ["pool_slot_id"])


def downgrade() -> None:
    op.drop_index("ix_folders_pool_slot_id", table_name="folders")
    op.drop_column("folders", "pool_slot_id")
    op.drop_index("ix_replenish_jobs_status", table_name="replenish_jobs")
    op.drop_table("replenish_jobs")
    op.drop_index("ix_pool_slots_status", table_name="pool_slots")
    op.drop_index("ix_pool_slots_slug", table_name="pool_slots")
    op.drop_table("pool_slots")
