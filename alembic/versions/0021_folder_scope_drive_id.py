"""normalize folder cache scope to stable SharePoint drive IDs

Revision ID: 0021
Revises: 0020
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Site keys are aliases and can change or have multiple names for one
    # configured drive. Folder cache rows must use the stable drive identity.
    op.execute("""
        UPDATE folders AS folder
           SET site_id = sites.drive_id
          FROM site_configurations AS sites
         WHERE lower(folder.site_id) = lower(sites.site_key)
           AND sites.drive_id IS NOT NULL
           AND sites.drive_id <> ''
    """)


def downgrade() -> None:
    # Drive IDs remain valid cache scopes; no destructive reverse alias mapping.
    pass
