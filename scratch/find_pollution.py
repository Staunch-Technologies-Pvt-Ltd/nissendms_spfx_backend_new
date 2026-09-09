import asyncio
import os
import sys
import json

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')

from app.graph import client

async def main():
    sites = await client.graph().get("/sites?search=NKSDocMan")
    site = sites["value"][0]
    site_id = site["id"]
    print("Site ID:", site_id)

    drives = await client.graph().get(f"/sites/{site_id}/drives")
    drive = None
    for d in drives.get("value", []):
        if d.get("name") in ("Documents", "Shared Documents"):
            drive = d
            break
    if not drive:
        drive = drives["value"][0]
    drive_id = drive["id"]
    print("Drive ID:", drive_id)

    # Search for MM-25_INSTRUCTION
    res = await client.graph().get(f"/drives/{drive_id}/root/search(q='MM-25_INSTRUCTION')")
    items = res.get("value", [])
    print(f"Found {len(items)} items")
    for it in items:
        print(f"\nItem: {it.get('name')} (id: {it.get('id')})")
        parent = (it.get('parentReference') or {}).get('path', '')
        print(f"Parent path: {parent}")
        
        fields = await client.graph().get(f"/drives/{drive_id}/items/{it.get('id')}/listItem/fields")
        print("Fields:")
        for k, v in fields.items():
            if not k.startswith("@"):
                print(f"  {k}: {v}")

        meta = await client.graph().get(f"/drives/{drive_id}/items/{it.get('id')}?$select=sharepointIds")
        list_id = meta.get("sharepointIds", {}).get("listId")
        print("List ID:", list_id)

        cols = await client.graph().get(f"/sites/{site_id}/lists/{list_id}/columns?expand=hidden")
        print("\nAll columns in library with 'vessel' or ending with '_0':")
        for c in cols.get("value", []):
            d = c.get("displayName") or ""
            n = c.get("name") or ""
            if "vessel" in d.lower() or "vessel" in n.lower() or d.endswith("_0") or n.endswith("_0"):
                print(f"  col: name={n} | displayName={d} | type={c.get('type')} | isHidden={c.get('hidden')}")

if __name__ == "__main__":
    asyncio.run(main())
