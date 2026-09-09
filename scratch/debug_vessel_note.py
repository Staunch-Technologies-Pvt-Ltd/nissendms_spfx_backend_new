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

    # Item 4814 has Bow Fighter vessel. Get its note column value to understand the format.
    # Also let's look at item 17456 which is the failing one (Snow Flower)
    for item_id in ['4814', '17456']:
        print(f"\n=== Item {item_id} ===")
        resp = await client.graph().get(
            f"/sites/{site_id}/lists/{list_id}/items/{item_id}/fields",
            params={'$select': 'Vessel_x0020_Name_x0020_,i62be25c1f7249f48f51efaf91f1f739'}
        )
        print(f"Vessel: {resp.get('Vessel_x0020_Name_x0020_')}")
        print(f"Note col: {resp.get('i62be25c1f7249f48f51efaf91f1f739')}")

    # Now - the CRITICAL test: patch item 17456 using the CORRECT WssId for Snow Flower
    # Item 4814 has WssId=10 for Bow Fighter. Snow Flower = WssId=57 (from test_actual_update.py)
    # But let's verify: what WssId is Snow Flower in THIS list's taxonomy?
    # Try patching with WssId=57 (Snow Flower) into i62be25c... for item 17456
    snow_flower_guid = '43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'
    
    # First try with WssId 57
    print("\n\n=== Patching item 17456 with WssId=57 ===")
    payload = {'i62be25c1f7249f48f51efaf91f1f739': f'57;#Snow Flower|{snow_flower_guid}'}
    try:
        res = await client.graph().patch(
            f"/sites/{site_id}/lists/{list_id}/items/17456/fields",
            json=payload
        )
        print(f"Vessel after patch: {res.get('Vessel_x0020_Name_x0020_')}")
        print(f"Note col after patch: {res.get('i62be25c1f7249f48f51efaf91f1f739')}")
    except Exception as e:
        print(f"Error: {e}")

    # Also try with WssId=-1
    print("\n\n=== Patching item 17456 with WssId=-1 ===")
    payload2 = {'i62be25c1f7249f48f51efaf91f1f739': f'-1;#Snow Flower|{snow_flower_guid}'}
    try:
        res2 = await client.graph().patch(
            f"/sites/{site_id}/lists/{list_id}/items/17456/fields",
            json=payload2
        )
        print(f"Vessel after patch: {res2.get('Vessel_x0020_Name_x0020_')}")
        print(f"Note col after patch: {res2.get('i62be25c1f7249f48f51efaf91f1f739')}")
    except Exception as e:
        print(f"Error: {e}")

    # Now check: what WssId does Snow Flower actually have in this list?
    print("\n\n=== Finding Snow Flower WssId from existing items ===")
    items_resp = await client.graph().get(
        f"/sites/{site_id}/lists/{list_id}/items?$expand=fields($select=Vessel_x0020_Name_x0020_,i62be25c1f7249f48f51efaf91f1f739)&$top=100"
    )
    items = (items_resp or {}).get('value', [])
    for item in items:
        f = item.get('fields', {})
        vessel = f.get('Vessel_x0020_Name_x0020_')
        note = f.get('i62be25c1f7249f48f51efaf91f1f739')
        if vessel and isinstance(vessel, dict) and 'Snow Flower' in str(vessel.get('Label', '')):
            print(f"  Item {item['id']}: vessel={vessel}, note={note}")
            break

if __name__ == '__main__':
    asyncio.run(main())
