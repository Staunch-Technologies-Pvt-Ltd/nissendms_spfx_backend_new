import asyncio
import os
os.chdir('c:/sharepoint spfx/backend')

from app.services.real_backend import RealBackend
from app.graph import drive as gd

be = RealBackend()

async def check():
    drive_id = await be._drive()
    root_id = await gd.get_root_item_id(drive_id)
    print("Drive ID:", drive_id)
    print("Root Item ID:", root_id)
    
    root_items = await gd.list_children(drive_id, root_id)
    print("\n--- SPO ROOT ITEMS ---")
    for item in root_items:
        is_f = "folder" in item
        print(f" - {item.get('name')} (id: {item.get('id')}, isFolder: {is_f})")
        if is_f:
            children = await gd.list_children(drive_id, item['id'])
            for c in children:
                print(f"     |- {c.get('name')} (id: {c.get('id')}, isFolder: {'folder' in c})")
                if "MV Test" in c.get('name', '') or "MV Pacific" in c.get('name', ''):
                    grand = await gd.list_children(drive_id, c['id'])
                    for g in grand:
                        print(f"          |- {g.get('name')} (id: {g.get('id')}, isFolder: {'folder' in g})")
                        if "folder" in g:
                            g_sub = await gd.list_children(drive_id, g['id'])
                            for gs in g_sub:
                                is_file = "file" in gs
                                print(f"               |- {gs.get('name')} (id: {gs.get('id')}, isFile: {is_file})")

asyncio.run(check())
