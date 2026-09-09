"""
auto_tag_files.py
=================
Updates SharePoint Online file tags with exact Term Store taxonomy mappings:
  - Group        : Drawings / Manuals (or department name)
  - Category     : Hull / Safety / Basic / Machinery / Electrical / Main Engine / etc.
  - Sub-Category : Bulkhead plans / Superstructure / Life Saving Appliances Plan / Safety Drawings / etc.
  - Vessel Name  : Belle Lune (or vessel name)
"""
import sys
import json
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from app.ocr.drawing_category import classify_document_content, VESSEL_MASTER_LIST

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

def get_taxonomy_tags(filename, path_segments):
    classification = classify_document_content("", filename, VESSEL_MASTER_LIST)

    vessel_name = classification.get("vessel_name")
    raw_group   = classification.get("group")         # "Drawing" or "Manual"
    raw_category = classification.get("category")    # "Basic", "Hull", "Machinery", "Automation", etc.
    sub_cat      = classification.get("sub_category") # "Bulkhead plans", "General Arrangement", etc.

    # Term Store Group values are plural: "Drawings" or "Manuals"
    if raw_group == "Manual":
        group = "Manuals"
    elif raw_group == "Drawing":
        group = "Drawings"
    else:
        group = "Drawings"

    # Category = taxonomy group name (Basic / Hull / Machinery / Safety / Automation / …)
    category = raw_category or "Basic"

    # If vessel name was not detected from filename/content, infer from folder path
    if not vessel_name:
        for seg in path_segments:
            for v in VESSEL_MASTER_LIST:
                if v.lower() == seg.lower():
                    vessel_name = v
                    break
            if "common" in seg.lower():
                vessel_name = "Common (All Ships)"

    if not vessel_name and path_segments and path_segments[0] == "Kaizen - Knowledge Bank":
        vessel_name = "N/A"

    # For Kaizen, Commercial, Insurance non-drawing files
    if path_segments:
        top = path_segments[0]
        if top == "Kaizen - Knowledge Bank":
            group = "Kaizen - Knowledge Bank"
            category = path_segments[1] if len(path_segments) > 1 else "Templates"
            sub_cat = path_segments[-1] if len(path_segments) > 2 else category
        elif top == "Commercial & Chartering":
            if not raw_group or raw_group not in ("Drawing", "Manual"):
                group = "Commercial & Chartering"
                category = path_segments[2] if len(path_segments) > 2 else "Agreements"
                sub_cat = path_segments[-1]
        elif top == "Insurance":
            if not raw_group or raw_group not in ("Drawing", "Manual"):
                group = "Insurance"
                category = path_segments[2] if len(path_segments) > 2 else "P&I"
                sub_cat = path_segments[-1]

    return {
        "VesselName": vessel_name or "",
        "Group":      group,
        "Category":   category,
        "SubCategory": sub_cat or "To Be Classified"
    }

def tag_file(item_id, item_name, path_segments):
    tags = get_taxonomy_tags(item_name, path_segments)
    res = graph_request(
        "PATCH",
        f"/sites/{SITE_ID}/drives/{DRIVE_ID}/items/{item_id}/listItem/fields",
        tags
    )
    if "error" in res:
        print(f"    [ERR] Tagging {item_name}: {res}")
    else:
        print(f"    [OK] {item_name} -> Group: {tags['Group']} | Category: {tags['Category']} | Sub-Category: {tags['SubCategory']} | Vessel: {tags['VesselName']}")

def process_folder(path_segments=[]):
    if path_segments:
        encoded = "/".join(urllib.parse.quote(s, safe="") for s in path_segments)
        url = f"/sites/{SITE_ID}/drives/{DRIVE_ID}/root:/{encoded}:/children?$select=id,name,folder,file&$top=200"
    else:
        url = f"/sites/{SITE_ID}/drives/{DRIVE_ID}/root/children?$select=id,name,folder,file&$top=200"

    res = graph_request("GET", url)
    items = res.get("value", [])

    for item in items:
        if "file" in item:
            tag_file(item["id"], item["name"], path_segments)
        elif "folder" in item:
            process_folder(path_segments + [item["name"]])

if __name__ == "__main__":
    print("=== Auto-Tagging Files with Term Store Specification ===")
    process_folder([])
    print("\n[OK] Auto-tagging complete!")
