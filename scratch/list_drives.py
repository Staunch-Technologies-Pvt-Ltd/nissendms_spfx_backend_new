import asyncio
import os
os.chdir('c:/sharepoint spfx/backend')

from app.services.real_backend import RealBackend
from app.graph import client

be = RealBackend()

async def list_all_drives():
    # 1. Search site for nissenkaiunsingapore
    site = await client.graph().get("/sites/nissenkaiunsingapore.sharepoint.com")
    print("Main Site ID:", site.get("id"))
    
    drives = await client.graph().get(f"/sites/{site['id']}/drives")
    print("\n--- DRIVES ON MAIN SITE ---")
    for d in drives.get("value", []):
        print(f" - Drive Name: {d.get('name')} | Drive ID: {d.get('id')} | Type: {d.get('driveType')}")
        
    # Check Communication site
    try:
        comm_site = await client.graph().get("/sites/nissenkaiunsingapore.sharepoint.com:/sites/CommunicationSite")
        print("\nComm Site ID:", comm_site.get("id"))
        cdrives = await client.graph().get(f"/sites/{comm_site['id']}/drives")
        print("\n--- DRIVES ON COMM SITE ---")
        for d in cdrives.get("value", []):
            print(f" - Drive Name: {d.get('name')} | Drive ID: {d.get('id')} | Type: {d.get('driveType')}")
    except Exception as e:
        print("Comm site error:", e)

asyncio.run(list_all_drives())
