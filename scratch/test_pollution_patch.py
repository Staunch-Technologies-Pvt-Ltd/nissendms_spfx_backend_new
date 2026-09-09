import asyncio
import sys
import os
import json

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client, drive as gd

async def test():
    site_id = 'nissenkaiunsingapore.sharepoint.com,d53340da-789f-439e-8b40-0e75575184c0,6b456675-cd99-49f2-8e27-c95b9295b925'
    drives = await client.graph().get(f'/sites/{site_id}/drives')
    drive_id = None
    for d in drives['value']:
        if d.get('name') in ('Documents', 'Shared Documents'):
            drive_id = d['id']
            break
    
    # Search for items
    query_url = f"/drives/{drive_id}/root/search(q='MM-25_INSTRUCTION')"
    items = (await client.graph().get(query_url)).get('value', [])
    print(f"Found {len(items)} items matching MM-25_INSTRUCTION")
    target_item = None
    for it in items:
        raw_p = (it.get('parentReference') or {}).get('path') or ''
        print(f"  Item: {it.get('name')} | path: {raw_p} | id: {it.get('id')}")
        if 'Pollution' in raw_p or 'Manuals' in raw_p:
            target_item = it
            break
    
    if not target_item and items:
        target_item = items[0]
        
    if not target_item:
        print("No item found!")
        return

    item_id = target_item['id']
    print(f"\n--- Testing on: {target_item.get('name')} ({item_id}) ---")
    
    # Inspect listItem/fields before
    fields_before = await client.graph().get(f"/drives/{drive_id}/items/{item_id}/listItem/fields")
    print("Fields BEFORE update:")
    for k, v in fields_before.items():
        if not k.startswith('@'):
            print(f"  {k}: {v}")

    # Inspect list columns for this item
    item_meta = await client.graph().get(f"/drives/{drive_id}/items/{item_id}?$select=sharepointIds")
    sp_ids = item_meta.get("sharepointIds") or {}
    list_id = sp_ids.get("listId")
    cols = await client.graph().get(f"/sites/{site_id}/lists/{list_id}/columns?expand=hidden")
    print("\nColumns related to Vessel:")
    for c in cols.get("value", []):
        d = c.get("displayName") or ""
        n = c.get("name") or ""
        if "vessel" in d.lower() or "vessel" in n.lower():
            print(f"  name={n!r}, displayName={d!r}, type={c.get('type')!r}, hidden={c.get('hidden')!r}")

    # Test what update_file_columns does:
    payload = {
        "Department": "Technical and Crewing",
        "VesselName": "Snow Flower",
        "Group": "Manuals",
        "Category": "Pollution"
    }
    print(f"\nCalling update_file_columns with payload: {payload}")
    res = await gd.update_file_columns(drive_id, item_id, payload)
    print("update_file_columns RESULT:")
    print(json.dumps(res, indent=2))

    # Inspect listItem/fields after
    fields_after = await client.graph().get(f"/drives/{drive_id}/items/{item_id}/listItem/fields")
    print("\nFields AFTER update:")
    for k, v in fields_after.items():
        if not k.startswith('@'):
            print(f"  {k}: {v}")

if __name__ == '__main__':
    asyncio.run(test())
