"""Allow full SharePoint site IDs for vessel locations.

Revision ID: 0023
Revises: 0022
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "vessels" not in inspector.get_table_names():
        return

    columns = {column["name"]: column for column in inspector.get_columns("vessels")}
    site_key = columns.get("provisioned_site_key")
    if site_key is not None and not isinstance(site_key["type"], sa.Text):
        op.alter_column(
            "vessels",
            "provisioned_site_key",
            existing_type=site_key["type"],
            type_=sa.Text(),
            existing_nullable=site_key.get("nullable", True),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "vessels" not in inspector.get_table_names():
        return

    columns = {column["name"]: column for column in inspector.get_columns("vessels")}
    site_key = columns.get("provisioned_site_key")
    if site_key is not None and isinstance(site_key["type"], sa.Text):
        op.alter_column(
            "vessels",
            "provisioned_site_key",
            existing_type=site_key["type"],
            type_=sa.String(length=100),
            existing_nullable=site_key.get("nullable", True),
        )