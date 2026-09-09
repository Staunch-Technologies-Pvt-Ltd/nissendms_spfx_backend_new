import asyncio
import sys
import os

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client

async def main():
    site_id = 'nissenkaiunsingapore.sharepoint.com,8688e65a-9abf-46ff-bc2c-62a4f3654580,73bdf9ab-0856-42f4-be7b-8fb38ee6d2cc'
    list_id = 'fa4dfba9-0060-4c2a-982a-15ad4a1661fa'

    # Get the full schema for Vessel_x0020_Name_x0020_, Category, and Group
    # to compare what's different
    for col_name in ['Vessel_x0020_Name_x0020_', 'Category', 'Group']:
        print(f"\n=== {col_name} ===")
        cols = await client.graph().get(f"/sites/{site_id}/lists/{list_id}/columns?$filter=name eq '{col_name}'")
        for c in (cols or {}).get('value', []):
            import json
            print(json.dumps(c, indent=2))

    # Also look at the full schema for the two note columns and the vessel note col
    for col_name in ['a57855ae762b489092e6d62b4a1dc5b9', 'g5e02a9aaa484560a09d362f0e15c3fa', 'i62be25c1f7249f48f51efaf91f1f739']:
        print(f"\n=== Note col {col_name} ===")
        cols = await client.graph().get(f"/sites/{site_id}/lists/{list_id}/columns?$filter=name eq '{col_name}'")
        for c in (cols or {}).get('value', []):
            import json
            print(json.dumps(c, indent=2))

    # Compare item 13248 in the OTHER library where vessel works
    site_id2 = 'nissenkaiunsingapore.sharepoint.com,d4cc3b40-789f-439e-8b40-0e75577184c0,6b46659d-cce7-4967-8e27-c9125b951c04'
    list_id2 = 'd1c4c41b-079b-4deb-befa-78e7762c065a'
    for col_name in ['Vessel_x0020_Name_x0020_']:
        print(f"\n=== Site2 {col_name} ===")
        cols = await client.graph().get(f"/sites/{site_id2}/lists/{list_id2}/columns?$filter=name eq '{col_name}'")
        for c in (cols or {}).get('value', []):
            import json
            print(json.dumps(c, indent=2))

if __name__ == '__main__':
    asyncio.run(main())
