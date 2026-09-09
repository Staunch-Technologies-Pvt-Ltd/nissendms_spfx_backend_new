import asyncio
import sys
import os
import httpx

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph.client import graph

async def main():
    client = graph()
    tok_res = client._app.acquire_token_for_client(scopes=['https://nissenkaiunsingapore.sharepoint.com/.default'])
    tok = tok_res['access_token']
    site_url = 'https://nissenkaiunsingapore.sharepoint.com/sites/NKSDocMan'
    headers = {
        'Authorization': f'Bearer {tok}',
        'Accept': 'application/json;odata=verbose',
        'Content-Type': 'application/json;odata=verbose',
    }
    async with httpx.AsyncClient() as http:
        # Get All Documents view fields
        url = f"{site_url}/_api/web/lists(guid'fa4dfba9-0060-4c2a-982a-15ad4a1661fa')/views/getByTitle('All Documents')/ViewFields"
        r = await http.get(url, headers=headers)
        print("View fields status:", r.status_code)
        if r.status_code == 200:
            data = r.json()
            items = data.get('d', {}).get('Items', {}).get('results', [])
            print("View fields in All Documents:")
            for item in items:
                print("  ", item)
        else:
            print("Err:", r.text[:300])

        # Also get list item 13248 (MM-25) via SharePoint REST API to see ALL its fields!
        url_item = f"{site_url}/_api/web/lists(guid'fa4dfba9-0060-4c2a-982a-15ad4a1661fa')/items(13248)"
        r_item = await http.get(url_item, headers=headers)
        print("\nItem 13248 REST status:", r_item.status_code)
        if r_item.status_code == 200:
            d = r_item.json().get('d', {})
            print("Item 13248 fields matching vessel or manual or category:")
            for k, v in d.items():
                if any(w in k.lower() for w in ('vessel', 'category', 'group', 'manual', 'ship')):
                    print(f"  {k}: {v}")

if __name__ == '__main__':
    asyncio.run(main())
