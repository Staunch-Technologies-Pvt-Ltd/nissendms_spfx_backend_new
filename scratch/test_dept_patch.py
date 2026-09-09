"""
Test whether sending 'Department' in PATCH payload causes Graph API to fail with 400!
"""
import json, urllib.request, urllib.parse, urllib.error

TENANT_ID     = "866aa516-5b6a-4088-9306-cb76327df469"
CLIENT_ID     = "0c5c905b-5ad7-41e2-a207-2e7b3349417e"
CLIENT_SECRET = "Qcq8Q~y-49MbUka5OC2maWE4ygtocj3ItL.znaUE"
SITE_ID       = "nissenkaiunsingapore.sharepoint.com,5c210f40-898e-4787-bd56-b49c7be3670b,32925125-47f4-41f3-94c8-873c4b053bc2"
DRIVE_ID      = "b!QA8hXI6Jh0e9VrSce-NnCyVRkjL0R_NBlMiHPEsFO8Ibb7kfR_YkQJtcYD4uxLe3"
ITEM_ID       = "014ZGIJDOEKFVWWP5IEJEZMRT2M2BS2ECT" # The file in Crewing

url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
data = urllib.parse.urlencode({
    "grant_type": "client_credentials",
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "scope": "https://graph.microsoft.com/.default",
}).encode()
with urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST")) as r:
    token = json.loads(r.read())["access_token"]
headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"}

patch_url = f"https://graph.microsoft.com/v1.0/sites/{SITE_ID}/drives/{DRIVE_ID}/items/{ITEM_ID}/listItem/fields"

# Test 1: With Department
body_with_dept = json.dumps({
    "Department": "Technical & Crewing",
    "VesselName": "Bow Fighter",
    "Group": "Drawings",
    "Category": "Basic",
    "SubCategory": "General Arrangement"
}).encode()

print("--- Test 1: PATCH with Department ---")
try:
    req = urllib.request.Request(patch_url, data=body_with_dept, headers=headers, method="PATCH")
    with urllib.request.urlopen(req) as r:
        print("Success:", json.loads(r.read()))
except urllib.error.HTTPError as e:
    print(f"Error {e.code}: {e.read().decode(errors='ignore')}")

# Test 2: With DMS_ aliases
body_with_aliases = json.dumps({
    "DMS_Group": "Drawings",
    "Group": "Drawings",
    "VesselName": "Bow Fighter",
    "Category": "Basic",
    "SubCategory": "General Arrangement"
}).encode()

print("\n--- Test 2: PATCH with DMS_Group ---")
try:
    req = urllib.request.Request(patch_url, data=body_with_aliases, headers=headers, method="PATCH")
    with urllib.request.urlopen(req) as r:
        print("Success:", json.loads(r.read()))
except urllib.error.HTTPError as e:
    print(f"Error {e.code}: {e.read().decode(errors='ignore')}")
