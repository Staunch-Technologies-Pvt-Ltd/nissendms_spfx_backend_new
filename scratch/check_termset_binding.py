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
    
    col = await client.graph().get(f"/sites/{site_id}/lists/{list_id}/columns/262be25c-1f72-49f4-8f51-efaf91f1f739")
    print("Vessel column details:")
    print(json.dumps(col, indent=2))
    
    # Also check other taxonomy columns like Group and Category
    cols = (await client.graph().get(f"/sites/{site_id}/lists/{list_id}/columns")).get('value', [])
    for c in cols:
        if c.get('name') in ('Group', 'Category', 'Vessel_x0020_Name_x0020_'):
            print(f"\n{c.get('name')}:")
            print("  termSet:", c.get('termSet'))
            print("  taxonomy:", c.get('taxonomy'))

if __name__ == '__main__':
    asyncio.run(main())
