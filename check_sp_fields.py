import json
import urllib.request
from fix_all_tags import _get_token, SITE_ID, DRIVE_ID

token = _get_token()
url = f"https://graph.microsoft.com/v1.0/sites/{SITE_ID}/drives/{DRIVE_ID}/root:/Technical%20%26%20Crewing/Registration/Ship%20Builder:/children?$expand=listItem($expand=fields)"
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read())

for item in data.get("value", []):
    print("File:", item["name"])
    fields = item.get("listItem", {}).get("fields", {})
    for k, v in fields.items():
        if not k.startswith("@") and not k.startswith("_") and k not in ("id", "ContentType", "Edit", "DocIcon", "ItemChildCount", "FolderChildCount", "AppAuthor", "AppEditor"):
            print(f"  {k}: {v}")
