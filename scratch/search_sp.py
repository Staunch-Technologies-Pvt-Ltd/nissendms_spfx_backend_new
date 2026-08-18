import asyncio
import os
os.chdir('c:/sharepoint spfx/backend')

from app.services.real_backend import RealBackend
from app.graph import drive as gd, client

be = RealBackend()

async def search_sp():
    drive_id = await be._drive()
    print("Drive ID:", drive_id)
    
    # Search for files
    res = await client.graph().get(f"/drives/{drive_id}/root/search(q='Vessel_Email')")
    print("Search results for 'Vessel_Email':", len(res.get("value", [])))
    for item in res.get("value", []):
        parent_ref = item.get("parentReference", {})
        print(f" - Name: {item.get('name')} | Path: {parent_ref.get('path')} | ID: {item.get('id')}")

    res2 = await client.graph().get(f"/drives/{drive_id}/root/search(q='Dashboard')")
    print("\nSearch results for 'Dashboard':", len(res2.get("value", [])))
    for item in res2.get("value", []):
        parent_ref = item.get("parentReference", {})
        print(f" - Name: {item.get('name')} | Path: {parent_ref.get('path')} | ID: {item.get('id')}")

asyncio.run(search_sp())
