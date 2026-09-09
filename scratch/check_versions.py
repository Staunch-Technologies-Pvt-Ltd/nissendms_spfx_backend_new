import asyncio
import sys
import os
import json

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client

async def main():
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    item_id = '01YT4WOQDDX5LOECYRWFGZL47KRXFDDHMP' # MM-33
    
    vers = (await client.graph().get(f'/drives/{drive_id}/items/{item_id}/listItem/versions')).get('value', [])
    print(f'Total versions: {len(vers)}')
    for v in vers:
        vid = v.get('id')
        # get version fields
        v_detail = await client.graph().get(f'/drives/{drive_id}/items/{item_id}/listItem/versions/{vid}/fields')
        vessel = v_detail.get('Vessel_x0020_Name_x0020_')
        print(f"\nVer {vid}: Vessel={vessel}")
        for k, val in v_detail.items():
            if any(w in k.lower() for w in ('vessel', '62be', '5e02', '5785')):
                print(f"   {k}: {val}")

if __name__ == '__main__':
    asyncio.run(main())
