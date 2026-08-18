import asyncio
import os
os.chdir('c:/sharepoint spfx/backend')

from app.services.real_backend import RealBackend
from app.graph import client

be = RealBackend()

async def list_all_sites_and_drives():
    # 1. Root site
    root_site = await client.graph().get("/sites/root")
    print("Root Site ID:", root_site.get("id"), "WebUrl:", root_site.get("webUrl"))
    
    drives = await client.graph().get(f"/sites/{root_site['id']}/drives")
    print("\n--- DRIVES ON ROOT SITE ---")
    for d in drives.get("value", []):
        print(f" - Name: {d.get('name')} | ID: {d.get('id')} | Type: {d.get('driveType')} | WebUrl: {d.get('webUrl')}")
        
    # Search all sites
    sites = await client.graph().get("/sites?search=*")
    print(f"\n--- ALL SITES ({len(sites.get('value', []))}) ---")
    for s in sites.get("value", []):
        print(f" Site: {s.get('name')} | ID: {s.get('id')} | WebUrl: {s.get('webUrl')}")
        sdrives = await client.graph().get(f"/sites/{s['id']}/drives")
        for sd in sdrives.get("value", []):
            print(f"    └─ Drive: {sd.get('name')} | ID: {sd.get('id')} | WebUrl: {sd.get('webUrl')}")

asyncio.run(list_all_sites_and_drives())
