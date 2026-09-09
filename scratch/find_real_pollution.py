import asyncio
import sys
import os

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client

async def main():
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    dm_id = '01YT4WOQFSPWRSQPOSRJDJSNNOGWKJUIDC' # Drawings and Manuals
    
    # List Manuals
    dm_children = (await client.graph().get(f"/drives/{drive_id}/items/{dm_id}/children")).get('value', [])
    manuals_id = None
    for c in dm_children:
        print(f"DM child: {c.get('name')} (id: {c.get('id')})")
        if 'manual' in c.get('name', '').lower():
            manuals_id = c.get('id')
            
    if not manuals_id:
        print("Manuals folder not found!")
        return

    # List Pollution
    man_children = (await client.graph().get(f"/drives/{drive_id}/items/{manuals_id}/children")).get('value', [])
    pollution_id = None
    for c in man_children:
        print(f"Manuals child: {c.get('name')} (id: {c.get('id')})")
        if 'pollution' in c.get('name', '').lower():
            pollution_id = c.get('id')
            
    if not pollution_id:
        print("Pollution folder not found!")
        return

    # List files in Pollution!
    poll_files = (await client.graph().get(f"/drives/{drive_id}/items/{pollution_id}/children")).get('value', [])
    print(f"\nFiles in Technical and Crewing New / Snow Flower / ... / Pollution ({len(poll_files)} files):")
    for f in poll_files:
        fid = f.get('id')
        fname = f.get('name')
        fields = await client.graph().get(f"/drives/{drive_id}/items/{fid}/listItem/fields")
        print(f"\n--- {fname} (id: {fid}) ---")
        for k in ('VesselName', 'Vessel_x0020_Name', 'Vessel_x0020_Name_x0020_', 'Category', 'Group', 'Domain', 'Sub_x002d_Category'):
            print(f"  {k}: {fields.get(k)}")

if __name__ == '__main__':
    asyncio.run(main())
