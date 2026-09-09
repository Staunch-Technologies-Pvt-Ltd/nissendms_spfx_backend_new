import asyncio
import sys
import os
import json

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client

async def main():
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    item_id = '01YT4WOQBC74GSEYD3WRAYAPCX4NGZ5R3R' # 17456

    # Test 1: Patch with note column directly
    payload1 = {'i62be25c1f7249f48f51efaf91f1f739': '-1;#Snow Flower|43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'}
    print("Testing payload1 (note field):", payload1)
    res1 = await client.graph().patch(f"/drives/{drive_id}/items/{item_id}/listItem/fields", json=payload1)
    print("res1:", json.dumps(res1, indent=2))

    # Test 2: What about WssId 57? (format: 57;#Snow Flower|43a2ac39-b9ba-40cb-8e28-ff60e4ed8658)
    payload2 = {'i62be25c1f7249f48f51efaf91f1f739': '57;#Snow Flower|43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'}
    print("\nTesting payload2 (with WssId):", payload2)
    res2 = await client.graph().patch(f"/drives/{drive_id}/items/{item_id}/listItem/fields", json=payload2)
    print("res2:", json.dumps(res2, indent=2))

    # Test 3: What if we patch Vessel_x0020_Name_x0020_?
    try:
        payload3 = {'Vessel_x0020_Name_x0020_': '-1;#Snow Flower|43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'}
        print("\nTesting payload3 (Vessel_x0020_Name_x0020_):", payload3)
        res3 = await client.graph().patch(f"/drives/{drive_id}/items/{item_id}/listItem/fields", json=payload3)
        print("res3:", json.dumps(res3, indent=2))
    except Exception as e:
        print("res3 err:", e)

if __name__ == '__main__':
    asyncio.run(main())
