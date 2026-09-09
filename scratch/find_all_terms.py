import asyncio
import sys
import os
import json

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client

async def main():
    site_id = 'nissenkaiunsingapore.sharepoint.com,8688e65a-9abf-46ff-bc2c-62a4f3654580,73bdf9ab-0856-42f4-be7b-8fb38ee6d2cc'
    
    # Get all termStore groups and sets
    groups = (await client.graph().get(f"/sites/{site_id}/termStore/groups")).get('value', [])
    for g in groups:
        gid = g.get('id')
        gname = g.get('displayName')
        print(f"\nGroup: {gname} ({gid})")
        sets = (await client.graph().get(f"/sites/{site_id}/termStore/groups/{gid}/sets")).get('value', [])
        for s in sets:
            sid = s.get('id')
            sname = (s.get('localizedNames') or [{}])[0].get('name') or sid
            print(f"  Set: {sname} ({sid})")
            # Look for Ghana Express or Snow Flower in this set
            terms = (await client.graph().get(f"/sites/{site_id}/termStore/groups/{gid}/sets/{sid}/terms")).get('value', [])
            for t in terms:
                tid = t.get('id')
                for l in t.get('labels', []):
                    lname = l.get('name')
                    if lname in ('Ghana Express', 'Snow Flower', 'Snow Flake'):
                        print(f"    -> FOUND TERM: {lname} | id={tid} in Set: {sname} ({sid}) in Group: {gname}")

if __name__ == '__main__':
    asyncio.run(main())
