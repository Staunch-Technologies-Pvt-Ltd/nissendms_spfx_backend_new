import asyncio
import httpx
from app.config import get_settings

async def test_user_lookup():
    settings = get_settings()
    token_url = f"{settings.graph_authority}/{settings.azure_tenant_id}/oauth2/v2.0/token"
    
    # Get token
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
    
    if token_resp.status_code != 200:
        print("Failed to get token")
        return
    
    app_token = token_resp.json().get("access_token", "")
    print("✓ Got access token")
    
    # Test user lookup
    email = "spe.admin@sg-nissenkaiun.com"
    print(f"\nLooking up user: {email}")
    
    async with httpx.AsyncClient(verify=settings.graph_verify_ssl) as client:
        user_resp = await client.get(
            f"{settings.graph_base_url}/users/{email}",
            headers={"Authorization": f"Bearer {app_token}"},
        )
    
    print(f"User lookup status: {user_resp.status_code}")
    if user_resp.status_code == 200:
        user_data = user_resp.json()
        print(f"✓ User found:")
        print(f"  Display Name: {user_data.get('displayName')}")
        print(f"  Mail: {user_data.get('mail')}")
        print(f"  UPN: {user_data.get('userPrincipalName')}")
    else:
        print(f"✗ User lookup failed: {user_resp.text}")

asyncio.run(test_user_lookup())
