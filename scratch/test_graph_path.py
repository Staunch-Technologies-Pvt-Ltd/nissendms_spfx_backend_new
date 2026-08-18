import asyncio
import os
os.chdir('c:/sharepoint spfx/backend')

from app.services.real_backend import RealBackend
from app.graph import drive as gd, client
from urllib.parse import quote

be = RealBackend()

async def test():
    drive_id = await be._drive()
    
    path3 = "Vessel Management/Technical & Crewing/MV Test 08112026/Crewing"
    enc3 = "/".join(quote(p) for p in path3.split("/"))
    
    print("Testing URL with Vessel Management:", f"/drives/{drive_id}/root:/{enc3}:/children")
    try:
        res3 = await client.graph().get(f"/drives/{drive_id}/root:/{enc3}:/children")
        print("RESULT 3 SUCCESS! Found", len(res3.get("value", [])), "items:")
        for item in res3.get("value", []):
            print(" -", item.get("name"), "(size:", item.get("size"), "bytes)")
    except Exception as e:
        print("RESULT 3 FAILED:", e)

asyncio.run(test())
