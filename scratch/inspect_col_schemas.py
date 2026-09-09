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
    
    cols = (await client.graph().get(f"/sites/{site_id}/lists/{list_id}/columns?expand=hidden")).get('value', [])
    for c in cols:
        d = c.get('displayName') or ''
        n = c.get('name') or ''
        if 'vessel' in d.lower() or 'vessel' in n.lower():
            print(f"\n--- Column: {n} ({d}) ---")
            print(json.dumps(c, indent=2))

if __name__ == '__main__':
    asyncio.run(main())
