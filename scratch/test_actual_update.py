import asyncio
import sys
import os
import json

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client, drive as gd

async def test():
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    item_id = '01YT4WOQGV6K7EDCQ5WJHJOCZKGUC4HHFO'

    payload = {
        "Department": "Technical and Crewing",
        "VesselName": "Snow Flower",
        "Group": "Manuals",
        "Category": "Pollution",
    }
    print(f"Calling update_file_columns on {item_id}...")
    res = await gd.update_file_columns(drive_id, item_id, payload)
    print("Result:")
    print(json.dumps(res, indent=2))

if __name__ == '__main__':
    asyncio.run(test())
