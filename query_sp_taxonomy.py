"""
query_sp_taxonomy.py
====================
Queries SharePoint Online Term Store via SharePoint REST API & CSOM / Graph
to extract the exact Term Sets, Groups, and Terms configured in the tenant.
"""
import json
import urllib.request
import urllib.parse
import urllib.error

TENANT_ID     = "866aa516-5b6a-4088-9306-cb76327df469"
CLIENT_ID     = "0c5c905b-5ad7-41e2-a207-2e7b3349417e"
CLIENT_SECRET = "Qcq8Q~y-49MbUka5OC2maWE4ygtocj3ItL.znaUE"
HOSTNAME      = "nissenkaiunsingapore.sharepoint.com"

def get_graph_token():
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

def get_sharepoint_token():
    url  = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = urllib.parse.urlencode({
        "grant_type":    "client_credentials",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "resource":      f"https://{HOSTNAME}",
    }).encode()
    # Or scope: https://{HOSTNAME}/.default
    data = urllib.parse.urlencode({
        "grant_type":    "client_credentials",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope":         f"https://{HOSTNAME}/.default",
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["access_token"]

def query_taxonomy():
    token = get_sharepoint_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json;odata=verbose",
        "Content-Type": "application/json;odata=verbose"
    }

    # 1. Try TermStore via SharePoint REST
    endpoints = [
        f"https://{HOSTNAME}/_api/v2.1/termStore/groups",
        f"https://{HOSTNAME}/_api/v2.1/termStore/sets",
        f"https://{HOSTNAME}/_api/SP.Taxonomy.TaxonomySession/getTaxonomySession/getDefaultSiteCollectionTermStore/groups?$expand=TermSets",
        f"https://{HOSTNAME}/_api/web/fields?$filter=TypeAsString eq 'TaxonomyFieldType' or TypeAsString eq 'TaxonomyFieldTypeMulti'",
    ]

    for ep in endpoints:
        print(f"\n--- Testing: {ep} ---")
        req = urllib.request.Request(ep, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read())
                print(json.dumps(data, indent=2)[:2000])
        except urllib.error.HTTPError as e:
            print(f"Error {e.code}: {e.read().decode(errors='ignore')[:300]}")

if __name__ == "__main__":
    query_taxonomy()
