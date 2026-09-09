import asyncio
import json
import httpx
import sys, os
sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
from app.graph import client

async def main():
    c = client.graph()
    tok_res = c._app.acquire_token_for_client(scopes=['https://nissenkaiunsingapore.sharepoint.com/.default'])
    tok = tok_res['access_token']
    site_url = 'https://nissenkaiunsingapore.sharepoint.com/sites/NKSDocMan'
    headers = {
        'Authorization': f'Bearer {tok}',
        'Accept': 'application/json;odata=nometadata',
        'Content-Type': 'application/json;odata=nometadata',
        'User-Agent': 'NONISV|SharePointCustom|VesselDMS/1.0',
    }
    async with httpx.AsyncClient() as http:
        # Try getting web title
        r = await http.get(f"{site_url}/_api/web?$select=Title", headers=headers)
        print("GET WEB STATUS:", r.status_code, r.text[:200], r.headers)

if __name__ == '__main__':
    asyncio.run(main())
