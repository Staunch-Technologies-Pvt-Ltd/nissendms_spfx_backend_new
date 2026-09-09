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
    
    snow_guid = '43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'
    
    # Key insight: item 12885 has Vessel set but note col is None.
    # This means vessel was set DIRECTLY — not via the note column!
    # Let's try all remaining approaches:
    
    payloads = [
        # Object with WssId=57 (known valid WssId for Snow Flower in this list)
        ("Object WssId=57", {"Vessel_x0020_Name_x0020_": {"Label": "Snow Flower", "TermGuid": snow_guid, "WssId": 57}}),
        # JSON string that Graph might interpret
        ("JSON string", {"Vessel_x0020_Name_x0020_": json.dumps({"Label": "Snow Flower", "TermGuid": snow_guid, "WssId": 57})}),
        # Try updating via 'fields' property in the listItem PATCH (body wrapping)
    ]
    
    for label, payload in payloads:
        print(f"\n--- {label} ---")
        try:
            res = await client.graph().patch(
                f"/sites/{site_id}/lists/{list_id}/items/17456/fields",
                json=payload
            )
            v = res.get('Vessel_x0020_Name_x0020_')
            print(f"Vessel: {v}")
            if v:
                print(f"*** SUCCESS! ***")
        except Exception as e:
            print(f"Error: {e}")

    # Try PATCH on the listItem itself (not /fields)
    print("\n--- PATCH listItem with fields wrapper ---")
    try:
        res = await client.graph().patch(
            f"/sites/{site_id}/lists/{list_id}/items/17456",
            json={"fields": {"Vessel_x0020_Name_x0020_": {"Label": "Snow Flower", "TermGuid": snow_guid, "WssId": 57}}}
        )
        print(f"Response: {res}")
    except Exception as e:
        print(f"Error: {e}")

    # Check what Group/Category note col ACTUALLY looks like for item 17456
    # to understand HOW category and group got set
    print("\n\n=== Check Group/Category note col for item 17456 ===")
    resp = await client.graph().get(
        f"/sites/{site_id}/lists/{list_id}/items/17456/fields"
    )
    print(f"Category: {resp.get('Category')}")
    print(f"Category note col: {resp.get('a57855ae762b489092e6d62b4a1dc5b9')!r}")
    print(f"Group: {resp.get('Group')}")
    print(f"Group note col: {resp.get('g5e02a9aaa484560a09d362f0e15c3fa')!r}")
    print(f"Vessel: {resp.get('Vessel_x0020_Name_x0020_')}")
    print(f"Vessel note col: {resp.get('i62be25c1f7249f48f51efaf91f1f739')!r}")

    # Also check 12885 for group/category note cols
    print("\n=== Check Group/Category note col for item 12885 (has vessel) ===")
    resp2 = await client.graph().get(
        f"/sites/{site_id}/lists/{list_id}/items/12885/fields"
    )
    print(f"Category: {resp2.get('Category')}")
    print(f"Category note col: {resp2.get('a57855ae762b489092e6d62b4a1dc5b9')!r}")
    print(f"Group: {resp2.get('Group')}")
    print(f"Group note col: {resp2.get('g5e02a9aaa484560a09d362f0e15c3fa')!r}")
    print(f"Vessel: {resp2.get('Vessel_x0020_Name_x0020_')}")
    print(f"Vessel note col: {resp2.get('i62be25c1f7249f48f51efaf91f1f739')!r}")

    # Version history for item 17456 - who set Category/Group?
    print("\n=== Version history for item 17456 - checking who set category/group ===")
    versions = await client.graph().get(
        f"/sites/{site_id}/lists/{list_id}/items/17456/versions?$top=5&$select=id,lastModifiedBy,fields"
    )
    for v in (versions or {}).get('value', []):
        mod = v.get('lastModifiedBy', {})
        user = mod.get('user', mod.get('application', {}))
        print(f"Version {v.get('id')}: {user.get('displayName','?')} - {v.get('fields', {}).get('Category')}")

if __name__ == '__main__':
    asyncio.run(main())
