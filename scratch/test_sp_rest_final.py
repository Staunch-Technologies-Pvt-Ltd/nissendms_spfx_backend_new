import asyncio, sys, os
sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph.drive import _update_taxonomy_with_sharepoint_rest

async def main():
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    item_id  = '01YT4WOQBC74GSEYD3WRAYAPCX4NGZ5R3R'  # list item 17456

    result = await _update_taxonomy_with_sharepoint_rest(
        drive_id,
        item_id,
        "Vessel Name",
        "Snow Flower",
        "43a2ac39-b9ba-40cb-8e28-ff60e4ed8658",
    )
    print("Result:", result)

    # Verify field was actually set
    from app.graph import client as gc
    fields = await gc.graph().get(
        "/sites/nissenkaiunsingapore.sharepoint.com,8688e65a-9abf-46ff-bc2c-62a4f3654580,"
        "73bdf9ab-0856-42f4-be7b-8fb38ee6d2cc/lists/fa4dfba9-0060-4c2a-982a-15ad4a1661fa"
        "/items/17456/fields?$select=Vessel_x0020_Name_x0020_"
    )
    print("Vessel after REST call:", fields.get('Vessel_x0020_Name_x0020_'))

asyncio.run(main())
