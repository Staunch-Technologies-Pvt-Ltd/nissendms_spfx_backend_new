import asyncio
import httpx
from app.config import get_settings

async def test_token():
    settings = get_settings()
    token_url = f"{settings.graph_authority}/{settings.azure_tenant_id}/oauth2/v2.0/token"
    print(f"Token URL: {token_url}")
    print(f"Client ID: {settings.graph_client_id}")
    print(f"Testing Azure credentials...")
    
    async with httpx.AsyncClient(verify=settings.graph_verify_ssl) as client:
        token_resp = await client.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": settings.graph_client_id,
                "client_secret": settings.graph_client_secret,
                "scope": settings.graph_scope,
            },
        )
        print(f"Token Status: {token_resp.status_code}")
        if token_resp.status_code == 200:
            token_data = token_resp.json()
            print(f"✓ Token acquired successfully")
            print(f"  Token expires in: {token_data.get('expires_in')} seconds")
        else:
            print(f"✗ Token request failed: {token_resp.text}")

asyncio.run(test_token())
