import asyncio
import sys
import os
import json

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client, drive as gd

async def test():
    site_id = 'nissenkaiunsingapore.sharepoint.com,8688e65a-9abf-46ff-bc2c-62a4f3654580,73bdf9ab-0856-42f4-be7b-8fb38ee6d2cc'
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    list_id = 'fa4dfba9-0060-4c2a-982a-15ad4a1661fa'

    # Search for files under Pollution
    res = await client.graph().get(f"/drives/{drive_id}/root/search(q='MM-25_INSTRUCTION MANUAL')")
    items = res.get('value', [])
    target = None
    for it in items:
        detail = await client.graph().get(f"/drives/{drive_id}/items/{it['id']}")
        p = (detail.get('parentReference') or {}).get('path', '')
        if 'Pollution' in p and 'Snow Flower' in p:
            target = (it['id'], it['name'], p)
            break
        elif 'Pollution' in p:
            target = (it['id'], it['name'], p)
    
    if not target:
        print("Target not found via search, let's search all items in folder")
        # Let's search for folder 'Pollution' under Snow Flower
        folders = (await client.graph().get(f"/drives/{drive_id}/root/search(q='Pollution')")).get('value', [])
        for f in folders:
            if f.get('folder'):
                fdetail = await client.graph().get(f"/drives/{drive_id}/items/{f['id']}")
                fp = (fdetail.get('parentReference') or {}).get('path', '')
                if 'Snow Flower' in fp:
                    print(f"Found Snow Flower Pollution folder: {f['id']} at {fp}")
                    # list children
                    children = (await client.graph().get(f"/drives/{drive_id}/items/{f['id']}/children")).get('value', [])
                    for ch in children:
                        print(f"  Child: {ch.get('name')} (id: {ch.get('id')})")
                        if 'MM-25' in ch.get('name', ''):
                            target = (ch['id'], ch['name'], fp + '/' + f['name'])
                    break

    if not target:
        print("Could not find target item!")
        return

    item_id, item_name, item_path = target
    print(f"\nTarget item: {item_name} (id: {item_id})")
    print(f"Path: {item_path}")

    # Inspect current fields
    fields = await client.graph().get(f"/drives/{drive_id}/items/{item_id}/listItem/fields")
    print("\nCurrent fields:")
    for k, v in fields.items():
        if not k.startswith('@'):
            print(f"  {k}: {v}")

    # Resolve Snow Flower
    term_res = await gd._resolve_term_guid(site_id, "vessel", "Snow Flower")
    print(f"\nResolved Snow Flower: {term_res}")

    # Now let's try patching with note column i62be25c1f7249f48f51efaf91f1f739:
    # note column format: -1;#Label|Guid
    label, guid = term_res
    note_payload = {
        "i62be25c1f7249f48f51efaf91f1f739": f"-1;#{label}|{guid}"
    }
    print(f"Patching with note payload: {note_payload}")
    patch_res = await client.graph().patch(
        f"/drives/{drive_id}/items/{item_id}/listItem/fields",
        json=note_payload
    )
    print("PATCH RESULT:")
    for k in ('Vessel_x0020_Name', 'Vessel_x0020_Name_x0020_', 'Category', 'Group', 'Domain'):
        if k in patch_res:
            print(f"  {k}: {patch_res[k]}")

if __name__ == '__main__':
    asyncio.run(test())
