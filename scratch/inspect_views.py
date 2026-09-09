import asyncio
import sys
import os
import json

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client

async def test():
    site_id = 'nissenkaiunsingapore.sharepoint.com,8688e65a-9abf-46ff-bc2c-62a4f3654580,73bdf9ab-0856-42f4-be7b-8fb38ee6d2cc'
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    list_id = 'fa4dfba9-0060-4c2a-982a-15ad4a1661fa'

    # Check views
    try:
        views = (await client.graph().get(f"/sites/{site_id}/lists/{list_id}/views")).get('value', [])
        print("VIEWS:")
        for v in views:
            print(f"  View: {v.get('title')} | id: {v.get('id')} | default: {v.get('defaultView')}")
            # View columns
            view_cols = (await client.graph().get(f"/sites/{site_id}/lists/{list_id}/views/{v.get('id')}/columns")).get('value', [])
            print(f"    View columns: {[c.get('name') for c in view_cols]}")
    except Exception as e:
        print("Views error:", e)

    # Let's check ALL columns with any metadata
    cols = (await client.graph().get(f"/sites/{site_id}/lists/{list_id}/columns?expand=hidden")).get('value', [])
    print(f"\nALL Columns in List (total {len(cols)}):")
    for c in cols:
        d = c.get('displayName') or ''
        n = c.get('name') or ''
        if any(w in d.lower() or w in n.lower() for w in ('vessel', 'category', 'group', 'manual', 'ship', 'draw')):
            print(f"  name={n!r} | disp={d!r} | hidden={c.get('hidden')} | readOnly={c.get('readOnly')} | taxonomy={c.get('taxonomy')}")

if __name__ == '__main__':
    asyncio.run(test())
