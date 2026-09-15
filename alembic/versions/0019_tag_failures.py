"""add persistent tag failure retry queue"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0019"
down_revision: Union[str, tuple[str, str], None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tag_failures",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("file_id", sa.String(256), nullable=False),
        sa.Column("site_id", sa.String(256), nullable=False),
        sa.Column("drive_id", sa.String(256), nullable=False),
        sa.Column("filename", sa.String(500), nullable=False, server_default=""),
        sa.Column("parent_path", sa.String(1024), nullable=False, server_default=""),
        sa.Column("error_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(30), nullable=False, server_default="needs_retry"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_failed_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("last_attempted_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("dismissed_by", sa.String(320), nullable=True),
        sa.Column("dismissed_reason", sa.Text(), nullable=True),
    )
    op.create_index("ix_tag_failures_file_id", "tag_failures", ["file_id"])
    op.create_index("ix_tag_failures_site_id", "tag_failures", ["site_id"])
    op.create_index("ix_tag_failures_drive_id", "tag_failures", ["drive_id"])
    op.create_index("ix_tag_failures_status", "tag_failures", ["status"])


def downgrade() -> None:
    op.drop_table("tag_failures")