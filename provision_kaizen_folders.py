"""
provision_kaizen_folders.py
============================
One-shot script: creates (or verifies) the full Kaizen - Knowledge Bank
folder tree directly in the SharePoint Online 'Shared Documents' library
visible at https://nissenkaiunsingapore.sharepoint.com/Shared%20Documents/.

Usage:
    cd "c:\\sharepoint spfx\\backend"
    python provision_kaizen_folders.py

Authenticates via client_credentials (service-principal) using the same
Graph app registered in .env -- no browser login required.
"""

import sys
import json
import time
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

# ---------------------------------------------------------------------------
# DMS Migration Tool app credentials (has Sites.FullControl.All + Files.ReadWrite.All)
# ---------------------------------------------------------------------------
TENANT_ID     = "866aa516-5b6a-4088-9306-cb76327df469"
CLIENT_ID     = "0c5c905b-5ad7-41e2-a207-2e7b3349417e"
CLIENT_SECRET = "Qcq8Q~y-49MbUka5OC2maWE4ygtocj3ItL.znaUE"

SHAREPOINT_HOSTNAME  = "nissenkaiunsingapore.sharepoint.com"
SHAREPOINT_SITE_PATH = "/"
GRAPH_BASE           = "https://graph.microsoft.com/v1.0"

# Known drive ID — fast path: skip drive resolution entirely
KNOWN_DOCUMENTS_DRIVE_ID = "b!QA8hXI6Jh0e9VrSce-NnCyVRkjL0R_NBlMiHPEsFO8Ibb7kfR_YkQJtcYD4uxLe3"


# ---------------------------------------------------------------------------
# Kaizen folder structure (mirrors template.py FLAT_TEMPLATE)
# ---------------------------------------------------------------------------
KAIZEN_STRUCTURE = {
    "Kaizen - Knowledge Bank": {
        "Templates": {},
        "Procedures and Work Instructions": {},
        "Lessons Learned": {},
        "Circulars and Guidance": {
            "Equipment Maker": {},
            "Class": {},
            "Flag - Port State": {},
            "SIRE-OCIMF-RightShip": {},
            "Shipyard": {},
        },
    }
}

# ---------------------------------------------------------------------------
# Graph HTTP helpers (stdlib urllib only)
# ---------------------------------------------------------------------------
_token_cache = {}

def _get_token():
    now = time.time()
    if _token_cache.get("expires_at", 0) > now + 60:
        return _token_cache["access_token"]
    url  = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = urllib.parse.urlencode({
        "grant_type":    "client_credentials",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope":         "https://graph.microsoft.com/.default",
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read())
    _token_cache["access_token"] = body["access_token"]
    _token_cache["expires_at"]   = now + int(body.get("expires_in", 3600))
    return _token_cache["access_token"]


def _graph(method, path, payload=None):
    token = _get_token()
    url   = f"{GRAPH_BASE}{path}"
    body  = json.dumps(payload).encode() if payload else None
    hdrs  = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode(errors="ignore")
        raise RuntimeError(f"Graph {method} {path} -> HTTP {e.code}: {err}") from e


def _get(path):  return _graph("GET",  path)
def _post(path, payload): return _graph("POST", path, payload)


# ---------------------------------------------------------------------------
# Site & Drive resolution
# ---------------------------------------------------------------------------
def get_site_id():
    r = _get(f"/sites/{SHAREPOINT_HOSTNAME}:{SHAREPOINT_SITE_PATH}")
    sid = r["id"]
    print(f"  OK  Site ID : {sid}")
    return sid


def get_documents_drive_id(site_id):
    # Strategy 1: list all drives (requires Sites.Read.All)
    r      = _get(f"/sites/{site_id}/drives")
    drives = r.get("value", [])
    print(f"  INFO: Found {len(drives)} drives via /sites/{site_id}/drives:")
    for d in drives:
        print(f"        - name='{d.get('name')}' driveType='{d.get('driveType')}' id={d.get('id')}")
    # Prefer drive named 'Documents'
    for d in drives:
        if d.get("driveType") == "documentLibrary" and d.get("name") in ("Documents", "Shared Documents"):
            print(f"  OK  Drive : '{d['name']}' (id={d['id']})")
            return d["id"]
    # Fallback: first documentLibrary
    for d in drives:
        if d.get("driveType") == "documentLibrary":
            print(f"  OK  Drive (fallback) : '{d['name']}' (id={d['id']})")
            return d["id"]
    for d in drives:
        print(f"  WARN: No documentLibrary, using first drive: '{d.get('name')}' (id={d.get('id')})")
        return d["id"]

    # Strategy 2: get the site's default drive directly (different permission scope)
    print("  INFO: /drives returned empty — trying /sites/{id}/drive (default drive) ...")
    try:
        d = _get(f"/sites/{site_id}/drive")
        if d.get("id"):
            print(f"  OK  Default Drive : '{d.get('name')}' (id={d['id']})")
            return d["id"]
    except RuntimeError as e:
        print(f"  WARN: Default drive also failed: {e}")

    # Strategy 3: use the site's root drive URL pattern
    print("  INFO: Trying /drives via site URL pattern ...")
    try:
        # Try getting root of the known drive ID through the site
        d = _get(f"/sites/{site_id}/lists?$filter=list/template eq 101&$select=id,name,webUrl")
        lists = d.get("value", [])
        print(f"  INFO: Found {len(lists)} document libraries (lists with template=101):")
        for lst in lists:
            print(f"        - name='{lst.get('name')}' id={lst.get('id')}")
    except RuntimeError as e:
        print(f"  WARN: List query failed: {e}")

    raise RuntimeError(
        f"Cannot access any drive on site {site_id}. "
        "The service principal likely needs 'Sites.ReadWrite.All' or 'Files.ReadWrite.All' "
        "application permission in Azure AD."
    )


# ---------------------------------------------------------------------------
# Folder provisioning (idempotent)
# ---------------------------------------------------------------------------
def ensure_folder(drive_id, parent_path, folder_name):
    full_path = (parent_path + "/" + folder_name).lstrip("/")
    encoded   = "/".join(urllib.parse.quote(s, safe="") for s in full_path.split("/"))

    # 1. Check if it already exists
    try:
        item = _get(f"/drives/{drive_id}/root:/{encoded}?$select=id,name,folder")
        if item.get("id"):
            print(f"    EXISTS  : {full_path}")
            return item["id"]
    except RuntimeError as exc:
        if "404" not in str(exc) and "itemNotFound" not in str(exc):
            raise

    # 2. Create it
    if parent_path.strip("/"):
        enc_parent = "/".join(urllib.parse.quote(s, safe="") for s in parent_path.strip("/").split("/"))
        create_url = f"/drives/{drive_id}/root:/{enc_parent}:/children"
    else:
        create_url = f"/drives/{drive_id}/root/children"

    try:
        created = _post(create_url, {
            "name": folder_name,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        })
        print(f"    CREATED : {full_path}  (id={created['id']})")
        return created["id"]
    except RuntimeError as exc:
        if "nameAlreadyExists" in str(exc) or "conflict" in str(exc).lower():
            item = _get(f"/drives/{drive_id}/root:/{encoded}?$select=id,name,folder")
            print(f"    EXISTS  : {full_path}  (race-condition recovery)")
            return item["id"]
        raise


def provision_tree(drive_id, parent_path, tree):
    for folder_name, children in tree.items():
        ensure_folder(drive_id, parent_path, folder_name)
        full = (parent_path + "/" + folder_name).lstrip("/")
        if children:
            provision_tree(drive_id, full, children)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 62)
    print("  Kaizen - Knowledge Bank  -  SharePoint Folder Provisioner")
    print("=" * 62)
    print(f"\nApp        : DMS Migration Tool")
    print(f"Tenant ID  : {TENANT_ID}")
    print(f"Client ID  : {CLIENT_ID}")
    print(f"Site       : https://{SHAREPOINT_HOSTNAME}{SHAREPOINT_SITE_PATH}\n")

    if not all([TENANT_ID, CLIENT_ID, CLIENT_SECRET]):
        print(f"ERROR: Missing Graph credentials.")
        sys.exit(1)

    # --- Drive resolution ---------------------------------------------------
    # Strategy 1: Use the known hard-coded drive ID (fastest, no extra permissions)
    # Strategy 2: Fall back to resolving via /sites/{id}/drives
    drive_id = None

    print("Step 1/2  Resolving Shared Documents drive ...")
    # Try the known drive ID first
    try:
        test = _get(f"/drives/{KNOWN_DOCUMENTS_DRIVE_ID}/root?$select=id,name")
        if test.get("id"):
            drive_id = KNOWN_DOCUMENTS_DRIVE_ID
            print(f"  OK  Using known drive ID: {drive_id}")
    except RuntimeError as e:
        print(f"  WARN: Known drive ID inaccessible ({e}) — will try site lookup ...")

    if not drive_id:
        # Try resolving via site
        try:
            site_id  = get_site_id()
            drive_id = get_documents_drive_id(site_id)
        except RuntimeError as e:
            print(f"  ERROR: Could not resolve drive: {e}")
            sys.exit(1)

    # --- Provisioning -------------------------------------------------------
    print("\nStep 2/2  Ensuring Kaizen folder tree ...")
    provision_tree(drive_id, "", KAIZEN_STRUCTURE)

    print("\nDone!  All Kaizen - Knowledge Bank folders are in place.")
    print(f"\nVerify: https://{SHAREPOINT_HOSTNAME}/Shared%20Documents/Kaizen%20-%20Knowledge%20Bank\n")


if __name__ == "__main__":
    main()
