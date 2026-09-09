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

    # Check all columns in the list and find Vessel, Category, Group schemas
    cols_resp = await client.graph().get(f"/sites/{site_id}/lists/{list_id}/columns?$top=200")
    cols = cols_resp.get('value', [])
    for c in cols:
        name = c.get('name','')
        disp = c.get('displayName','')
        if any(x in name.lower() for x in ['vessel', 'category', 'group']) or \
           any(x in disp.lower() for x in ['vessel', 'category', 'group']):
            print(json.dumps({
                'name': c.get('name'),
                'displayName': c.get('displayName'),
                'id': c.get('id'),
                'hidden': c.get('hidden'),
                'readOnly': c.get('readOnly'),
                'type_keys': [k for k in c.keys() if k not in ['name','displayName','id','hidden','readOnly','columnGroup','description','enforceUniqueValues','indexed','required']],
            }, indent=2))

    # Now check the note field for Vessel
    print("\n\n=== VESSEL NOTE COL ===")
    for c in cols:
        name = c.get('name','')
        if name == 'i62be25c1f7249f48f51efaf91f1f739':
            print(json.dumps(c, indent=2))

    # Get raw field data for an item that HAS vessel set (if any)
    # First find an item with vessel set
    items_resp = await client.graph().get(f"/sites/{site_id}/lists/{list_id}/items?$select=id,fields&$expand=fields($select=Vessel_x0020_Name_x0020_,i62be25c1f7249f48f51efaf91f1f739)&$top=20")
    items = (items_resp or {}).get('value', [])
    for item in items:
        fields = item.get('fields', {})
        vessel = fields.get('Vessel_x0020_Name_x0020_')
        note = fields.get('i62be25c1f7249f48f51efaf91f1f739')
        if vessel:
            print(f"\nItem {item.get('id')} has vessel: {vessel}")
            print(f"Note col value: {note}")

if __name__ == '__main__':
    asyncio.run(main())
