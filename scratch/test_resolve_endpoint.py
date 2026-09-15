import sys, asyncio
sys.path.insert(0, 'backend')
from app.main import resolve_site_tags, SiteResolveTagsIn

async def test():
    site_id = 'nissenkaiunsingapore.sharepoint.com,8688e65a-9abf-46ff-bc2c-62a4f3654580,73bdf9ab-0856-42f4-be7b-8fb38ee6d2cc'
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    item_id = '01YT4WOQB242QQTRULWZAJWCMRCUXUSJXZ'
    
    req = SiteResolveTagsIn(
        choices={'vessel': 'ocr', 'group': 'ocr', 'category': 'ocr', 'department': 'ocr'},
        values={
            'ocr_tags': {'vessel': 'Bow Fraternity', 'group': 'Drawing', 'category': 'Electrical', 'department': 'Technical and Crewing'},
            'path_tags': {},
        }
    )
    try:
        res = await resolve_site_tags(site_id, drive_id, item_id, req, x_graph_access_token=None, x_sp_access_token=None, _session=None)
        print('Success:', res)
    except Exception as exc:
        print('Exception:', type(exc), exc)
        if hasattr(exc, 'detail'):
            print('Detail:', exc.detail)

if __name__ == '__main__':
    asyncio.run(test())
