"""
verify_file_tags_live.py
"""
import json
import urllib.request
import urllib.parse

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

def get_live_tags(path):
    encoded = "/".join(urllib.parse.quote(s, safe="") for s in path.split("/"))
    url = f"https://graph.microsoft.com/v1.0/sites/{SITE_ID}/drives/{DRIVE_ID}/root:/{encoded}:/children?$expand=listItem($expand=fields)&$select=id,name,file,listItem&$top=100"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read()).get("value", [])

PATHS_TO_CHECK = [
    "Technical & Crewing/Norse New Haven/Crewing",
    "Technical & Crewing/BELLE LUNE/Drawings and Manuals/To be Classified",
    "Technical & Crewing/BELLE LUNE/Drawings and Manuals/Manual/Main Engine",
]

if __name__ == "__main__":
    for path in PATHS_TO_CHECK:
        print(f"\n=== PATH: {path} ===")
        try:
            items = get_live_tags(path)
            print(f"Found {len(items)} files in folder")
            for i, item in enumerate(items):
                if "file" in item:
                    flds = item.get("listItem", {}).get("fields", {})
                    print(f"\nFile: {item['name']}")
                    print(f"  VesselName  : {flds.get('VesselName')}")
                    print(f"  Category    : {flds.get('Category')}")
                    print(f"  Group       : {flds.get('Group')}")
                    print(f"  SubCategory : {flds.get('SubCategory')}")
                    if i == 0:  # Print all column names for first file
                        all_keys = [k for k in flds.keys() if not k.startswith('@')]
                        print(f"  All SP cols : {all_keys}")
        except Exception as e:
            print(f"ERROR: {e}")
