import asyncio
import sys
import os

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client

async def main():
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    item_id = '01YT4WOQGV6K7EDCQ5WJHJOCZKGUC4HHFO' # 13248
    
    vers = (await client.graph().get(f'/drives/{drive_id}/items/{item_id}/listItem/versions')).get('value', [])
    print(f'Total versions: {len(vers)}')
    for v in vers:
        vid = v.get('id')
        v_fields = await client.graph().get(f'/drives/{drive_id}/items/{item_id}/listItem/versions/{vid}/fields')
        vessel = v_fields.get('Vessel_x0020_Name_x0020_')
        mod = v.get('lastModifiedDateTime')
        modby = (v.get('lastModifiedBy') or {}).get('user', {}).get('displayName') or (v.get('lastModifiedBy') or {}).get('application', {}).get('displayName')
        print(f"Ver {vid} ({mod} by {modby}): Vessel={vessel}")

if __name__ == '__main__':
    asyncio.run(main())
