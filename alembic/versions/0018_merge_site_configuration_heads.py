"""merge audit and persisted site configuration migration heads

Revision ID: 0018
Revises: 0016, 0017
"""
from typing import Sequence, Union

revision: str = "0018"
down_revision: Union[str, tuple[str, str], None] = ("0016", "0017")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
