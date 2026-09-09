import asyncio
import sys
import os

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client

async def test():
    site_id = 'nissenkaiunsingapore.sharepoint.com,8688e65a-9abf-46ff-bc2c-62a4f3654580,73bdf9ab-0856-42f4-be7b-8fb38ee6d2cc'
    
    cols = (await client.graph().get(f"/sites/{site_id}/columns")).get('value', [])
    print(f"Total site columns: {len(cols)}")
    for c in cols:
        d = c.get('displayName') or ''
        n = c.get('name') or ''
        if any(w in d.lower() or w in n.lower() for w in ('vessel', 'manual', 'ship')):
            print(f"  Site col: name={n!r} | disp={d!r} | id={c.get('id')}")

if __name__ == '__main__':
    asyncio.run(test())
