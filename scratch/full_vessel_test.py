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

    # Page through ALL items and find any with vessel set
    vessel_count = 0
    no_vessel_count = 0
    total = 0
    url = f"/sites/{site_id}/lists/{list_id}/items?$expand=fields($select=Vessel_x0020_Name_x0020_)&$top=200"
    while url:
        resp = await client.graph().get(url)
        items = resp.get('value', [])
        for item in items:
            total += 1
            vessel = item.get('fields', {}).get('Vessel_x0020_Name_x0020_')
            if vessel:
                vessel_count += 1
                print(f"Item {item['id']}: {vessel}")
        next_link = resp.get('@odata.nextLink', '')
        url = next_link.replace('https://graph.microsoft.com/v1.0', '') if next_link else ''
    
    print(f"\nTotal: {total}, With vessel: {vessel_count}, Without: {total - vessel_count}")

    # CRITICAL TEST: Use the Graph API's taxonomy format for writing
    # Perhaps what's needed is NOT the note column but the actual Vessel_x0020_Name_x0020_ 
    # field with a taxonomy object structure
    # Try with @odata.type annotation
    print("\n\n=== Try taxonomy object format ===")
    snow_flower_guid = '43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'
    payloads = [
        # Format seen for Group/Category when reading: {Label, TermGuid, WssId}
        # Try writing same object format
        ("Object format", {"Vessel_x0020_Name_x0020_": {"Label": "Snow Flower", "TermGuid": snow_flower_guid, "WssId": -1}}),
        # Try with just the taxonomy termId in different ways
        ("termId format", {"Vessel_x0020_Name_x0020_@odata.type": "#microsoft.graph.taxonomy", "Vessel_x0020_Name_x0020_": snow_flower_guid}),
    ]
    for label, payload in payloads:
        print(f"\n--- {label} ---")
        try:
            res = await client.graph().patch(
                f"/sites/{site_id}/lists/{list_id}/items/17456/fields",
                json=payload
            )
            v = res.get('Vessel_x0020_Name_x0020_')
            print(f"Result Vessel: {v}")
            if v:
                print("*** SUCCESS ***")
        except Exception as e:
            print(f"Error: {e}")

    # Also check: what if the note col format needed is just "Label|Guid" without -1;# prefix?
    print("\n\n=== Try note col with Label|Guid (no prefix) ===")
    try:
        res = await client.graph().patch(
            f"/sites/{site_id}/lists/{list_id}/items/17456/fields",
            json={'i62be25c1f7249f48f51efaf91f1f739': f'Snow Flower|{snow_flower_guid}'}
        )
        v = res.get('Vessel_x0020_Name_x0020_')
        n = res.get('i62be25c1f7249f48f51efaf91f1f739')
        print(f"Vessel: {v}, Note: {n}")
    except Exception as e:
        print(f"Error: {e}")

    # Read item 17456 after the patches to see current state
    print("\n\n=== Item 17456 state after all patches ===")
    final = await client.graph().get(
        f"/sites/{site_id}/lists/{list_id}/items/17456/fields?$select=Vessel_x0020_Name_x0020_,i62be25c1f7249f48f51efaf91f1f739,Modified"
    )
    print(json.dumps({k: v for k, v in final.items() if not k.startswith('@')}, indent=2))

if __name__ == '__main__':
    asyncio.run(main())
