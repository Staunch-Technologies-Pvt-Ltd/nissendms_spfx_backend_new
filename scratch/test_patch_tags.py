"""
Diagnostic: find a file in the Crewing folder and attempt a PATCH to update tags.
"""
import json, urllib.request, urllib.parse, urllib.error

TENANT_ID     = "866aa516-5b6a-4088-9306-cb76327df469"
CLIENT_ID     = "0c5c905b-5ad7-41e2-a207-2e7b3349417e"
CLIENT_SECRET = "Qcq8Q~y-49MbUka5OC2maWE4ygtocj3ItL.znaUE"
SITE_ID       = "nissenkaiunsingapore.sharepoint.com,5c210f40-898e-4787-bd56-b49c7be3670b,32925125-47f4-41f3-94c8-873c4b053bc2"
DRIVE_ID      = "b!QA8hXI6Jh0e9VrSce-NnCyVRkjL0R_NBlMiHPEsFO8Ibb7kfR_YkQJtcYD4uxLe3"

# Auth
url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
data = urllib.parse.urlencode({
    "grant_type": "client_credentials",
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "scope": "https://graph.microsoft.com/.default",
}).encode()
with urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST")) as r:
    token = json.loads(r.read())["access_token"]
print("Token OK")

headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

# Try to list files in Crewing folder (recently uploaded file)
folder_path = "Technical%20%26%20Crewing/Norse%20New%20Haven"
list_url = (
    f"https://graph.microsoft.com/v1.0/drives/{DRIVE_ID}/root:/{folder_path}:/children"
    f"?$select=id,name,file,folder&$top=10"
)
req2 = urllib.request.Request(list_url, headers=headers)
try:
    with urllib.request.urlopen(req2) as r:
        items = json.loads(r.read()).get("value", [])
    print(f"Found {len(items)} items in {folder_path}")
    for it in items:
        print(f"  {'[DIR] ' if 'folder' in it else '[FILE]'} {it['name']}  id={it['id']}")
except urllib.error.HTTPError as e:
    print(f"List ERR {e.code}: {e.read().decode(errors='ignore')[:300]}")
    items = []

# Find a file to test patch on
file_item = None
for it in items:
    if "file" in it:
        file_item = it
        break

if not file_item:
    print("No file found for test — trying root children instead")
    list2 = f"https://graph.microsoft.com/v1.0/drives/{DRIVE_ID}/root/children?$select=id,name,file,folder&$top=20"
    with urllib.request.urlopen(urllib.request.Request(list2, headers=headers)) as r:
        root_items = json.loads(r.read()).get("value", [])
    for it in root_items:
        print(f"  {'[DIR]' if 'folder' in it else '[FILE]'} {it['name']}")

if file_item:
    item_id = file_item["id"]
    print(f"\nTesting PATCH on: {file_item['name']}  (id={item_id})")

    # 1. Read current fields
    get_url = f"https://graph.microsoft.com/v1.0/sites/{SITE_ID}/drives/{DRIVE_ID}/items/{item_id}/listItem/fields"
    req3 = urllib.request.Request(get_url, headers=headers)
    try:
        with urllib.request.urlopen(req3) as r:
            flds = json.loads(r.read())
        print("Current fields:")
        for k, v in flds.items():
            if not k.startswith("@") and k in ("VesselName", "Group", "Category", "SubCategory"):
                print(f"  {k}: {v!r}")
    except urllib.error.HTTPError as e:
        print(f"GET ERR {e.code}: {e.read().decode(errors='ignore')[:300]}")

    # 2. Try PATCH
    patch_url = f"https://graph.microsoft.com/v1.0/sites/{SITE_ID}/drives/{DRIVE_ID}/items/{item_id}/listItem/fields"
    body = json.dumps({
        "VesselName": "Norse New Haven",
        "Group": "Drawings",
        "Category": "Basic",
        "SubCategory": "General Arrangement"
    }).encode()
    patch_req = urllib.request.Request(
        patch_url,
        data=body,
        headers={**headers, "Content-Type": "application/json"},
        method="PATCH"
    )
    try:
        with urllib.request.urlopen(patch_req) as pr:
            result = json.loads(pr.read())
        print("PATCH OK!")
        print(f"  VesselName={result.get('VesselName')!r}")
        print(f"  Group={result.get('Group')!r}")
        print(f"  Category={result.get('Category')!r}")
        print(f"  SubCategory={result.get('SubCategory')!r}")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="ignore")
        print(f"PATCH ERR {e.code}: {err_body[:500]}")
