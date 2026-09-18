"""scope folder cache rows by configured SharePoint site

Revision ID: 0020
Revises: 0019
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("folders", sa.Column("site_id", sa.String(length=200), nullable=True))
    op.execute("UPDATE folders SET site_id = COALESCE(NULLIF(current_setting('app.active_site', true), ''), 'local') WHERE site_id IS NULL")
    op.alter_column("folders", "site_id", nullable=False)

    # Older databases do not all use the same name for the original unique
    # path constraint. Remove any one-column unique constraint/index that
    # covers only folders.path before adding the site-scoped constraint.
    op.execute("""
        DO $$
        DECLARE
            constraint_name text;
            index_name text;
        BEGIN
            SELECT c.conname
              INTO constraint_name
              FROM pg_constraint c
              JOIN pg_class t ON t.oid = c.conrelid
             WHERE t.relname = 'folders'
               AND c.contype = 'u'
               AND c.conkey = ARRAY[
                   (SELECT a.attnum FROM pg_attribute a
                     WHERE a.attrelid = t.oid AND a.attname = 'path')
               ]::smallint[]
             LIMIT 1;

            IF constraint_name IS NOT NULL THEN
                EXECUTE format('ALTER TABLE folders DROP CONSTRAINT %I', constraint_name);
            END IF;

            FOR index_name IN
                SELECT i.relname
                  FROM pg_index x
                  JOIN pg_class i ON i.oid = x.indexrelid
                  JOIN pg_class t ON t.oid = x.indrelid
                 WHERE t.relname = 'folders'
                   AND x.indisunique
                   AND x.indnatts = 1
                   AND x.indkey[0] = (
                       SELECT a.attnum FROM pg_attribute a
                        WHERE a.attrelid = t.oid AND a.attname = 'path'
                   )
            LOOP
                EXECUTE format('DROP INDEX IF EXISTS %I', index_name);
            END LOOP;
        END $$;
    """)
    op.create_unique_constraint("uq_folders_site_path", "folders", ["site_id", "path"])
    op.create_index("ix_folders_site_id", "folders", ["site_id"])


def downgrade() -> None:
    op.drop_index("ix_folders_site_id", table_name="folders")
    op.drop_constraint("uq_folders_site_path", "folders", type_="unique")
    op.create_unique_constraint("folders_path_key", "folders", ["path"])
    op.drop_column("folders", "site_id")
