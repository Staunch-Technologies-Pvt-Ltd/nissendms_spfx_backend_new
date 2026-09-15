import sys, asyncio
sys.path.insert(0, 'backend')
from app.graph.client import graph

async def test():
    g = graph()
    drive_id = 'b!WuaIhr-a_0a8LGKk82VFgKv5vXNWCPRCvnuPs47m0syp-036YAAqTJgqFa1KFmH6'
    
    # Let's search for files from the user screenshot:
    # N-2120_E-1140_INSTRUCTION FOR BATT CH & DISCH BOARD.pdf
    items = await g.get(f'/drives/{drive_id}/root/search(q=\'N-2120_E-1140\')')
    val = items.get('value', [])
    print('Found files:', len(val))
    if val:
        item = val[0]
        item_id = item['id']
        print('Item name:', item['name'])
        print('Item id:', item_id)
        fields = await g.get(f'/drives/{drive_id}/items/{item_id}/listItem/fields')
        print('Current fields:')
        for k, v in fields.items():
            if any(w in k.lower() for w in ['vessel', 'category', 'group', 'department', 'i62', 'g5e', 'a57', 'j5d']):
                print(f'  {k}: {v}')

if __name__ == '__main__':
    asyncio.run(test())
