import asyncio
import sys
import os
import json
import httpx

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

# Load the user's access token from the backend's token cache
# (normally passed as x-graph-access-token header)
# For testing, we need to get a delegated token
# Let's first see if calling SP REST API with the Graph token works at all

from app.graph import client as graph_client

async def test_via_sp_rest_with_graph_token():
    """Test using the SharePoint REST API with the Graph API token (may not work but worth trying)"""
    graph_token = graph_client.graph()._token()
    
    site_url = "https://nissenkaiunsingapore.sharepoint.com/sites/NKSDocMan"
    snow_guid = '43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'
    
    headers = {
        "Authorization": f"Bearer {graph_token}",
        "Accept": "application/json;odata=verbose",
        "Content-Type": "application/json;odata=verbose",
    }
    
    # Try using the Graph token to call SP REST API
    # Note: this is the same token we use for Graph - but SP REST uses a different audience
    async with httpx.AsyncClient(timeout=30) as http_client:
        resp = await http_client.get(f"{site_url}/_api/web/title", headers=headers)
        print(f"SP REST with Graph token: {resp.status_code}")
        print(f"Response: {resp.text[:200]}")


async def test_via_graph_proxy():
    """Test using the Graph API to proxy SP REST calls"""
    # Microsoft Graph supports calling SP REST via:
    # GET /sites/{site-id}/_api/web
    # This is a Graph proxy that uses the Graph token but calls SP REST
    site_id = 'nissenkaiunsingapore.sharepoint.com,8688e65a-9abf-46ff-bc2c-62a4f3654580,73bdf9ab-0856-42f4-be7b-8fb38ee6d2cc'
    list_id = 'fa4dfba9-0060-4c2a-982a-15ad4a1661fa'
    
    snow_guid = '43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'
    
    # The Graph API for list items ALSO has a beta endpoint for taxonomy
    # Let me try: PATCH /beta/sites/.../lists/.../items/{id}/fields
    # which might handle taxonomy differently
    try:
        import httpx
        graph_token = graph_client.graph()._token()
        headers = {
            "Authorization": f"Bearer {graph_token}",
            "Content-Type": "application/json",
        }
        
        # Try beta endpoint
        async with httpx.AsyncClient(timeout=30) as http_client:
            resp = await http_client.patch(
                f"https://graph.microsoft.com/beta/sites/{site_id}/lists/{list_id}/items/17456/fields",
                json={"Vessel_x0020_Name_x0020_": f"Snow Flower"},
                headers=headers
            )
            print(f"\nBeta endpoint with plain string: {resp.status_code}")
            if resp.status_code < 400:
                data = resp.json()
                print(f"Vessel: {data.get('Vessel_x0020_Name_x0020_')}")
    except Exception as e:
        print(f"Beta endpoint error: {e}")


async def main():
    await test_via_sp_rest_with_graph_token()
    await test_via_graph_proxy()
    
    # KEY INSIGHT: Instead of writing to the broken note col, 
    # let's see what the DRIVE-based approach returns for the item with vessel set
    # vs the item without vessel set - particularly look at the sharepointIds
    print("\n\n=== Drive item info for item 12885 (has vessel) ===")
    # Find the drive item ID for list item 12885
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    
    # Get item by list ID
    site_id = 'nissenkaiunsingapore.sharepoint.com,8688e65a-9abf-46ff-bc2c-62a4f3654580,73bdf9ab-0856-42f4-be7b-8fb38ee6d2cc'
    list_id = 'fa4dfba9-0060-4c2a-982a-15ad4a1661fa'
    
    item = await graph_client.graph().get(
        f"/sites/{site_id}/lists/{list_id}/items/12885?$expand=driveItem($select=id,name,parentReference)"
    )
    drive_item = item.get('driveItem', {})
    print(f"DriveItem ID: {drive_item.get('id')}")
    print(f"DriveItem name: {drive_item.get('name')}")
    
    if drive_item.get('id'):
        drive_item_id = drive_item['id']
        # Try patching vessel via note column on the DRIVE endpoint
        # for item 12885 that already HAS vessel set
        # This won't break anything since it's already Snow Flower
        print(f"\nPatching item 12885 via drive endpoint with note col...")
        try:
            res = await graph_client.graph().patch(
                f"/drives/{drive_id}/items/{drive_item_id}/listItem/fields",
                json={'i62be25c1f7249f48f51efaf91f1f739': '-1;#Snow Flower|43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'}
            )
            print(f"Vessel: {res.get('Vessel_x0020_Name_x0020_')}")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == '__main__':
    asyncio.run(main())
