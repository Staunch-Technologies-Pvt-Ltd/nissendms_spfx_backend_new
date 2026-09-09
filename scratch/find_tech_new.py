import asyncio
import sys
import os

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client

async def main():
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    
    # Check root children
    root = await client.graph().get(f"/drives/{drive_id}/root/children")
    print("Root children:")
    for c in root.get('value', []):
        if 'technical' in c.get('name', '').lower() or 'crewing' in c.get('name', '').lower():
            print(f"  {c.get('name')} (id: {c.get('id')})")
            # List children of this folder
            sub = await client.graph().get(f"/drives/{drive_id}/items/{c.get('id')}/children")
            for sc in sub.get('value', []):
                if 'snow' in sc.get('name', '').lower():
                    print(f"    Sub: {sc.get('name')} (id: {sc.get('id')})")
                    # List children
                    grand = await client.graph().get(f"/drives/{drive_id}/items/{sc.get('id')}/children")
                    for gc in grand.get('value', []):
                        print(f"      Grand: {gc.get('name')} (id: {gc.get('id')})")

if __name__ == '__main__':
    asyncio.run(main())
