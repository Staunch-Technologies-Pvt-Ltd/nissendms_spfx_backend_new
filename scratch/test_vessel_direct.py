import asyncio
import sys
import os

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client

async def main():
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    item_id = '01YT4WOQBC74GSEYD3WRAYAPCX4NGZ5R3R' # 17456

    # The standard Graph note-column approach (used for Group/Category) doesn't work for
    # Vessel_x0020_Name_x0020_ on this library. Try writing the taxonomy field
    # as a wssId;#Label|Guid string DIRECTLY to the main column:
    payloads = [
        ("Vessel direct with wssId;#...", {'Vessel_x0020_Name_x0020_': '57;#Snow Flower|43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'}),
        ("Vessel direct with -1;#...", {'Vessel_x0020_Name_x0020_': '-1;#Snow Flower|43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'}),
    ]
    for label, payload in payloads:
        print(f"\n--- {label} ---")
        try:
            res = await client.graph().patch(f"/drives/{drive_id}/items/{item_id}/listItem/fields", json=payload)
            vessel = res.get('Vessel_x0020_Name_x0020_')
            print(f"Vessel: {vessel}")
            if vessel:
                print(f"*** SUCCESS: {label} ***")
        except Exception as e:
            print(f"Error: {e}")

    # Try via list endpoint as well
    site_id = 'nissenkaiunsingapore.sharepoint.com,8688e65a-9abf-46ff-bc2c-62a4f3654580,73bdf9ab-0856-42f4-be7b-8fb38ee6d2cc'
    list_id = 'fa4dfba9-0060-4c2a-982a-15ad4a1661fa'
    list_payloads = [
        ("List endpoint Vessel wssId", {'Vessel_x0020_Name_x0020_': '57;#Snow Flower|43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'}),
        ("List endpoint Vessel -1", {'Vessel_x0020_Name_x0020_': '-1;#Snow Flower|43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'}),
    ]
    for label, payload in list_payloads:
        print(f"\n--- {label} ---")
        try:
            res = await client.graph().patch(f"/sites/{site_id}/lists/{list_id}/items/17456/fields", json=payload)
            vessel = res.get('Vessel_x0020_Name_x0020_')
            print(f"Vessel: {vessel}")
            if vessel:
                print(f"*** SUCCESS: {label} ***")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == '__main__':
    asyncio.run(main())
