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
    # MM-33 (currently Ghana Express)
    item_id = '01YT4WOQDDX5LOECYRWFGZL47KRXFDDHMP'
    
    # Try patching Snow Flower via note column
    payload = {'i62be25c1f7249f48f51efaf91f1f739': '-1;#Snow Flower|43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'}
    print("Patching MM-33 with Snow Flower...")
    res = await client.graph().patch(f"/drives/{drive_id}/items/{item_id}/listItem/fields", json=payload)
    print("Result Vessel on MM-33:", res.get('Vessel_x0020_Name_x0020_'))

if __name__ == '__main__':
    asyncio.run(main())
