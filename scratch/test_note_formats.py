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
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    item_id = '01YT4WOQBC74GSEYD3WRAYAPCX4NGZ5R3R' # 17456
    
    # Let's test various payload formats on the note field:
    # Format 1: "Snow Flower|43a2ac39-b9ba-40cb-8e28-ff60e4ed8658" (no -1;#)
    # Format 2: "-1;#Snow Flower|43a2ac39-b9ba-40cb-8e28-ff60e4ed8658;" (with trailing ;)
    # Format 3: "57;#Snow Flower|43a2ac39-b9ba-40cb-8e28-ff60e4ed8658;"
    # Format 4: via /sites/.../lists/.../items/17456/fields
    
    attempts = [
        ("No prefix", {"i62be25c1f7249f48f51efaf91f1f739": "Snow Flower|43a2ac39-b9ba-40cb-8e28-ff60e4ed8658"}),
        ("With semicolon", {"i62be25c1f7249f48f51efaf91f1f739": "-1;#Snow Flower|43a2ac39-b9ba-40cb-8e28-ff60e4ed8658;"}),
        ("WssId with semicolon", {"i62be25c1f7249f48f51efaf91f1f739": "57;#Snow Flower|43a2ac39-b9ba-40cb-8e28-ff60e4ed8658;"}),
        ("List endpoint", {"i62be25c1f7249f48f51efaf91f1f739": "-1;#Snow Flower|43a2ac39-b9ba-40cb-8e28-ff60e4ed8658"}),
    ]
    
    for label, payload in attempts:
        print(f"\n--- Testing {label} ---")
        try:
            if label == "List endpoint":
                res = await client.graph().patch(f"/sites/{site_id}/lists/{list_id}/items/17456/fields", json=payload)
            else:
                res = await client.graph().patch(f"/drives/{drive_id}/items/{item_id}/listItem/fields", json=payload)
            vessel = res.get('Vessel_x0020_Name_x0020_')
            print(f"Vessel result: {vessel}")
            if vessel:
                print("SUCCESS with", label, "!")
                break
        except Exception as e:
            print("Error:", e)

if __name__ == '__main__':
    asyncio.run(main())
