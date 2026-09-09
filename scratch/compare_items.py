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
    
    # 13248 (works)
    f1 = await client.graph().get(f"/drives/{drive_id}/items/01YT4WOQGV6K7EDCQ5WJHJOCZKGUC4HHFO/listItem")
    # 17456 (fails)
    f2 = await client.graph().get(f"/drives/{drive_id}/items/01YT4WOQBC74GSEYD3WRAYAPCX4NGZ5R3R/listItem")
    
    print("--- 13248 (WORKS) ---")
    print(json.dumps(f1, indent=2))
    print("\n--- 17456 (FAILS) ---")
    print(json.dumps(f2, indent=2))

if __name__ == '__main__':
    asyncio.run(main())
