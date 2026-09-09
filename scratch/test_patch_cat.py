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
    
    # Check current Category on MM-33
    f_before = await client.graph().get(f"/drives/{drive_id}/items/{item_id}/listItem/fields")
    print("MM-33 Category BEFORE:", f_before.get('Category'))

    # Try patching Category to 'Pollution'
    # 'Pollution|4837e546-fc45-4325-ab49-723662a5deed'
    payload = {'a57855ae762b489092e6d62b4a1dc5b9': '-1;#Pollution|4837e546-fc45-4325-ab49-723662a5deed'}
    print("Patching MM-33 Category to Pollution...")
    res = await client.graph().patch(f"/drives/{drive_id}/items/{item_id}/listItem/fields", json=payload)
    print("MM-33 Category AFTER:", res.get('Category'))

if __name__ == '__main__':
    asyncio.run(main())
