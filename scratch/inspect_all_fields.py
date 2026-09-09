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
    
    # 13248 (has Vessel_x0020_Name_x0020_ set)
    f1 = await client.graph().get(f"/drives/{drive_id}/items/01YT4WOQGV6K7EDCQ5WJHJOCZKGUC4HHFO/listItem/fields")
    # 17456 (empty Vessel)
    f2 = await client.graph().get(f"/drives/{drive_id}/items/01YT4WOQBC74GSEYD3WRAYAPCX4NGZ5R3R/listItem/fields")
    
    print("--- 13248 (HAS VESSEL) ---")
    for k, v in f1.items():
        if not k.startswith('@'):
            print(f"  {k}: {v}")

    print("\n--- 17456 (EMPTY VESSEL) ---")
    for k, v in f2.items():
        if not k.startswith('@'):
            print(f"  {k}: {v}")

if __name__ == '__main__':
    asyncio.run(main())
