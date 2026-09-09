import asyncio, os, sys, re, logging
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')

from app.graph import client

logging.basicConfig(level=logging.INFO)

async def test():
    site_id = 'nissenkaiunsingapore.sharepoint.com,d53340da-789f-439e-8b40-0e75575184c0,6b456675-cd99-49f2-8e27-c95b9295b925'
    drive_id = 'b!2kAz1Z94nkOLQA51V1GEwHVmRWuZzfJJjifJW5KVuSUbxMTRmwfrTb76eOd2LAZa'
    list_id = 'd1c4c41b-079b-4deb-befa-78e7762c065a'
    # EA-4: TEST RECORD OF GENERATOR CONTROL SYSTEM.pdf
    item_id = '01HQG22HC5SR354O5G4RA3OJWTQLCNEB3C'

    cols = await client.graph().get(f'/sites/{site_id}/lists/{list_id}/columns?expand=hidden')
    values = cols.get('value', [])
    note_cols = {}
    for c in values:
        d = c.get('displayName') or ''
        n = c.get('name') or ''
        if d.endswith('_0') and n:
            base_norm = re.sub(r'[^a-z0-9]', '', d[:-2].lower())
            note_cols[base_norm] = n
    print('Discovered note cols:', note_cols)

    # Let's patch EA-4 with SC413, Electrical, Drawings
    payload = {
        note_cols['vesselname']: '-1;#SC413|7cf4ad44-035a-42df-a04f-368d3b5b4849',
        note_cols['category']: '-1;#Electrical|ddc97b8e-30bd-41ed-8f74-b5730181fbdd',
        note_cols['group']: '-1;#Drawings|4126f096-1bb7-4538-885c-ea827147e0ec',
        'Domain': 'Technical and Crewing'
    }
    url = f'/drives/{drive_id}/items/{item_id}/listItem/fields'
    res = await client.graph().patch(url, json=payload)
    print('PATCH RESULT:')
    print('  Vessel_x0020_Name:', res.get('Vessel_x0020_Name'))
    print('  Category:', res.get('Category'))
    print('  Group:', res.get('Group'))
    print('  Domain:', res.get('Domain'))

if __name__ == '__main__':
    asyncio.run(test())
