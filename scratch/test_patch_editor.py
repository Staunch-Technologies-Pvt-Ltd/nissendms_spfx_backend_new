import json, urllib.request, urllib.parse, urllib.error

TENANT_ID     = "866aa516-5b6a-4088-9306-cb76327df469"
CLIENT_ID     = "0c5c905b-5ad7-41e2-a207-2e7b3349417e"
CLIENT_SECRET = "Qcq8Q~y-49MbUka5OC2maWE4ygtocj3ItL.znaUE"
SITE_ID       = "nissenkaiunsingapore.sharepoint.com,5c210f40-898e-4787-bd56-b49c7be3670b,32925125-47f4-41f3-94c8-873c4b053bc2"
DRIVE_ID      = "b!QA8hXI6Jh0e9VrSce-NnCyVRkjL0R_NBlMiHPEsFO8Ibb7kfR_YkQJtcYD4uxLe3"
ITEM_ID       = "014ZGIJDOEKFVWWP5IEJEZMRT2M2BS2ECT"

url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
data = urllib.parse.urlencode({
    "grant_type": "client_credentials",
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "scope": "https://graph.microsoft.com/.default",
}).encode()
with urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST")) as r:
    token = json.loads(r.read())["access_token"]
headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"}

# Try patching EditorLookupId to 52
patch_url = f"https://graph.microsoft.com/v1.0/sites/{SITE_ID}/drives/{DRIVE_ID}/items/{ITEM_ID}/listItem/fields"
body = json.dumps({"EditorLookupId": "52"}).encode()
print("Trying PATCH EditorLookupId='52'...")
try:
    req = urllib.request.Request(patch_url, data=body, headers=headers, method="PATCH")
    with urllib.request.urlopen(req) as r:
        print("Success:", json.loads(r.read()))
except urllib.error.HTTPError as e:
    print(f"Error {e.code}: {e.read().decode(errors='ignore')}")

# Try via SharePoint REST API (validateUpdateListItem)
# In SharePoint REST API, validateUpdateListItem allows updating Editor and Author!
site_web_url = "https://nissenkaiunsingapore.sharepoint.com"
sp_scope = "https://nissenkaiunsingapore.sharepoint.com/.default"
data_sp = urllib.parse.urlencode({
    "grant_type": "client_credentials",
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "scope": sp_scope,
}).encode()
try:
    with urllib.request.urlopen(urllib.request.Request(url, data=data_sp, method="POST")) as r:
        sp_token = json.loads(r.read())["access_token"]
    print("\nSharePoint REST token acquired!")
    sp_headers = {
        "Authorization": f"Bearer {sp_token}",
        "Accept": "application/json;odata=verbose",
        "Content-Type": "application/json;odata=verbose",
    }
    # Call validateUpdateListItem on the item (list item id 5671)
    # FormValues: [{"FieldName": "Editor", "FieldValue": "[{'Key': 'user@email'}]"}]
    rest_url = f"{site_web_url}/_api/web/lists/getbytitle('Documents')/items(5671)/ValidateUpdateListItem"
    rest_body = json.dumps({
        "formValues": [
            {"FieldName": "Editor", "FieldValue": "[{'Key': 'i:0#.f|membership|speadmin@nissenkaiun.com'}]"} # Or user lookup
        ],
        "bNewDocumentUpdate": True
    }).encode()
    print("Calling ValidateUpdateListItem...")
    req_sp = urllib.request.Request(rest_url, data=rest_body, headers=sp_headers, method="POST")
    with urllib.request.urlopen(req_sp) as r:
        print("ValidateUpdateListItem Result:", json.loads(r.read()))
except Exception as e:
    print("SharePoint REST call error:", e)
