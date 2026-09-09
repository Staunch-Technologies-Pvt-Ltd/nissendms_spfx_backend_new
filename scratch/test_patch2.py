"""
Find the recently uploaded file and test PATCH on it.
From screenshot: NK-23_N-2119_GH-3030_O.D.M... in Technical & Crewing/Bow Fighter and Manuals/Drawing/Basic
"""
import json, urllib.request, urllib.parse, urllib.error

TENANT_ID     = "866aa516-5b6a-4088-9306-cb76327df469"
CLIENT_ID     = "0c5c905b-5ad7-41e2-a207-2e7b3349417e"
CLIENT_SECRET = "Qcq8Q~y-49MbUka5OC2maWE4ygtocj3ItL.znaUE"
SITE_ID       = "nissenkaiunsingapore.sharepoint.com,5c210f40-898e-4787-bd56-b49c7be3670b,32925125-47f4-41f3-94c8-873c4b053bc2"
DRIVE_ID      = "b!QA8hXI6Jh0e9VrSce-NnCyVRkjL0R_NBlMiHPEsFO8Ibb7kfR_YkQJtcYD4uxLe3"

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

def list_folder(path_encoded):
    list_url = f"https://graph.microsoft.com/v1.0/drives/{DRIVE_ID}/root:/{path_encoded}:/children?$select=id,name,file,folder&$top=50"
    req = urllib.request.Request(list_url, headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read()).get("value", [])
    except urllib.error.HTTPError as e:
        print(f"  ERR {e.code}: {e.read().decode(errors='ignore')[:200]}")
        return []

# Check the Crewing folder from the screenshot
folders_to_check = [
    "Technical%20%26%20Crewing/Norse%20New%20Haven/Crewing",
    "Technical%20%26%20Crewing/Bow%20Fighter/Drawings%20and%20Manuals/Drawings/Basic",
    "Technical%20%26%20Crewing/Bow%20Fighter",
    "Technical%20%26%20Crewing/Norse%20New%20Haven/Crewing",
]

file_item = None
for folder in folders_to_check:
    items = list_folder(folder)
    print(f"\n{folder}: {len(items)} items")
    for it in items:
        kind = "FILE" if "file" in it else "DIR "
        print(f"  [{kind}] {it['name']}")
        if "file" in it and file_item is None:
            file_item = it

if file_item:
    item_id = file_item["id"]
    item_name = file_item["name"]
    print(f"\n--- Testing PATCH on: {item_name} ---")
    print(f"item_id: {item_id}")

    # Read current fields
    get_url = f"https://graph.microsoft.com/v1.0/sites/{SITE_ID}/drives/{DRIVE_ID}/items/{item_id}/listItem/fields"
    req2 = urllib.request.Request(get_url, headers=headers)
    try:
        with urllib.request.urlopen(req2) as r:
            flds = json.loads(r.read())
        print("Current tag fields:")
        for k in ("VesselName", "Group", "Category", "SubCategory"):
            print(f"  {k}: {flds.get(k)!r}")
    except urllib.error.HTTPError as e:
        print(f"GET ERR {e.code}: {e.read().decode(errors='ignore')[:300]}")

    # Try PATCH
    patch_url = f"https://graph.microsoft.com/v1.0/sites/{SITE_ID}/drives/{DRIVE_ID}/items/{item_id}/listItem/fields"
    body = json.dumps({
        "VesselName": "Bow Fighter",
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
        print("PATCH SUCCESS!")
        for k in ("VesselName", "Group", "Category", "SubCategory"):
            print(f"  {k}: {result.get(k)!r}")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="ignore")
        print(f"PATCH FAILED {e.code}: {err_body[:600]}")
