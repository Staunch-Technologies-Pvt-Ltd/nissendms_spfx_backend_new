import asyncio
import sys
import os
import json

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client

async def main():
    site_id = 'nissenkaiunsingapore.sharepoint.com,8688e65a-9abf-46ff-bc2c-62a4f3654580,73bdf9ab-0856-42f4-be7b-8fb38ee6d2cc'
    list_id = 'fa4dfba9-0060-4c2a-982a-15ad4a1661fa'
    
    # Item 12885 has Snow Flower with WssId=57. Check its note column value.
    # This will tell us the EXACT format needed to set vessel.
    print("=== Item 12885 (Snow Flower) ALL fields ===")
    fields = await client.graph().get(
        f"/sites/{site_id}/lists/{list_id}/items/12885/fields"
    )
    # Show key fields
    vessel = fields.get('Vessel_x0020_Name_x0020_')
    note_col = fields.get('i62be25c1f7249f48f51efaf91f1f739')
    print(f"Vessel: {vessel}")
    print(f"Note col: {note_col!r}")
    
    # Also show all non-null taxonomy fields
    for k, v in sorted(fields.items()):
        if v and not k.startswith('@') and k != 'id':
            print(f"  {k}: {v!r}")

    # Now: TRY to set item 17456 using WssId=57 format via note col
    # (the correct format now that we know WssId=57 is valid for Snow Flower in this list)
    print("\n\n=== Patching item 17456 with WssId=57 format ===")
    snow_flower_guid = '43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'
    
    # Try the format: WssId;#Label|Guid (the standard taxonomy note format)
    for fmt_label, fmt_val in [
        ("57;#Label|Guid", f'57;#Snow Flower|{snow_flower_guid}'),
        ("-1;#Label|Guid", f'-1;#Snow Flower|{snow_flower_guid}'),
        ("Label|Guid", f'Snow Flower|{snow_flower_guid}'),
    ]:
        print(f"\n--- {fmt_label} ---")
        try:
            res = await client.graph().patch(
                f"/sites/{site_id}/lists/{list_id}/items/17456/fields",
                json={'i62be25c1f7249f48f51efaf91f1f739': fmt_val}
            )
            v = res.get('Vessel_x0020_Name_x0020_')
            n = res.get('i62be25c1f7249f48f51efaf91f1f739')
            print(f"Vessel: {v}")
            print(f"Note col: {n!r}")
            if v:
                print(f"*** SUCCESS with {fmt_label} ***")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == '__main__':
    asyncio.run(main())
