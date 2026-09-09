import asyncio
import sys
import os

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client

async def test():
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    folder_id = '01YT4WOQG2Q3EKJ5V63ZFINPRXJEDSNOM6' # Pollution folder
    
    children = (await client.graph().get(f'/drives/{drive_id}/items/{folder_id}/children')).get('value', [])
    print(f'Children in Pollution folder: {len(children)}')
    for c in children:
        cid = c['id']
        cname = c['name']
        fields = await client.graph().get(f'/drives/{drive_id}/items/{cid}/listItem/fields')
        print(f"\n--- {cname} ({cid}) ---")
        for k in ('VesselName', 'Vessel_x0020_Name', 'Vessel_x0020_Name_x0020_', 'Category', 'Group', 'Domain', 'Sub_x002d_Category'):
            print(f"  {k}: {fields.get(k)}")

asyncio.run(test())
