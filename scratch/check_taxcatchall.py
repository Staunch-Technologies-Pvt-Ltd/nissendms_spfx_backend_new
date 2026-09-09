import asyncio
import sys
import os

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client

async def main():
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    
    r1 = await client.graph().get(f"/drives/{drive_id}/items/01YT4WOQGV6K7EDCQ5WJHJOCZKGUC4HHFO/listItem/fields?$select=TaxCatchAll")
    r2 = await client.graph().get(f"/drives/{drive_id}/items/01YT4WOQBC74GSEYD3WRAYAPCX4NGZ5R3R/listItem/fields?$select=TaxCatchAll")
    
    print("13248 TaxCatchAll:", r1.get('TaxCatchAll'))
    print("17456 TaxCatchAll:", r2.get('TaxCatchAll'))

if __name__ == '__main__':
    asyncio.run(main())
