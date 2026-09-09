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
    
    note_col = 'i62be25c1f7249f48f51efaf91f1f739'
    
    # Read item 4814 fully to understand exactly what its note col looks like
    print("=== Item 4814 full field read ===")
    resp = await client.graph().get(
        f"/sites/{site_id}/lists/{list_id}/items/4814/fields"
    )
    vessel = resp.get('Vessel_x0020_Name_x0020_')
    note = resp.get(note_col)
    cat = resp.get('Category')
    cat_note = resp.get('a57855ae762b489092e6d62b4a1dc5b9')
    grp = resp.get('Group')
    grp_note = resp.get('g5e02a9aaa484560a09d362f0e15c3fa')
    print(f"Vessel: {json.dumps(vessel, default=str)}")
    print(f"Vessel note col ({note_col}): {note!r}")
    print(f"Category: {json.dumps(cat, default=str)}")
    print(f"Category note col: {cat_note!r}")
    print(f"Group: {json.dumps(grp, default=str)}")
    print(f"Group note col: {grp_note!r}")

    # Now test: patch item 17456 to change its vessel
    # But first wipe the note col from item 17456 (it currently has "-1;#Snow Flower|...")
    # to reset state, patch it to empty
    print("\n=== Resetting item 17456 note col to empty ===")
    try:
        res0 = await client.graph().patch(
            f"/sites/{site_id}/lists/{list_id}/items/17456/fields",
            json={note_col: ''}
        )
        print(f"After reset, note col: {res0.get(note_col)!r}")
        print(f"After reset, vessel: {res0.get('Vessel_x0020_Name_x0020_')}")
    except Exception as e:
        print(f"Reset error: {e}")

    # Now try patching item 4814's note col TO MATCH its own format
    # i.e. just "Label|Guid" - but we already tested this. Let's try
    # patching item 4814 again to verify the format
    bow_fighter_guid = 'c7ab5d87-e348-456f-8b89-b37ab70b621c'
    print("\n=== Re-patching item 4814 with Label|Guid format ===")
    try:
        res4814 = await client.graph().patch(
            f"/sites/{site_id}/lists/{list_id}/items/4814/fields",
            json={note_col: f'Bow Fighter|{bow_fighter_guid}'}
        )
        v = res4814.get('Vessel_x0020_Name_x0020_')
        n = res4814.get(note_col)
        print(f"Vessel: {v}")
        print(f"Note col: {n!r}")
    except Exception as e:
        print(f"Error: {e}")

    # And try patching item 4814 with Snow Flower
    snow_flower_guid = '43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'
    print("\n=== Patching item 4814 with Snow Flower ===")
    try:
        res_snow = await client.graph().patch(
            f"/sites/{site_id}/lists/{list_id}/items/4814/fields",
            json={note_col: f'Snow Flower|{snow_flower_guid}'}
        )
        v = res_snow.get('Vessel_x0020_Name_x0020_')
        n = res_snow.get(note_col)
        print(f"Vessel: {v}")
        print(f"Note col: {n!r}")
        if v and 'Snow Flower' in str(v):
            print("*** SUCCESS - Label|Guid format works! ***")
    except Exception as e:
        print(f"Error: {e}")
    
    # Finally restore item 4814 to Bow Fighter
    await client.graph().patch(
        f"/sites/{site_id}/lists/{list_id}/items/4814/fields",
        json={note_col: f'Bow Fighter|{bow_fighter_guid}'}
    )
    print("\nRestored item 4814 to Bow Fighter.")

if __name__ == '__main__':
    asyncio.run(main())
