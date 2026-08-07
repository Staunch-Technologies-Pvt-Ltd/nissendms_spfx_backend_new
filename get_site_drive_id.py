"""
Get the SharePoint site's Documents library drive ID.

The app registration needs ONE of these Graph permissions:
  - Sites.Read.All  (read-only)
  - Sites.ReadWrite.All  (read-write)
  - Files.ReadWrite.All  (also works)

To grant it:
  Azure Portal → App registrations → your app → API permissions
  → Add a permission → Microsoft Graph → Application permissions
  → Sites.ReadWrite.All → Grant admin consent

Usage: python get_site_drive_id.py
"""
import requests

TENANT_ID = "8e7d453e-ffcb-4df6-8f2f-6c7ca1b0d457"
CLIENT_ID = "c5905e40-0090-43e4-9ad6-76a7858f1815"
CLIENT_SECRET = "qZP8Q~63L.BdtlSeTBU8EBUk2zxDaB2iKRTugbNw"
SITE_HOSTNAME = "kamalnathqatestergmail.sharepoint.com"

# 1. Get token
token_resp = requests.post(
    f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
    data={
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default",
    },
)
token_resp.raise_for_status()
token = token_resp.json()["access_token"]
headers = {"Authorization": f"Bearer {token}"}

# 2. Get site ID (root communication site)
site_resp = requests.get(
    f"https://graph.microsoft.com/v1.0/sites/{SITE_HOSTNAME}:/",
    headers=headers,
)
if site_resp.status_code == 403:
    print("ERROR 403: The app registration is missing Sites.Read.All or Sites.ReadWrite.All permission.")
    print("\nTo fix:")
    print("  1. Go to Azure Portal → App registrations → your app (c5905e40-...)")
    print("  2. API permissions → Add a permission → Microsoft Graph → Application permissions")
    print("  3. Add 'Sites.ReadWrite.All'")
    print("  4. Click 'Grant admin consent'")
    print("  5. Re-run this script")
    print()
    print("Alternatively, get the drive ID manually:")
    print(f"  Open in browser (while logged in as admin):")
    print(f"  https://{SITE_HOSTNAME}/_api/v2.0/drive")
    print(f"  Look for the 'id' field in the JSON response.")
    exit(1)

site_resp.raise_for_status()
site = site_resp.json()
site_id = site["id"]
print(f"Site: {site.get('displayName', site.get('name', ''))}  (id={site_id})")

# 3. Get the default drive (Documents library)
drive_resp = requests.get(
    f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive",
    headers=headers,
)
drive_resp.raise_for_status()
drive = drive_resp.json()
drive_id = drive["id"]
drive_name = drive.get("name", "")
print(f"Default drive: {drive_name!r}  id={drive_id}")

# 4. List all drives (so you can pick the right one if there are multiple)
all_drives_resp = requests.get(
    f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives",
    headers=headers,
)
all_drives = all_drives_resp.json().get("value", [])
print(f"\nAll drives on this site:")
for d in all_drives:
    marker = " ← DEFAULT (Documents)" if d["id"] == drive_id else ""
    print(f"  {d['name']!r:30s}  id={d['id']}{marker}")

print(f"\n{'='*60}")
print(f"Update your .env with:")
print(f"  DRIVE_ID={drive_id}")
print(f"  CONTAINER_ID=  (leave blank or remove — not needed for site drives)")
print(f"  CONTAINER_TYPE_ID=  (leave blank or remove)")
print(f"{'='*60}")
