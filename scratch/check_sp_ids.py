import asyncio, sys, os, json
sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')
from app.graph import client as gc

async def main():
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    item_id = '01YT4WOQBC74GSEYD3WRAYAPCX4NGZ5R3R'  # item 17456

    # Check what sharepointIds actually returns
    meta = await gc.graph().get(f"/drives/{drive_id}/items/{item_id}?$select=sharepointIds")
    print("sharepointIds:", json.dumps(meta.get('sharepointIds'), indent=2))

    # siteUrl field?
    sp_ids = meta.get('sharepointIds') or {}
    print("siteId:", sp_ids.get('siteId'))
    print("listId:", sp_ids.get('listId'))
    print("listItemId:", sp_ids.get('listItemId'))
    print("siteUrl:", sp_ids.get('siteUrl'))
    print("webId:", sp_ids.get('webId'))

asyncio.run(main())
