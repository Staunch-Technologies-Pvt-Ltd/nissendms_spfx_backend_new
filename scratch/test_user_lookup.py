import json, urllib.request, urllib.parse, urllib.error

TENANT_ID     = "866aa516-5b6a-4088-9306-cb76327df469"
CLIENT_ID     = "0c5c905b-5ad7-41e2-a207-2e7b3349417e"
CLIENT_SECRET = "Qcq8Q~y-49MbUka5OC2maWE4ygtocj3ItL.znaUE"
SITE_ID       = "nissenkaiunsingapore.sharepoint.com,5c210f40-898e-4787-bd56-b49c7be3670b,32925125-47f4-41f3-94c8-873c4b053bc2"
DRIVE_ID      = "b!QA8hXI6Jh0e9VrSce-NnCyVRkjL0R_NBlMiHPEsFO8Ibb7kfR_YkQJtcYD4uxLe3"
ITEM_ID       = "014ZGIJDOEKFVWWP5IEJEZMRT2M2BS2ECT"

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
headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"}

# 1. Fetch current fields
get_url = f"https://graph.microsoft.com/v1.0/sites/{SITE_ID}/drives/{DRIVE_ID}/items/{ITEM_ID}/listItem/fields"
req = urllib.request.Request(get_url, headers=headers)
with urllib.request.urlopen(req) as r:
    fields = json.loads(r.read())

print("Current fields on item:")
for k, v in fields.items():
    if not k.startswith("@"):
        print(f"  {k}: {v}")

# 2. Check site users (User Information List) to find users and their lookup IDs
users_url = f"https://graph.microsoft.com/v1.0/sites/{SITE_ID}/lists/User Information List/items?$expand=fields&$top=50"
try:
    req_u = urllib.request.Request(users_url, headers=headers)
    with urllib.request.urlopen(req_u) as r:
        u_data = json.loads(r.read())
    print(f"\nFound {len(u_data.get('value', []))} users in User Information List:")
    for u in u_data.get("value", []):
        f = u.get("fields", {})
        print(f"  ID={f.get('id')} / LookupID={u.get('id')} | Title={f.get('Title')} | EMail={f.get('EMail')} | Name={f.get('Name')}")
except Exception as e:
    print("Error fetching user list:", e)
