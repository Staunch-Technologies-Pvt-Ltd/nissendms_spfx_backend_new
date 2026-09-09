import asyncio
import sys
import os

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client

async def main():
    site_id = 'nissenkaiunsingapore.sharepoint.com,8688e65a-9abf-46ff-bc2c-62a4f3654580,73bdf9ab-0856-42f4-be7b-8fb38ee6d2cc'
    set_id = '552ae441-6494-4c7e-97c7-825933bb1a80'
    terms = (await client.graph().get(f'/sites/{site_id}/termStore/sets/{set_id}/terms')).get('value', [])
    print(f'Total terms in Vessel DMS set: {len(terms)}')
    for t in terms:
        names = [l.get('name') for l in t.get('labels', [])]
        if any(w in ' '.join(names).lower() for w in ('snow', 'vessel', 'flower', 'ghana', 'belle')):
            print(f"  MATCH: {t.get('id')} {names}")

if __name__ == '__main__':
    asyncio.run(main())
