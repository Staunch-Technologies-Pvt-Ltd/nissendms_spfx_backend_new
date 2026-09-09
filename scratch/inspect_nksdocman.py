import asyncio
import sys
import os
import json

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client, drive as gd

async def main():
    site_id = 'nissenkaiunsingapore.sharepoint.com,8688e65a-9abf-46ff-bc2c-62a4f3654580,73bdf9ab-0856-42f4-be7b-8fb38ee6d2cc'
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'

    # Search for MM-25_INSTRUCTION MANUAL.pdf
    res = await client.graph().get(f"/drives/{drive_id}/root/search(q='MM-25_INSTRUCTION MANUAL')")
    items = res.get('value', [])
    print(f"Found {len(items)} items in NKSDocMan")
    for it in items:
        iid = it['id']
        name = it['name']
        # get item with parentReference
        item_detail = await client.graph().get(f"/drives/{drive_id}/items/{iid}")
        parent_path = (item_detail.get('parentReference') or {}).get('path', '')
        print(f"\nFile: {name} (id: {iid})")
        print(f"Parent path: {parent_path}")
        
        # listItem fields
        fields = await client.graph().get(f"/drives/{drive_id}/items/{iid}/listItem/fields")
        print("Fields:")
        for k in ('VesselName', 'Vessel_x0020_Name', 'Vessel_x0020_Name_x0020_', 'Category', 'Group', 'Domain', 'Sub_x002d_Category', 'SubCategory'):
            if k in fields:
                print(f"  {k}: {fields[k]}")
        # Note fields
        for k, v in fields.items():
            if k.endswith('_0') or len(k) == 32:
                print(f"  [hidden/note] {k}: {v}")

        # List columns for this list
        sp_ids = item_detail.get('sharepointIds') or {}
        list_id = sp_ids.get('listId')
        print(f"List ID: {list_id}")
        cols = await client.graph().get(f"/sites/{site_id}/lists/{list_id}/columns?expand=hidden")
        print("Vessel / Note columns in library:")
        for c in cols.get('value', []):
            d = c.get('displayName') or ''
            n = c.get('name') or ''
            if 'vessel' in d.lower() or 'vessel' in n.lower() or d.endswith('_0') or n.endswith('_0'):
                print(f"  name={n!r} | displayName={d!r} | type={c.get('type')!r} | hidden={c.get('hidden')!r}")

        # Test resolving term for Snow Flower on THIS site
        info = await gd._get_term_store_info(site_id)
        print("\nTerm store info for NKSDocMan site:")
        print("  vessel_set_id:", info.get("vessel_set_id"))
        print("  dms_set_id:", info.get("dms_set_id"))
        print("  vessel_terms count:", len(info.get("vessel_terms", [])))
        term_res = await gd._resolve_term_guid(site_id, "vessel", "Snow Flower")
        print("  _resolve_term_guid for Snow Flower on NKSDocMan:", term_res)

        break

if __name__ == '__main__':
    asyncio.run(main())
