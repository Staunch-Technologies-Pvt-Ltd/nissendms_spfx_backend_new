import asyncio
import sys
import os
import json

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client

async def main():
    site_id = 'nissenkaiunsingapore.sharepoint.com,8688e65a-9abf-46ff-bc2c-62a4f3654580,73bdf9ab-0856-42f4-be7b-8fb38ee6d2cc'
    list_id = 'fa4dfba9-0060-4c2a-982a-15ad4a1661fa'
    
    # Get ALL columns including hidden via the ?$expand=hidden parameter
    # and check for ALL columns that relate to Vessel
    all_cols = []
    url = f"/sites/{site_id}/lists/{list_id}/columns?$top=200"
    while url:
        resp = await client.graph().get(url)
        all_cols.extend(resp.get('value', []))
        next_link = resp.get('@odata.nextLink', '')
        url = next_link.replace('https://graph.microsoft.com/v1.0', '') if next_link else ''
    
    print(f"Total cols: {len(all_cols)}")
    
    # Find ANYTHING related to vessel
    vessel_cols = [c for c in all_cols if 'vessel' in str(c.get('name','')).lower() or 
                   'vessel' in str(c.get('displayName','')).lower()]
    print(f"\nVessel-related cols: {len(vessel_cols)}")
    for c in vessel_cols:
        print(json.dumps({
            'name': c.get('name'),
            'displayName': c.get('displayName'),
            'id': c.get('id'),
            'hidden': c.get('hidden'),
            'readOnly': c.get('readOnly'),
        }, indent=2))
    
    # Now try: write to Vessel_x0020_Name_x0020_ using a DIFFERENT format
    # Try with a "termStoreId" based approach using odata type hints
    snow_guid = '43a2ac39-b9ba-40cb-8e28-ff60e4ed8658'
    
    # Read the raw field to see if there's a field like 'VesselName' or 'Vessel_Name'
    print("\n\n=== Reading item 17456 fields with ALL fields ===")
    resp = await client.graph().get(
        f"/sites/{site_id}/lists/{list_id}/items/17456/fields"
    )
    # Show anything with 'vessel' in the key
    for k, v in sorted(resp.items()):
        if 'vessel' in k.lower():
            print(f"  {k}: {v!r}")
    
    # Try a completely different field name
    # In SP, sometimes 'Vessel_Name' or 'VesselName' also exists
    # Let's also check the content type column links
    print("\n\n=== Content types for this list ===")
    ct_resp = await client.graph().get(f"/sites/{site_id}/lists/{list_id}/contentTypes?$top=5")
    for ct in (ct_resp or {}).get('value', []):
        print(f"  {ct.get('id')}: {ct.get('name')}")
    
    # And check the column links for a content type to see what vessel field is exposed
    if ct_resp and ct_resp.get('value'):
        ct_id = ct_resp['value'][0]['id']
        cl_resp = await client.graph().get(
            f"/sites/{site_id}/lists/{list_id}/contentTypes/{ct_id}/columnLinks?$top=50"
        )
        for cl in (cl_resp or {}).get('value', []):
            if 'vessel' in str(cl).lower():
                print(f"  Column link: {cl}")

    # IMPORTANT TEST: Try the Group note col format on the Vessel column
    # Maybe the issue is we're writing to note col BUT with wrong format for vessel
    # Category note col write works with: -1;#Label|Guid
    # What if vessel needs a DIFFERENT format?
    
    print("\n\n=== Testing different note col formats for vessel ===")
    # Try the TaxWssId format - WssId is explicitly from TaxCatchAll
    # First get TaxCatchAll for item 17456 to see ALL terms registered
    tca = await client.graph().get(
        f"/sites/{site_id}/lists/{list_id}/items/17456/fields?$select=TaxCatchAll,TaxCatchAllLabel"
    )
    print(f"TaxCatchAll: {tca.get('TaxCatchAll')}")
    print(f"TaxCatchAllLabel: {tca.get('TaxCatchAllLabel')}")
    
    # Also try the SP REST API list items endpoint via Graph proxy
    # i.e., using odata query to get the raw SP data
    print("\n\n=== Try writing via different graph endpoint paths ===")
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    drive_item_id = '01YT4WOQBC74GSEYD3WRAYAPCX4NGZ5R3R'
    
    # The DRIVE endpoint /listItem/fields - using the note col - one more try with exact category format
    cat_note = 'a57855ae762b489092e6d62b4a1dc5b9'
    vessel_note = 'i62be25c1f7249f48f51efaf91f1f739'
    pollution_guid = '4837e546-fc45-4325-ab49-723662a5deed'
    
    # Simultaneously update Category (should work) and Vessel (may not work)
    # to confirm both are attempted in same request
    print("Patching both Category and Vessel note cols together...")
    try:
        res = await client.graph().patch(
            f"/drives/{drive_id}/items/{drive_item_id}/listItem/fields",
            json={
                cat_note: f'-1;#Pollution|{pollution_guid}',
                vessel_note: f'-1;#Snow Flower|{snow_guid}',
            }
        )
        print(f"Category: {res.get('Category')}")
        print(f"Vessel: {res.get('Vessel_x0020_Name_x0020_')}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == '__main__':
    asyncio.run(main())
