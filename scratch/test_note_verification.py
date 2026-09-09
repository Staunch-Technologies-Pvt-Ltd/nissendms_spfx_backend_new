import asyncio
import sys
import os
import json

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client as graph_client

async def main():
    site_id = 'nissenkaiunsingapore.sharepoint.com,8688e65a-9abf-46ff-bc2c-62a4f3654580,73bdf9ab-0856-42f4-be7b-8fb38ee6d2cc'
    list_id = 'fa4dfba9-0060-4c2a-982a-15ad4a1661fa'
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    
    snow_guid = '43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'
    vessel_note = 'i62be25c1f7249f48f51efaf91f1f739'
    
    # === TEST 1: Find a NEW item (never had vessel set) and try note col write ===
    # Item 3052 had no Category - we successfully set Category via note col earlier.
    # Let's find an item with no Vessel and try to set it via note col.
    # Item 17456 = our test item
    
    # First, confirm current state
    print("=== Current state of item 17456 ===")
    curr = await graph_client.graph().get(
        f"/sites/{site_id}/lists/{list_id}/items/17456/fields?$select=Vessel_x0020_Name_x0020_,Modified"
    )
    print(f"Vessel: {curr.get('Vessel_x0020_Name_x0020_')}")
    print(f"Modified: {curr.get('Modified')}")
    
    # Write note col
    print("\n=== Patching note col ===")
    patch_res = await graph_client.graph().patch(
        f"/sites/{site_id}/lists/{list_id}/items/17456/fields",
        json={vessel_note: f'-1;#Snow Flower|{snow_guid}'}
    )
    print(f"Patch response Vessel: {patch_res.get('Vessel_x0020_Name_x0020_')}")
    
    # Immediately read the field back separately
    print("\n=== Reading field immediately after patch ===")
    after = await graph_client.graph().get(
        f"/sites/{site_id}/lists/{list_id}/items/17456/fields?$select=Vessel_x0020_Name_x0020_,Modified"
    )
    print(f"Vessel (immediate read): {after.get('Vessel_x0020_Name_x0020_')}")
    print(f"Modified: {after.get('Modified')}")
    
    # Try item 3052 (no vessel) - we tested Category set on it earlier
    print("\n\n=== Testing vessel note col on item 3052 (fresh item, no vessel) ===")
    curr3052 = await graph_client.graph().get(
        f"/sites/{site_id}/lists/{list_id}/items/3052/fields?$select=Vessel_x0020_Name_x0020_,Category,Modified"
    )
    print(f"Current - Vessel: {curr3052.get('Vessel_x0020_Name_x0020_')}, Cat: {curr3052.get('Category')}")
    
    # Set vessel via note col
    patch_3052 = await graph_client.graph().patch(
        f"/sites/{site_id}/lists/{list_id}/items/3052/fields",
        json={vessel_note: f'-1;#Snow Flower|{snow_guid}'}
    )
    print(f"Patch response Vessel: {patch_3052.get('Vessel_x0020_Name_x0020_')}")
    
    # Read back separately
    after_3052 = await graph_client.graph().get(
        f"/sites/{site_id}/lists/{list_id}/items/3052/fields?$select=Vessel_x0020_Name_x0020_,Category,Modified"
    )
    print(f"After patch - Vessel: {after_3052.get('Vessel_x0020_Name_x0020_')}")
    
    # Also check TaxCatchAll after patch
    tca_3052 = await graph_client.graph().get(
        f"/sites/{site_id}/lists/{list_id}/items/3052/fields?$select=TaxCatchAll"
    )
    print(f"TaxCatchAll WssId 57 present: {any(t.get('LookupId') == 57 for t in (tca_3052.get('TaxCatchAll') or []))}")

if __name__ == '__main__':
    asyncio.run(main())
