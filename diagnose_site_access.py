"""Read-only diagnostic: why do sites show 'Unable to load'?
Run from backend/:  .venv\\Scripts\\python diagnose_site_access.py
For each site_configurations row it prints the app's Graph permission (roles),
the drives Graph actually returns for the site, and whether the configured
drive_id is reachable. Makes no changes.
"""
import base64, json, sys
import httpx
from sqlalchemy import text
from app.config import settings
from app.db.base import engine


def token():
    r = httpx.post(
        f"https://login.microsoftonline.com/{settings.azure_tenant_id}/oauth2/v2.0/token",
        data={"client_id": settings.graph_client_id, "client_secret": settings.graph_client_secret,
              "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"},
        timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


tok = token()
p = tok.split(".")[1]; p += "=" * (-len(p) % 4)
print("App roles:", json.loads(base64.urlsafe_b64decode(p)).get("roles"))
H = {"Authorization": f"Bearer {tok}"}
G = "https://graph.microsoft.com/v1.0"

with engine.connect() as c:
    rows = c.execute(text("SELECT site_key, site_id, drive_id FROM site_configurations")).mappings().all()

for r in rows:
    print(f"\n== {r['site_key']}  site_id={r['site_id']}")
    if r["site_id"]:
        x = httpx.get(f"{G}/sites/{r['site_id']}/drives?$select=id,name,driveType", headers=H, timeout=30)
        print("  site drives:", x.status_code,
              [(d["name"], d["id"]) for d in x.json().get("value", [])] if x.status_code == 200 else x.text[:200])
    if r["drive_id"]:
        y = httpx.get(f"{G}/drives/{r['drive_id']}/root/children?$top=1", headers=H, timeout=30)
        print("  configured drive:", y.status_code, "OK" if y.status_code == 200 else y.text[:200])
