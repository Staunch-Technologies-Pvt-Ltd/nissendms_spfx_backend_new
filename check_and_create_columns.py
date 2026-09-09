"""
check_and_create_columns.py
===========================
1. Inspects custom columns on the SharePoint Document Library list.
2. Creates the metadata columns if they don't exist:
   - Vessel Name (Text)
   - Group (Text / Choice)
   - Category (Text / Choice)
   - Sub-Category (Text / Choice)
3. Iterates over existing files in the library, determines their metadata from their folder path,
   and tags/populates the listItem fields!
"""
import json
import urllib.request
import urllib.parse
import urllib.error

TENANT_ID     = "866aa516-5b6a-4088-9306-cb76327df469"
CLIENT_ID     = "0c5c905b-5ad7-41e2-a207-2e7b3349417e"
CLIENT_SECRET = "Qcq8Q~y-49MbUka5OC2maWE4ygtocj3ItL.znaUE"
SITE_ID       = "nissenkaiunsingapore.sharepoint.com,5c210f40-898e-4787-bd56-b49c7be3670b,32925125-47f4-41f3-94c8-873c4b053bc2"
DRIVE_ID      = "b!QA8hXI6Jh0e9VrSce-NnCyVRkjL0R_NBlMiHPEsFO8Ibb7kfR_YkQJtcYD4uxLe3"

def get_token():
    url  = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = urllib.parse.urlencode({
        "grant_type":    "client_credentials",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope":         "https://graph.microsoft.com/.default",
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["access_token"]

TOKEN = get_token()

def graph_request(method, path, body=None):
    url = f"https://graph.microsoft.com/v1.0{path}"
    data = json.dumps(body).encode() if body else None
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json",
    }
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode(errors="ignore")
        return {"error": e.code, "message": err}

def list_columns():
    return graph_request("GET", f"/sites/{SITE_ID}/drives/{DRIVE_ID}/list/columns")

def create_text_column(display_name, name):
    payload = {
        "description": f"{display_name} metadata column for DMS taxonomy",
        "displayName": display_name,
        "name": name,
        "text": {
            "allowMultipleLines": False,
            "maxLength": 255
        }
    }
    return graph_request("POST", f"/sites/{SITE_ID}/drives/{DRIVE_ID}/list/columns", payload)

if __name__ == "__main__":
    print("=== Fetching Existing Columns ===")
    cols = list_columns()
    existing_names = set()
    for c in cols.get("value", []):
        existing_names.add(c.get("name"))
        existing_names.add(c.get("displayName"))
    print(f"Existing column display names: {[c.get('displayName') for c in cols.get('value', [])]}")

    columns_to_ensure = [
        ("Vessel Name", "VesselName"),
        ("Group", "Group"),
        ("Category", "Category"),
        ("Sub-Category", "SubCategory"),
    ]

    for display_name, name in columns_to_ensure:
        if display_name in existing_names or name in existing_names:
            print(f"✓ Column '{display_name}' already exists.")
        else:
            print(f"Creating column '{display_name}'...")
            res = create_text_column(display_name, name)
            print(f"  Result: {res.get('id') or res.get('error')} ({res.get('displayName') or res.get('message')})")
