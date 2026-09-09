import asyncio
import sys
import os

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client

async def main():
    sites = (await client.graph().get('/sites?search=NKSDocMan')).get('value', [])
    for s in sites:
        print(f"SITE: {s.get('displayName')} | {s.get('id')}")
        drives = (await client.graph().get(f"/sites/{s['id']}/drives")).get('value', [])
        for d in drives:
            print(f"   DRIVE: {d.get('name')} | {d.get('id')}")
            # Search for Pollution in this drive
            try:
                res = await client.graph().get(f"/drives/{d['id']}/root/search(q='Pollution')")
                vals = res.get('value', [])
                for v in vals:
                    if v.get('folder'):
                        p = (v.get('parentReference') or {}).get('path', '')
                        print(f"      FOUND FOLDER: {v.get('name')} (id: {v.get('id')}) in path: {p}")
            except Exception as e:
                print("      search err:", e)

if __name__ == '__main__':
    asyncio.run(main())
