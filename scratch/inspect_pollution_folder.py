import asyncio
import sys
import os
import json

sys.path.insert(0, 'c:/sharepoint spfx/backend')
os.chdir('c:/sharepoint spfx/backend')
sys.stdout.reconfigure(encoding='utf-8')

from app.graph import client, drive as gd

async def main():
    site_id = 'nissenkaiunsingapore.sharepoint.com,d53340da-789f-439e-8b40-0e75575184c0,6b456675-cd99-49f2-8e27-c95b9295b925'
    drives = await client.graph().get(f'/sites/{site_id}/drives')
    drive_id = None
    for d in drives['value']:
        if d.get('name') in ('Documents', 'Shared Documents'):
            drive_id = d['id']
            break
    print('Drive ID:', drive_id)
    
    root_children = await client.graph().get(f'/drives/{drive_id}/root/children')
    print('Root children:')
    for c in root_children.get('value', []):
        print(f"  {c.get('name')} (id: {c.get('id')})")
    
    children = await client.graph().get(f'/drives/{drive_id}/items/{folder_id}/children')
    items = children.get('value', [])
    print(f'Children in Pollution folder count: {len(items)}')
    for c in items:
        cid = c.get('id')
        cname = c.get('name')
        print(f'\n--- File: {cname} (id: {cid}) ---')
        fields = await client.graph().get(f'/drives/{drive_id}/items/{cid}/listItem/fields')
        for k in ('Vessel_x0020_Name', 'Vessel_x0020_Name_x0020_', 'Category', 'Group', 'Domain', 'Sub_x002d_Category'):
            if k in fields:
                print(f'  {k}: {fields[k]}')
        for k, v in fields.items():
            if not k.startswith('@') and k not in ('Vessel_x0020_Name', 'Vessel_x0020_Name_x0020_', 'Category', 'Group', 'Domain', 'Sub_x002d_Category', 'FileLeafRef', 'Created', 'Modified', 'AuthorLookupId', 'EditorLookupId', 'LinkFilename', 'LinkFilenameNoMenu', 'DocIcon', 'FileSizeDisplay', 'ItemChildCount', 'FolderChildCount', 'Edit', '_UIVersionString', 'ParentVersionStringLookupId', 'ParentLeafNameLookupId', '_ComplianceFlags', '_ComplianceTag', '_ComplianceTagWrittenTime', '_ComplianceTagUserId', '_CommentCount', '_LikeCount', '_DisplayName', 'ContentType', 'id'):
                print(f'  [field] {k}: {v}')

if __name__ == '__main__':
    asyncio.run(main())
