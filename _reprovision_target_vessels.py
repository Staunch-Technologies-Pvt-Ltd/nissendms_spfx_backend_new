import asyncio, json
from app.services import get_backend

TARGET_NAMES = {"MV Vessel", "MV Fighter"}

async def main():
    be = get_backend()
    vessels = await be.list_vessels()
    targets = [v for v in vessels if v.get("name") in TARGET_NAMES]
    done = []
    errors = []
    for v in targets:
        try:
            r = await be.reprovision_vessel(v["id"])
            done.append({"id": v["id"], "name": v["name"], "ok": r.get("ok")})
        except Exception as e:
            errors.append({"id": v.get("id"), "name": v.get("name"), "error": str(e)})
    print(json.dumps({"targets": [v.get("name") for v in targets], "reprovisioned": done, "errors": errors}, indent=2))

asyncio.run(main())
