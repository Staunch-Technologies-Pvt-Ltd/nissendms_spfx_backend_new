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

    # Get ALL columns including hidden ones - page through all
    all_cols = []
    url = f"/sites/{site_id}/lists/{list_id}/columns?$top=200&$select=name,displayName,id,hidden,readOnly"
    while url:
        resp = await client.graph().get(url)
        all_cols.extend(resp.get('value', []))
        url = resp.get('@odata.nextLink', '').replace('https://graph.microsoft.com/v1.0', '')
        if not url:
            break

    print(f"Total columns: {len(all_cols)}")

    # Find any note companion columns for Vessel, Category, Group
    # Standard SP taxonomy note col name = first_hex_digit_of_guid + rest_of_guid_no_dashes
    vessel_id = '262be25c-1f72-49f4-8f51-efaf91f1f739'
    cat_id = 'a57855ae-762b-4890-92e6-d62b4a1dc5b9'
    grp_id = '05e02a9a-aa48-4560-a09d-362f0e15c3fa'

    for label, guid in [('Vessel', vessel_id), ('Category', cat_id), ('Group', grp_id)]:
        # Expected note col name: first char + rest (no dashes)
        hex_no_dash = guid.replace('-','')
        first_char = hex_no_dash[0]
        note_name_expected = first_char + hex_no_dash[1:]  # this IS the same as hex_no_dash
        print(f"\n{label} guid: {guid}")
        print(f"Expected note col internal name: {note_name_expected}")

    # Find note cols (_0 pattern)
    note_cols = [c for c in all_cols if c.get('displayName','').endswith('_0') or c.get('displayName','').endswith(' _0')]
    print(f"\nNote columns (_0 pattern): {len(note_cols)}")
    for c in note_cols:
        print(f"  name={c['name']}, displayName={c['displayName']}, hidden={c.get('hidden')}")

    # Look for items that have Vessel set in NKSDocMan
    print("\n\nSearching for items with Vessel set...")
    items_url = f"/sites/{site_id}/lists/{list_id}/items?$expand=fields($select=id,Vessel_x0020_Name_x0020_)&$top=50&$filter=fields/Vessel_x0020_Name_x0020_ ne null"
    try:
        items_resp = await client.graph().get(items_url)
        items = (items_resp or {}).get('value', [])
        print(f"Found {len(items)} items with vessel")
        for item in items[:5]:
            fields = item.get('fields', {})
            print(f"  Item ID={item['id']}, vessel={fields.get('Vessel_x0020_Name_x0020_')}")
    except Exception as e:
        print(f"Filter failed: {e}")
        # Try without filter
        items_url2 = f"/sites/{site_id}/lists/{list_id}/items?$expand=fields($select=id,Vessel_x0020_Name_x0020_)&$top=50"
        items_resp = await client.graph().get(items_url2)
        items = (items_resp or {}).get('value', [])
        for item in items[:10]:
            fields = item.get('fields', {})
            vessel = fields.get('Vessel_x0020_Name_x0020_')
            if vessel:
                print(f"  Item ID={item['id']}, vessel={vessel}")

if __name__ == '__main__':
    asyncio.run(main())
