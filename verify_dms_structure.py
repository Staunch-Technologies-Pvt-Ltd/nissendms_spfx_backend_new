"""
verify_dms_structure.py
========================
Inspects and prints the entire folder structure in the SharePoint Online
'Shared Documents' library using the DMS Migration Tool credentials.
"""
import json
import urllib.request
import urllib.parse

TENANT_ID     = "866aa516-5b6a-4088-9306-cb76327df469"
CLIENT_ID     = "0c5c905b-5ad7-41e2-a207-2e7b3349417e"
CLIENT_SECRET = "Qcq8Q~y-49MbUka5OC2maWE4ygtocj3ItL.znaUE"
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

def get_children(path=""):
    url = f"https://graph.microsoft.com/v1.0/drives/{DRIVE_ID}/root"
    if path:
        encoded = "/".join(urllib.parse.quote(s, safe="") for s in path.split("/"))
        url += f":/{encoded}:/children?$select=id,name,folder,file&$top=100"
    else:
        url += "/children?$select=id,name,folder,file&$top=100"

    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read()).get("value", [])

def print_tree(path="", indent=0):
    items = get_children(path)
    for item in items:
        prefix = "  " * indent
        is_folder = "folder" in item
        icon = "[DIR]" if is_folder else "[FILE]"
        print(f"{prefix}{icon} {item['name']}")
        if is_folder and indent < 3:
            subpath = f"{path}/{item['name']}".lstrip("/")
            print_tree(subpath, indent + 1)

if __name__ == "__main__":
    print(f"=== SharePoint Structure in Shared Documents ===")
    print_tree("Kaizen - Knowledge Bank")
