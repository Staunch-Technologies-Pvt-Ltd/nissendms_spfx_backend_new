import asyncio, json
from app.services import get_backend

async def main():
    be = get_backend()
    vessels = await be.list_vessels()
    fixed = []
    errors = []
    for v in vessels:
        try:
            r = await be.reprovision_vessel(v["id"])
            fixed.append({"id": v["id"], "name": v["name"], "ok": r.get("ok", False)})
        except Exception as e:
            errors.append({"id": v.get("id"), "name": v.get("name"), "error": str(e)})
    print(json.dumps({"reprovisioned": len(fixed), "errors": errors[:10]}, indent=2))

asyncio.run(main())
