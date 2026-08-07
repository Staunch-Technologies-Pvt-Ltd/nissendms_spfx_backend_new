"""One-time: create the shared SharePoint Embedded container.

Prerequisites (see docs/SETUP.md):
  - backend/.env has AZURE_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET,
    CONTAINER_TYPE_ID
  - the backend app owns / is authorized on that container type
  - FileStorageContainer.Selected (Application) granted + admin-consented

Run:  cd backend && .\.venv\Scripts\python.exe -m scripts.create_container

Prints the new container id and drive id. Put the container id into .env as
CONTAINER_ID (the script also prints the exact line to add).
"""
import asyncio

from app.config import settings
from app.graph.client import graph
from app.graph.drive import get_container_drive_id


async def main() -> None:
    if not settings.graph_configured:
        raise SystemExit(
            "Graph not configured. Fill AZURE_TENANT_ID, GRAPH_CLIENT_ID, "
            "GRAPH_CLIENT_SECRET, CONTAINER_TYPE_ID in backend/.env first."
        )

    if settings.container_id:
        print(f"CONTAINER_ID already set: {settings.container_id}")
        drive_id = await get_container_drive_id(settings.container_id)
        print(f"drive id: {drive_id}")
        return

    print(f"Creating container '{settings.container_display_name}' ...")
    container = await graph().post(
        "/storage/fileStorage/containers",
        json={
            "displayName": settings.container_display_name,
            "description": "Vessel DMS document store",
            "containerTypeId": settings.container_type_id,
        },
    )
    container_id = container["id"]
    print(f"  container id: {container_id}")

    drive_id = await get_container_drive_id(container_id)
    print(f"  drive id:     {drive_id}")
    print("\nAdd this line to backend/.env:")
    print(f"CONTAINER_ID={container_id}")


if __name__ == "__main__":
    asyncio.run(main())
