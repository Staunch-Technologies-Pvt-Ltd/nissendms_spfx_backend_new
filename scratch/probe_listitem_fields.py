import asyncio
import json

from app.graph.client import GraphError, graph

DRIVE_ID = "b!w2ml3n29QUCKqrff5dKw0y8rHbxjA0ZEhZ8W7Uj7-MTYn3IwJ1xTRIcjyET58h5g"
ITEM_ID = "014ZGIJDOZHOQBU5XBRVCZPMJOVFF6DAJV"


async def main() -> None:
    try:
        res = await graph().get(f"/drives/{DRIVE_ID}/items/{ITEM_ID}/listItem/fields")
        print("GET_OK", json.dumps(res)[:800])
    except GraphError as e:
        print("GET_ERR", e.status, str(e))

    try:
        patched = await graph().patch(
            f"/drives/{DRIVE_ID}/items/{ITEM_ID}/listItem/fields",
            json={"VesselName": "Senegal Express"},
        )
        print("PATCH_OK", patched)
    except GraphError as e:
        print("PATCH_ERR", e.status, str(e))


if __name__ == "__main__":
    asyncio.run(main())
