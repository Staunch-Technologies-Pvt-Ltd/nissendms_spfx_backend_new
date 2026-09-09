import asyncio
import sys
import os
import json
import httpx
import msal

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.config import settings

async def main():
    # Try to get a SharePoint-scoped token
    # SP REST API needs: https://{tenant}.sharepoint.com/.default
    sp_scope = "https://nissenkaiunsingapore.sharepoint.com/.default"
    
    app = msal.ConfidentialClientApplication(
        client_id=settings.graph_client_id,
        authority=settings.authority_url,
        client_credential=settings.graph_client_secret,
    )
    
    result = app.acquire_token_for_client(scopes=[sp_scope])
    if 'access_token' not in result:
        print(f"ERROR: Can't get SP token: {result.get('error_description', result.get('error'))}")
        return
    
    sp_token = result['access_token']
    print("Got SP token successfully!")
    
    # Now use SP REST API to set Vessel Name on item 17456
    # ValidateUpdateListItem endpoint
    site_url = "https://nissenkaiunsingapore.sharepoint.com/sites/NKSDocMan"
    list_title = "Shared Documents"
    item_id = "17456"
    
    snow_flower_guid = '43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'
    vessel_field_name = "Vessel Name"  # The DISPLAY name for SP REST API
    
    # SP REST API format for taxonomy fields via ValidateUpdateListItem
    # Uses {termGuid}|{termLabel} format
    validate_payload = {
        "formValues": [
            {
                "FieldName": "Vessel Name",
                "FieldValue": f"-1;#{snow_flower_guid}|Snow Flower"  # SP REST format
            }
        ],
        "bNewDocumentUpdate": False
    }
    
    headers = {
        "Authorization": f"Bearer {sp_token}",
        "Accept": "application/json;odata=verbose",
        "Content-Type": "application/json;odata=verbose",
    }
    
    url = f"{site_url}/_api/web/lists/getByTitle('{list_title}')/items({item_id})/ValidateUpdateListItem()"
    
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=validate_payload, headers=headers)
        print(f"Status: {resp.status_code}")
        print(f"Response: {resp.text[:500]}")

    # Also check: what the Graph API returns for the item now
    from app.graph import client as graph_client
    item_fields = await graph_client.graph().get(
        f"/sites/nissenkaiunsingapore.sharepoint.com,8688e65a-9abf-46ff-bc2c-62a4f3654580,73bdf9ab-0856-42f4-be7b-8fb38ee6d2cc/lists/fa4dfba9-0060-4c2a-982a-15ad4a1661fa/items/17456/fields?$select=Vessel_x0020_Name_x0020_"
    )
    print(f"\nVessel after SP REST call: {item_fields.get('Vessel_x0020_Name_x0020_')}")

if __name__ == '__main__':
    asyncio.run(main())
