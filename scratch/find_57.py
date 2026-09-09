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
    
    # Check 13248
    item1 = await client.graph().get(f"/drives/{drive_id}/items/01YT4WOQGV6K7EDCQ5WJHJOCZKGUC4HHFO/listItem/fields")
    print("All keys on 13248:")
    for k, v in item1.items():
        if not k.startswith('@'):
            print(f"  {k}: {repr(v)[:100]}")
            if "57" in str(v) or "Snow Flower" in str(v):
                print(f"    ^^^ MATCHES SNOW FLOWER / 57: {k} = {v}")

if __name__ == '__main__':
    asyncio.run(main())
