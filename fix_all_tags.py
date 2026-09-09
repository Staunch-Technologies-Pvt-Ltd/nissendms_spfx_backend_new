"""
fix_all_tags.py  (v4)
======================
Bulk re-tagger for the nissenkaiunsingapore.sharepoint.com comm-site library.

Taxonomy based on official Term Store:
- Group: Drawings | Manuals
- Category & Sub-Category: exact Term Store mapping
- Vessel Name: 24 Production Vessels (Belle Lune, Peissy, Ghana Express, etc.)
"""
import sys
import io
import json
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

# Force UTF-8 stdout and line buffering for real-time logs
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)

TENANT_ID     = "866aa516-5b6a-4088-9306-cb76327df469"
CLIENT_ID     = "0c5c905b-5ad7-41e2-a207-2e7b3349417e"
CLIENT_SECRET = "Qcq8Q~y-49MbUka5OC2maWE4ygtocj3ItL.znaUE"
SITE_ID       = "nissenkaiunsingapore.sharepoint.com,5c210f40-898e-4787-bd56-b49c7be3670b,32925125-47f4-41f3-94c8-873c4b053bc2"
DRIVE_ID      = "b!QA8hXI6Jh0e9VrSce-NnCyVRkjL0R_NBlMiHPEsFO8Ibb7kfR_YkQJtcYD4uxLe3"

DRY_RUN = "--apply" not in sys.argv

sys.path.insert(0, str(Path(__file__).parent))
from app.ocr.drawing_category import (
    VESSEL_MASTER_LIST,
    DRAWING_TAXONOMY,
    MANUAL_TAXONOMY,
    classify_document_content,
    extract_vessel_name_from_text,
)

DRAWING_CATS = {k.lower(): k for k in DRAWING_TAXONOMY}
MANUAL_CATS  = {k.lower(): k for k in MANUAL_TAXONOMY}
VESSEL_LOWER = {v.lower(): v for v in VESSEL_MASTER_LIST}

SKIP_TOP_FOLDERS = {"commercial & chartering", "insurance", "kaizen - knowledge bank"}

def _get_token():
    url  = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default",
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST")) as r:
        return json.loads(r.read())["access_token"]

TOKEN   = _get_token()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
print("[AUTH] Token OK", flush=True)

def _graph_pages(url: str) -> list:
    items = []
    while url:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
        items.extend(data.get("value", []))
        url = data.get("@odata.nextLink", "")
    return items

def _graph_patch(path: str, body: dict) -> dict:
    url  = f"https://graph.microsoft.com/v1.0{path}"
    data = json.dumps(body).encode()
    req  = urllib.request.Request(url, data=data,
                                  headers={**HEADERS, "Content-Type": "application/json"},
                                  method="PATCH")
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return {"error": e.code, "message": e.read().decode(errors="ignore")}

def _children(item_id: str) -> list:
    url = (
        f"https://graph.microsoft.com/v1.0"
        f"/drives/{DRIVE_ID}/items/{item_id}/children"
        f"?$expand=listItem($expand=fields)"
        f"&$select=id,name,folder,file&$top=500"
    )
    return _graph_pages(url)

def _n(s): return (s or "").strip().lower()

def _classify_by_text(context_text: str, filename: str) -> tuple[str, str, str, str]:
    """Run classifier on filename + path context to extract (vessel, group, category, sub_category)."""
    res = classify_document_content(context_text, filename, VESSEL_MASTER_LIST)
    vessel = res.get("vessel_name") or ""
    raw_grp = res.get("group") or ""
    group = "Drawings" if raw_grp == "Drawing" else ("Manuals" if raw_grp == "Manual" else "Drawings")
    category = res.get("category") or "To Be Classified"
    sub_category = res.get("sub_category") or "To Be Classified"
    return vessel, group, category, sub_category

def derive_correct_tags(path_segments: list, filename: str) -> dict | None:
    if not path_segments:
        return None

    top = _n(path_segments[0])
    if top in SKIP_TOP_FOLDERS:
        return None

    folder_context = " ".join(path_segments)

    # 1. Determine Vessel Name
    vessel = ""
    # Check if segment 1 is an exact registered vessel name
    if top == "technical & crewing" and len(path_segments) >= 2:
        seg1_low = _n(path_segments[1])
        if seg1_low in VESSEL_LOWER:
            vessel = VESSEL_LOWER[seg1_low]
    
    # If not found from folder path[1], detect from path or filename
    if not vessel:
        detected = extract_vessel_name_from_text(folder_context, filename, VESSEL_MASTER_LIST)
        if detected and detected in VESSEL_MASTER_LIST:
            vessel = detected

    # 2. Determine Group, Category, Sub-Category
    group, category, sub_category = "", "", ""

    # Check for "Drawings and Manuals" in path
    dm_idx = next(
        (i for i, p in enumerate(path_segments)
         if _n(p) in ("drawings and manuals", "drawing and manual", "drawings & manuals")),
        -1,
    )

    if dm_idx >= 0:
        t1 = path_segments[dm_idx + 1] if len(path_segments) > dm_idx + 1 else ""
        t2 = path_segments[dm_idx + 2] if len(path_segments) > dm_idx + 2 else ""
        t3 = path_segments[dm_idx + 3] if len(path_segments) > dm_idx + 3 else ""
        n1, n2 = _n(t1), _n(t2)

        if n1 in ("drawings", "drawing"):
            group = "Drawings"
            category = DRAWING_CATS.get(n2, t2)
            sub_category = t3
        elif n1 in ("manuals", "manual"):
            group = "Manuals"
            category = MANUAL_CATS.get(n2, t2)
            sub_category = t3
        elif n1 in DRAWING_CATS:
            group = "Drawings"
            category = DRAWING_CATS[n1]
            sub_category = DRAWING_CATS.get(n2, t2)
        elif n1 in MANUAL_CATS:
            group = "Manuals"
            category = MANUAL_CATS[n1]
            sub_category = MANUAL_CATS.get(n2, t2)
        elif n1 in ("to be classified", "to_be_classified"):
            group = "Manuals"
            category = "To Be Classified"
            sub_category = "To Be Classified"

    # If Group or Category not yet determined or generic, run intelligent classifier
    if not group or not category or _n(category) in {"drawings and manuals", "drawing", "manual", "registration", "ship builder", "to be classified"}:
        _, c_grp, c_cat, c_sub = _classify_by_text(folder_context, filename)
        if c_grp and c_cat:
            group = c_grp
            category = c_cat
            sub_category = c_sub

    # If sub_category is missing or equals category or is empty, resolve leaf term
    if not sub_category or _n(sub_category) == _n(category):
        _, _, _, c_sub = _classify_by_text(folder_context, filename)
        if c_sub and _n(c_sub) != _n(category):
            sub_category = c_sub
        else:
            # Fallback to category default or To Be Classified
            if group == "Drawings" and category in DRAWING_TAXONOMY:
                sub_category = list(DRAWING_TAXONOMY[category].keys())[0]
            elif group == "Manuals" and category in MANUAL_TAXONOMY:
                sub_category = list(MANUAL_TAXONOMY[category].keys())[0]
            else:
                sub_category = "To Be Classified"

    # Final normalization
    if _n(group) in ("drawing", "drawings"):
        group = "Drawings"
    elif _n(group) in ("manual", "manuals"):
        group = "Manuals"
    else:
        group = "Drawings"

    bad_categories = {
        "technical & crewing", "commercial & chartering", "insurance",
        "kaizen - knowledge bank", "drawings and manuals",
        "registration", "ship builder", "shared documents", "drawing", "manual",
    }
    if not category or _n(category) in bad_categories:
        category = "To Be Classified"
        sub_category = "To Be Classified"
        group = "Manuals"

    return {
        "VesselName":  vessel,
        "Group":       group,
        "Category":    category,
        "SubCategory": sub_category,
    }

def _differs(current: dict, correct: dict) -> bool:
    for k in ("VesselName", "Group", "Category", "SubCategory"):
        c = _n(current.get(k, ""))
        e = _n(correct.get(k, ""))
        if c != e:
            return True
    return False

stats = {"scanned": 0, "skipped": 0, "wrong": 0, "patched": 0, "errors": 0}

def walk(item_id: str, path_segments: list):
    try:
        children = _children(item_id)
    except Exception as exc:
        print(f"  [ERR] listing /{'/'.join(path_segments)}: {exc}", flush=True)
        stats["errors"] += 1
        return

    for item in children:
        name = item["name"]
        iid  = item["id"]

        if "folder" in item:
            walk(iid, path_segments + [name])

        elif "file" in item:
            stats["scanned"] += 1
            flds = (item.get("listItem") or {}).get("fields") or {}
            current = {
                "VesselName":  flds.get("VesselName") or flds.get("Vessel_x0020_Name") or "",
                "Group":       flds.get("Group", ""),
                "Category":    flds.get("Category", ""),
                "SubCategory": flds.get("SubCategory") or flds.get("Sub_x0020_Category") or "",
            }

            correct = derive_correct_tags(path_segments, name)

            if correct is None:
                stats["skipped"] += 1
                continue

            if not _differs(current, correct):
                continue

            stats["wrong"] += 1
            path_str = "/".join(path_segments + [name])
            print(f"\n  FILE : {path_str}", flush=True)
            print(f"  OLD  : Vessel={current['VesselName']!r}  Group={current['Group']!r}"
                  f"  Cat={current['Category']!r}  Sub={current['SubCategory']!r}", flush=True)
            print(f"  NEW  : Vessel={correct['VesselName']!r}  Group={correct['Group']!r}"
                  f"  Cat={correct['Category']!r}  Sub={correct['SubCategory']!r}", flush=True)

            if DRY_RUN:
                print("  --> DRY RUN (no change written)", flush=True)
                continue

            patch_path = f"/drives/{DRIVE_ID}/items/{iid}/listItem/fields"
            result = _graph_patch(patch_path, correct)
            if isinstance(result, dict) and "error" in result:
                print(f"  [ERR] patch failed: {result}", flush=True)
                stats["errors"] += 1
            else:
                print("  [OK] patched successfully", flush=True)
                stats["patched"] += 1

if __name__ == "__main__":
    mode = "DRY RUN (pass --apply to write changes)" if DRY_RUN else "APPLY MODE — writing changes to SharePoint!"
    print(f"\n=== Fix All SharePoint Tags — {mode} ===\n", flush=True)

    req = urllib.request.Request(
        f"https://graph.microsoft.com/v1.0/drives/{DRIVE_ID}/root?$select=id",
        headers=HEADERS,
    )
    with urllib.request.urlopen(req) as r:
        root_id = json.loads(r.read())["id"]

    root_children = _graph_pages(
        f"https://graph.microsoft.com/v1.0/drives/{DRIVE_ID}/root/children"
        f"?$select=id,name,folder&$top=200"
    )
    top_names = [i["name"] for i in root_children if "folder" in i]
    print(f"[ROOT] Top-level folders: {top_names}\n", flush=True)

    walk(root_id, [])

    print(f"\n=== Complete ===", flush=True)
    print(f"  Files scanned  : {stats['scanned']}", flush=True)
    print(f"  Files skipped  : {stats['skipped']}  (non-TC departments)", flush=True)
    print(f"  Wrong tags     : {stats['wrong']}", flush=True)
    print(f"  Patched        : {stats['patched']}", flush=True)
    print(f"  Errors         : {stats['errors']}", flush=True)
