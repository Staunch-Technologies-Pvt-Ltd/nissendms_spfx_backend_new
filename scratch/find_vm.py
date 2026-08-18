import asyncio
import os
os.chdir('c:/sharepoint spfx/backend')

from app.services.real_backend import RealBackend
from app.graph import drive as gd

be = RealBackend()

async def find_vessel_management():
    drive_id = await be._drive()
    root_id = await gd.get_root_item_id(drive_id)
    root_items = await gd.list_children(drive_id, root_id)
    
    vm_folder = None
    for item in root_items:
        if item.get('name') == 'Vessel Management':
            vm_folder = item
            break
            
    print("Vessel Management folder found at root:", vm_folder)
    if vm_folder:
        vm_children = await gd.list_children(drive_id, vm_folder['id'])
        print(f"\nChildren of Vessel Management ({len(vm_children)} items):")
        for c in vm_children:
            print(f" - {c.get('name')} (id: {c.get('id')})")
            if c.get('name') == 'Technical & Crewing':
                tc_children = await gd.list_children(drive_id, c['id'])
                for tc in tc_children:
                    print(f"     |- {tc.get('name')} (id: {tc.get('id')})")
                    if tc.get('name') == 'MV Test 08112026':
                        v_children = await gd.list_children(drive_id, tc['id'])
                        for vc in v_children:
                            print(f"          |- {vc.get('name')} (id: {vc.get('id')})")
                            if vc.get('name') == 'Crewing':
                                cr_children = await gd.list_children(drive_id, vc['id'])
                                print(f"\n>>> FILES IN Vessel Management/Technical & Crewing/MV Test 08112026/Crewing ({len(cr_children)} files):")
                                for f in cr_children:
                                    print(f"               FILE: {f.get('name')} | ID: {f.get('id')} | Size: {f.get('size')}")

asyncio.run(find_vessel_management())
