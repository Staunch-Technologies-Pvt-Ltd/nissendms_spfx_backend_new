import json
import urllib.request
from fix_all_tags import _get_token, SITE_ID, DRIVE_ID

token = _get_token()
paths = [
    "Technical & Crewing/BELLE LUNE/PO & Invoice/Purchase Order/Basic",
    "Technical & Crewing/Maersk Finisterre/PO & Invoice/Purchase Order",
    "Technical & Crewing/Potiniere/Service Agreements/Vendor & Service Provider",
    "Technical & Crewing/Bow Fraternity/To be Classified",
]

for p in paths:
    encoded = urllib.request.pathname2url(p)
    url = f"https://graph.microsoft.com/v1.0/sites/{SITE_ID}/drives/{DRIVE_ID}/root:/{encoded}:/children?$expand=listItem($expand=fields)"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        print(f"\n=== Folder: {p} ===")
        for item in data.get("value", []):
            if "file" in item:
                fields = item.get("listItem", {}).get("fields", {})
                print(f"  File: {item['name']}")
                print(f"    VesselName : {fields.get('VesselName')}")
                print(f"    Group      : {fields.get('Group')}")
                print(f"    Category   : {fields.get('Category')}")
                print(f"    SubCategory: {fields.get('SubCategory')}")
    except Exception as e:
        print(f"Error {p}: {e}")
