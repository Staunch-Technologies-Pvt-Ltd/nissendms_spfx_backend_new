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
    
    payloads = [
        ("String label", {'Vessel_x0020_Name_x0020_': 'Snow Flower'}),
        ("Dict label+guid", {'Vessel_x0020_Name_x0020_': {'Label': 'Snow Flower', 'TermGuid': '43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'}}),
        ("Dict guid only", {'Vessel_x0020_Name_x0020_': {'TermGuid': '43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'}}),
        ("Dict WssId only", {'Vessel_x0020_Name_x0020_': {'WssId': 57}}),
        ("Vessel_x0020_Name_x0020_Id int", {'Vessel_x0020_Name_x0020_Id': 57}),
        ("Vessel_x0020_Name_x0020_LookupId int", {'Vessel_x0020_Name_x0020_LookupId': 57}),
        ("Vessel_x0020_NameId int", {'Vessel_x0020_NameId': 57}),
        ("VesselNameId int", {'VesselNameId': 57}),
        ("VesselName string", {'VesselName': 'Snow Flower'}),
        ("Vessel_x0020_Name string", {'Vessel_x0020_Name': 'Snow Flower'}),
        ("Vessel_x0020_Name dict", {'Vessel_x0020_Name': {'Label': 'Snow Flower', 'TermGuid': '43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'}}),
        ("Vessel Name string", {'Vessel Name': 'Snow Flower'}),
    ]
    
    for label, payload in payloads:
        try:
            res = await client.graph().patch(f"/drives/{drive_id}/items/{item_id}/listItem/fields", json=payload)
            v = res.get('Vessel_x0020_Name_x0020_') or res.get('Vessel_x0020_Name') or res.get('VesselName')
            print(f"[OK] {label}: vessel={v}")
            if v:
                print(f"*** FOUND WORKING PAYLOAD: {label} -> {payload} ***")
                return
        except Exception as e:
            # print first 80 chars of error
            msg = str(e).split('\n')[0][:80]
            print(f"[FAIL] {label}: {msg}")

if __name__ == '__main__':
    asyncio.run(main())
