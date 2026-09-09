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

    # Let's check item 17456 full field listing to see what IS there
    print("=== Item 17456 current fields ===")
    resp = await client.graph().get(
        f"/sites/{site_id}/lists/{list_id}/items/17456/fields"
    )
    # Show taxonomy-related fields
    for k, v in sorted(resp.items()):
        if v and k not in ['@odata.etag', 'id']:
            print(f"  {k}: {v!r}")

    print("\n\n=== Testing: can we set Vessel via the drive endpoint instead of list endpoint? ===")
    # Try to set via DRIVE listItem endpoint
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    drive_item_id = '01YT4WOQBC74GSEYD3WRAYAPCX4NGZ5R3R'
    
    # What does the drive endpoint return for this item's fields?
    drive_fields = await client.graph().get(
        f"/drives/{drive_id}/items/{drive_item_id}/listItem/fields"
    )
    print("Vessel in drive fields:", drive_fields.get('Vessel_x0020_Name_x0020_'))
    print("Group in drive fields:", drive_fields.get('Group'))
    print("Category in drive fields:", drive_fields.get('Category'))
    
    # The code currently uses -1;# format for note columns. 
    # Let's try: what does the Group note col look like for item 4814?
    # and can we use SAME format to set Vessel?
    # Since note col patches return None (silently fail), 
    # let's check if updating Vessel Name works when the WssId matches an existing term
    
    # Find an item that already has vessel set in this library
    print("\n=== Finding item with vessel set ===")
    items_resp = await client.graph().get(
        f"/sites/{site_id}/lists/{list_id}/items?$expand=fields&$top=200"
    )
    items = (items_resp or {}).get('value', [])
    vessel_items = [(i['id'], i['fields'].get('Vessel_x0020_Name_x0020_')) for i in items if i.get('fields', {}).get('Vessel_x0020_Name_x0020_')]
    print(f"Items with vessel set: {len(vessel_items)}")
    for iid, v in vessel_items[:5]:
        print(f"  item {iid}: {v}")

    # Now let's try: set vessel on item 17456 using the WssId of Snow Flower
    # We know: Snow Flower WssId in this library isn't known yet
    # From the Vessel_x0020_Name_x0020_ field we see it returns {Label, TermGuid, WssId}
    # The WssId is a local list-specific term ID. We need to find Snow Flower's WssId in THIS list
    snow_flower_guid = '43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'
    
    # Find Snow Flower's WssId - look at items with Snow Flower vessel
    snow_items = [(i['id'], i['fields'].get('Vessel_x0020_Name_x0020_')) for i in items if isinstance(i.get('fields', {}).get('Vessel_x0020_Name_x0020_'), dict) and 'Snow' in str(i['fields']['Vessel_x0020_Name_x0020_'].get('Label', ''))]
    print(f"\nSnow Flower items: {snow_items[:3]}")
    
    # Let's also check via the TaxCatchAll
    print("\n=== TaxCatchAll for item 4814 ===")
    catch_resp = await client.graph().get(
        f"/sites/{site_id}/lists/{list_id}/items/4814/fields?$select=TaxCatchAll"
    )
    print(f"TaxCatchAll: {catch_resp.get('TaxCatchAll')}")

if __name__ == '__main__':
    asyncio.run(main())
