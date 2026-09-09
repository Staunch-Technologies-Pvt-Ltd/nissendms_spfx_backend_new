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
    
    snow_flower_guid = '43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'
    note_col = 'i62be25c1f7249f48f51efaf91f1f739'
    
    # The CORRECT format discovered from item 4814 (Bow Fighter):
    # "Label|TermGuid" — NO WssId;# prefix!
    print("=== Patching item 17456 with correct format: Label|TermGuid ===")
    payload = {note_col: f'Snow Flower|{snow_flower_guid}'}
    try:
        res = await client.graph().patch(
            f"/sites/{site_id}/lists/{list_id}/items/17456/fields",
            json=payload
        )
        vessel = res.get('Vessel_x0020_Name_x0020_')
        note_val = res.get(note_col)
        print(f"Vessel after patch: {vessel}")
        print(f"Note col after patch: {note_val}")
        if vessel:
            print("*** SUCCESS! ***")
    except Exception as e:
        print(f"Error: {e}")

    # Also check the drive endpoint
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    item_drive_id = '01YT4WOQBC74GSEYD3WRAYAPCX4NGZ5R3R'
    
    print("\n=== Patching via Drive endpoint ===")
    payload2 = {note_col: f'Snow Flower|{snow_flower_guid}'}
    try:
        res2 = await client.graph().patch(
            f"/drives/{drive_id}/items/{item_drive_id}/listItem/fields",
            json=payload2
        )
        vessel2 = res2.get('Vessel_x0020_Name_x0020_')
        print(f"Vessel after patch: {vessel2}")
        if vessel2:
            print("*** SUCCESS via drive endpoint! ***")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == '__main__':
    asyncio.run(main())
