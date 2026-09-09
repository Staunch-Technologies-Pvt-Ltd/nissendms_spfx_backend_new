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
    
    for name in ('Group', 'g5e02a9aaa484560a09d362f0e15c3fa', 'Category', 'a57855ae762b489092e6d62b4a1dc5b9', 'Vessel_x0020_Name_x0020_', 'i62be25c1f7249f48f51efaf91f1f739'):
        col = await client.graph().get(f"/sites/{site_id}/lists/{list_id}/columns/{name}")
        print(f"\n--- {name} ---")
        print(json.dumps(col, indent=2))

if __name__ == '__main__':
    asyncio.run(main())
