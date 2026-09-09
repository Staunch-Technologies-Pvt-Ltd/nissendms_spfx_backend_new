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
    # Try different SP token scopes and approaches
    scopes_to_try = [
        "https://nissenkaiunsingapore.sharepoint.com/.default",
        "https://nissenkaiunsingapore.sharepoint.com/Sites.ReadWrite.All",
    ]
    
    app = msal.ConfidentialClientApplication(
        client_id=settings.graph_client_id,
        authority=settings.authority_url,
        client_credential=settings.graph_client_secret,
    )
    
    for sp_scope in scopes_to_try:
        print(f"\n=== Testing scope: {sp_scope} ===")
        result = app.acquire_token_for_client(scopes=[sp_scope])
        if 'access_token' not in result:
            print(f"FAILED: {result.get('error_description', result.get('error'))}")
            continue
        
        sp_token = result['access_token']
        print(f"Got token: {sp_token[:50]}...")
        
        # Decode token to see scopes
        import base64
        parts = sp_token.split('.')
        if len(parts) >= 2:
            payload = parts[1]
            payload += '=' * (4 - len(payload) % 4)
            try:
                decoded = json.loads(base64.b64decode(payload))
                print(f"Token 'roles': {decoded.get('roles', [])}")
                print(f"Token 'scp': {decoded.get('scp', '')}")
                print(f"Token 'aud': {decoded.get('aud', '')}")
            except:
                pass
        
        # Try the SP REST API
        site_url = "https://nissenkaiunsingapore.sharepoint.com/sites/NKSDocMan"
        
        headers = {
            "Authorization": f"Bearer {sp_token}",
            "Accept": "application/json;odata=verbose",
            "Content-Type": "application/json;odata=verbose",
        }
        
        # First try a simple GET
        async with httpx.AsyncClient(timeout=30) as http_client:
            resp = await http_client.get(
                f"{site_url}/_api/web/title",
                headers=headers
            )
            print(f"GET /web/title: {resp.status_code}")
            if resp.status_code == 200:
                print(f"  Response: {resp.text[:200]}")

if __name__ == '__main__':
    asyncio.run(main())
