import asyncio
import os
os.chdir('c:/sharepoint spfx/backend')

from app.services.real_backend import RealBackend
from app.graph import client

be = RealBackend()

async def list_root_drives():
    site_id = "nissenkaiunsingapore.sharepoint.com,5c210f40-898e-4787-bd56-b49c7be3670b,32925125-47f4-41f3-94c8-873c4b053bc2"
    drives = await client.graph().get(f"/sites/{site_id}/drives")
    print("--- DRIVES ON ROOT SITE ---")
    for d in drives.get("value", []):
        print(f" - Name: {d.get('name')} | ID: {d.get('id')} | Type: {d.get('driveType')} | WebUrl: {d.get('webUrl')}")

asyncio.run(list_root_drives())
