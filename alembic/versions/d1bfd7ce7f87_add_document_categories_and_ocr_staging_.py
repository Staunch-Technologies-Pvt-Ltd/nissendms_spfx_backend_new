"""add_document_categories_and_ocr_staging_files

Revision ID: d1bfd7ce7f87
Revises: 0015
Create Date: 2026-08-27 17:45:13.657959
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


"""add_document_categories_and_ocr_staging_files

Revision ID: d1bfd7ce7f87
Revises: 0015
Create Date: 2026-08-27 17:45:13.657959
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd1bfd7ce7f87'
down_revision: Union[str, None] = '0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS document_categories (
        id SERIAL PRIMARY KEY,
        name VARCHAR(200) NOT NULL UNIQUE,
        department VARCHAR(100),
        dms_path_template VARCHAR(1024),
        tag_fields_json TEXT NOT NULL DEFAULT '[{"key":"vessel","label":"Vessel Name","type":"select_vessel","required":true,"options":null},{"key":"group","label":"Department","type":"select_dept","required":true,"options":null},{"key":"category","label":"Category","type":"text","required":true,"options":null},{"key":"sub_category","label":"Sub-Category","type":"text","required":false,"options":null}]',
        ocr_hints_json TEXT NOT NULL DEFAULT '[]',
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_by_email VARCHAR(320),
        created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
        updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS ix_document_categories_name ON document_categories (name);
    CREATE INDEX IF NOT EXISTS ix_document_categories_is_active ON document_categories (is_active);

    CREATE TABLE IF NOT EXISTS ocr_staging_files (
        id SERIAL PRIMARY KEY,
        filename VARCHAR(400) NOT NULL,
        drive_item_id VARCHAR(256),
        source_folder_id VARCHAR(256),
        source_subfolder_path VARCHAR(1024),
        vessel_name VARCHAR(200),
        upload_source VARCHAR(20) NOT NULL DEFAULT 'direct',
        status VARCHAR(30) NOT NULL DEFAULT 'ocr_pending',
        category_id INTEGER REFERENCES document_categories(id) ON DELETE SET NULL,
        suggested_tags_json TEXT,
        ocr_text_preview TEXT,
        confidence DOUBLE PRECISION,
        matched_keywords_json TEXT,
        final_path VARCHAR(1024),
        uploaded_by_email VARCHAR(320),
        error TEXT,
        created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
        updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS ix_ocr_staging_files_drive_item_id ON ocr_staging_files (drive_item_id);
    CREATE INDEX IF NOT EXISTS ix_ocr_staging_files_upload_source ON ocr_staging_files (upload_source);
    CREATE INDEX IF NOT EXISTS ix_ocr_staging_files_status ON ocr_staging_files (status);

    -- Seed initial Drawing and Manual categories if not exists
    INSERT INTO document_categories (name, department, dms_path_template, tag_fields_json, ocr_hints_json, is_active)
    VALUES (
        'Drawing',
        'Technical & Crewing',
        '{group}/{vessel}/Drawings and Manuals/Drawing/{sub_category}',
        '[{"key":"vessel","label":"Vessel Name","type":"select_vessel","required":true,"options":null},{"key":"group","label":"Department","type":"select_dept","required":true,"options":null},{"key":"category","label":"Category","type":"text","required":true,"options":null},{"key":"sub_category","label":"Sub-Category / System","type":"select","required":true,"options":["Basic","Hull","Safety","Engine","Electrical","Archive","Other Drawings"]},{"key":"leaf","label":"Specific Plan / Title","type":"text","required":false,"options":null}]',
        '["general arrangement","capacity plan","trim & stability","loading manual","damage control","eedi","ssam","docking plan","emergency towing","hull","sea trial","container stowage","mooring arrangement","midship section","bulkhead","shell expansion","rudder","cargo securing","lsa plan","fire control","engine room","shafting","stern tube","bilge","ballast","fuel oil","lube oil","cooling water","air system","single line diagram","switchboard","power distribution"]',
        TRUE
    ) ON CONFLICT (name) DO NOTHING;

    INSERT INTO document_categories (name, department, dms_path_template, tag_fields_json, ocr_hints_json, is_active)
    VALUES (
        'Manual',
        'Technical & Crewing',
        '{group}/{vessel}/Drawings and Manuals/Manual/{sub_category}',
        '[{"key":"vessel","label":"Vessel Name","type":"select_vessel","required":true,"options":null},{"key":"group","label":"Department","type":"select_dept","required":true,"options":null},{"key":"category","label":"Category","type":"text","required":true,"options":null},{"key":"sub_category","label":"Sub-Category / System","type":"select","required":true,"options":["Main Engine","Auxiliary Engine","Boiler","Shafting","Steering Gear","Propulsion","Thrusters","Electrical","Automation","Cargo","Safety","Pollution","Refrigeration","Deck Machinery","Other Manuals"]},{"key":"leaf","label":"Specific Manual Title","type":"text","required":false,"options":null}]',
        '["main engine","auxiliary engine","diesel generator","boiler","stern tube","cpp","steering gear","shaft generator","thruster","power management","pms","switchboard","alarm monitoring","ams","engine control","cargo crane","hatch cover","ballast pump","cargo pump","inert gas","crude oil washing","odme","fire detection","fire alarm","co2 system","emergency generator","bwts","oily water separator","ows","sewage treatment","incinerator","scrubber","scr","egr","ac plant","windlass","mooring winch"]',
        TRUE
    ) ON CONFLICT (name) DO NOTHING;
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS ocr_staging_files CASCADE;
    DROP TABLE IF EXISTS document_categories CASCADE;
    """)
