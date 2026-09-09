import asyncio
import sys
import os

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client

async def test():
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    item_id = '01YT4WOQGV6K7EDCQ5WJHJOCZKGUC4HHFO'
    
    item_meta = await client.graph().get(f"/drives/{drive_id}/items/{item_id}?$select=sharepointIds")
    print("item_meta:", item_meta)

asyncio.run(test())
