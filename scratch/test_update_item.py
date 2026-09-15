import sys, asyncio
sys.path.insert(0, 'backend')
from app.graph.drive import update_file_columns

async def test():
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    item_id = '01YT4WOQB242QQTRULWZAJWCMRCUXUSJXZ'
    
    payload = {
        'vessel': 'Bow Fraternity',
        'group': 'Drawing',
        'category': 'Electrical',
    }
    
    res = await update_file_columns(drive_id, item_id, payload)
    print('Result:', res)

if __name__ == '__main__':
    asyncio.run(test())
