import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.graph import drive as gd
from app.config import settings
from app.main import _build_sharepoint_metadata_payload

async def test():
    payload = _build_sharepoint_metadata_payload(
        department="Technical & Crewing",
        vessel="Bow Fighter",
        group="Drawings",
        category="Basic",
        sub_category="Capacity Plan & Dead Weight"
    )
    print("Payload passed to update_file_columns:", payload)
    
    item_id = "014ZGIJDOEKFVWWP5IEJEZMRT2M2BS2ECT"
    res = await gd.update_file_columns(settings.sp_drive_id, item_id, payload)
    print("Result of update_file_columns:", res)

asyncio.run(test())
