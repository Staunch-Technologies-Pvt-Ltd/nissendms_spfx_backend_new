"""
inspect_terms_detail.py
"""
import json
import urllib.request
import urllib.parse
import urllib.error

TENANT_ID     = "866aa516-5b6a-4088-9306-cb76327df469"
CLIENT_ID     = "0c5c905b-5ad7-41e2-a207-2e7b3349417e"
CLIENT_SECRET = "Qcq8Q~y-49MbUka5OC2maWE4ygtocj3ItL.znaUE"
SITE_ID       = "nissenkaiunsingapore.sharepoint.com,5c210f40-898e-4787-bd56-b49c7be3670b,32925125-47f4-41f3-94c8-873c4b053bc2"

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

def get_graph(path):
    url = f"https://graph.microsoft.com/v1.0{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode(errors="ignore")
        return {"error": e.code, "message": err}

if __name__ == "__main__":
    print("=== Site Term Store Groups ===")
    groups = get_graph(f"/sites/{SITE_ID}/termStore/groups")
    print(json.dumps(groups, indent=2))

    print("\n=== Site Term Store Sets ===")
    sets = get_graph(f"/sites/{SITE_ID}/termStore/sets")
    print(json.dumps(sets, indent=2))
