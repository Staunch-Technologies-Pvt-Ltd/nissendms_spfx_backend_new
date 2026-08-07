import asyncio, json
from app.services import get_backend

TARGET_NAMES = ["MV Vessel", "MV Fighter"]

async def main():
    be = get_backend()
    vessels = await be.list_vessels()
    name_to_id = {v["name"]: v["id"] for v in vessels}
    result = {"done": [], "errors": []}026-08-01 08:27:25,342 INFO apscheduler.scheduler: Scheduler started
2026-08-01 08:27:25,342 INFO app.scheduler: Scheduler started: precreate_next_month (daily) + session_sweep (15 min) + reconcile_pool (5 min)
INFO:     Application startup complete.
INFO:     127.0.0.1:52674 - "OPTIONS /api/vessels/flat-tree HTTP/1.1" 200 OK
INFO:     127.0.0.1:52674 - "GET /api/vessels/flat-tree HTTP/1.1" 401 Unauthorized
INFO:     127.0.0.1:52674 - "OPTIONS /api/vessels HTTP/1.1" 200 OK
INFO:     127.0.0.1:52674 - "GET /api/vessels HTTP/1.1" 401 Unauthorized
INFO:     127.0.0.1:54068 - "GET /api/vessels/flat-tree HTTP/1.1" 401 Unauthorized
INFO:     127.0.0.1:54068 - "GET /api/vessels HTTP/1.1" 401 Unauthorized
INFO:     127.0.0.1:54068 - "GET /api/vessels/flat-tree HTTP/1.1" 401 Unauthorized
INFO:     127.0.0.1:54068 - "GET /api/vessels HTTP/1.1" 401 Unauthorized
    for name in TARGET_NAMES:
        vid = name_to_id.get(name)
        if not vid:
            result["errors"].append({"name": name, "error": "not found in vessels table"})
            continue
        try:
            r = await be.reprovision_vessel(vid)
            result["done"].append({"name": name, "id": vid, "ok": r.get("ok", False)})
        except Exception as e:
            result["errors"].append({"name": name, "id": vid, "error": str(e)})
    print(json.dumps(result, indent=2))

asyncio.run(main())
