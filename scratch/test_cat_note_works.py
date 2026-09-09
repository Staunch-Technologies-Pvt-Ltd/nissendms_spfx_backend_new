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
    
    # Find an item where Category was NEVER set so we can test setting it fresh
    # Let's find items with Category=None
    print("Looking for items with Category=None...")
    items_resp = await client.graph().get(
        f"/sites/{site_id}/lists/{list_id}/items?$expand=fields($select=Category,Group,Vessel_x0020_Name_x0020_)&$top=200"
    )
    items = items_resp.get('value', [])
    no_cat_items = [(i['id'], i['fields']) for i in items if not i['fields'].get('Category')]
    print(f"Items with no Category (first 200): {len(no_cat_items)}")
    
    if no_cat_items:
        test_id = no_cat_items[0][0]
        print(f"Test item: {test_id}")
        print(f"Current fields: {no_cat_items[0][1]}")
        
        # Try to set Category via note col using same format as our app
        # Pollution GUID: 4837e546-fc45-4325-ab49-723662a5deed, WssId=54
        print(f"\nPatching item {test_id} with Category=Pollution via note col...")
        cat_note_col = 'a57855ae762b489092e6d62b4a1dc5b9'
        pollution_guid = '4837e546-fc45-4325-ab49-723662a5deed'
        
        try:
            res = await client.graph().patch(
                f"/sites/{site_id}/lists/{list_id}/items/{test_id}/fields",
                json={cat_note_col: f'-1;#Pollution|{pollution_guid}'}
            )
            cat = res.get('Category')
            note = res.get(cat_note_col)
            print(f"Category after patch: {cat}")
            print(f"Category note col: {note!r}")
            if cat:
                print("*** Category note col write WORKS! ***")
            else:
                print("*** Category note col write DOES NOT work either! ***")
        except Exception as e:
            print(f"Error: {e}")
    
    # The KEY question: DOES our backend actually set vessel on items in the OTHER drive
    # (the drive where item 13248 is = Technical/Snow Flower)?
    # Let's look at some of the 3053 vessel-set items' version history
    # to understand how they were originally set.
    print("\n\n=== Version history for item 12885 (has vessel set) ===")
    versions = await client.graph().get(
        f"/sites/{site_id}/lists/{list_id}/items/12885/versions?$top=10&$select=id,lastModifiedBy,lastModifiedDateTime"
    )
    for v in (versions or {}).get('value', []):
        mod = v.get('lastModifiedBy', {})
        user = mod.get('user', mod.get('application', {}))
        print(f"Version {v.get('id')}: {user.get('displayName','?')} - {v.get('lastModifiedDateTime')}")

    # Check very recently created items (those added via our app) - do they have vessel?
    print("\n\n=== Recently modified items (by SharePoint App) - do they have vessel? ===")
    items_resp2 = await client.graph().get(
        f"/sites/{site_id}/lists/{list_id}/items?$expand=fields($select=Category,Group,Vessel_x0020_Name_x0020_,Modified,Editor)&$orderby=Modified desc&$top=10"
    )
    for item in (items_resp2 or {}).get('value', []):
        f = item.get('fields', {})
        print(f"  ID={item['id']}, Modified={f.get('Modified')}, Vessel={f.get('Vessel_x0020_Name_x0020_')}, Cat={bool(f.get('Category'))}")

if __name__ == '__main__':
    asyncio.run(main())
