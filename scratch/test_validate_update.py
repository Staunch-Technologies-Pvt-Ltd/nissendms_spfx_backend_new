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
    item_id = '17456' # list item id
    
    # Try Graph validateUpdateListItem
    url = f"/sites/{site_id}/lists/{list_id}/items/{item_id}/validateUpdateListItem"
    
    # Try test A: fieldName = "Vessel_x0020_Name_x0020_", fieldValue = "Snow Flower"
    body_a = {
        "formValues": [
            {"fieldName": "Vessel_x0020_Name_x0020_", "fieldValue": "Snow Flower"}
        ],
        "bNewDocumentUpdate": True
    }
    print("Testing validateUpdateListItem A...")
    try:
        res_a = await client.graph().post(url, json=body_a)
        print("Res A:", json.dumps(res_a, indent=2))
    except Exception as e:
        print("Err A:", e)

    # Try test B: fieldName = "Vessel Name", fieldValue = "Snow Flower"
    body_b = {
        "formValues": [
            {"fieldName": "Vessel Name", "fieldValue": "Snow Flower"}
        ],
        "bNewDocumentUpdate": True
    }
    print("\nTesting validateUpdateListItem B...")
    try:
        res_b = await client.graph().post(url, json=body_b)
        print("Res B:", json.dumps(res_b, indent=2))
    except Exception as e:
        print("Err B:", e)

    # Try test C: with TermGuid: "Snow Flower|43a2ac39-b9ba-40cb-8e28-ff60e4ed8658"
    body_c = {
        "formValues": [
            {"fieldName": "Vessel_x0020_Name_x0020_", "fieldValue": "Snow Flower|43a2ac39-b9ba-40cb-8e28-ff60e4ed8658"}
        ],
        "bNewDocumentUpdate": True
    }
    print("\nTesting validateUpdateListItem C...")
    try:
        res_c = await client.graph().post(url, json=body_c)
        print("Res C:", json.dumps(res_c, indent=2))
    except Exception as e:
        print("Err C:", e)

    # Try test D: fieldName = "Vessel Name", fieldValue with GUID
    body_d = {
        "formValues": [
            {"fieldName": "Vessel Name", "fieldValue": "Snow Flower|43a2ac39-b9ba-40cb-8e28-ff60e4ed8658"}
        ],
        "bNewDocumentUpdate": True
    }
    print("\nTesting validateUpdateListItem D...")
    try:
        res_d = await client.graph().post(url, json=body_d)
        print("Res D:", json.dumps(res_d, indent=2))
    except Exception as e:
        print("Err D:", e)

    # Check fields of 17456 now
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    fields = await client.graph().get(f"/drives/{drive_id}/items/01YT4WOQBC74GSEYD3WRAYAPCX4NGZ5R3R/listItem/fields")
    print("\nFields of 17456 after validateUpdateListItem:")
    for k in ('VesselName', 'Vessel_x0020_Name', 'Vessel_x0020_Name_x0020_', 'Category', 'Group'):
        print(f"  {k}: {fields.get(k)}")

if __name__ == '__main__':
    asyncio.run(main())
