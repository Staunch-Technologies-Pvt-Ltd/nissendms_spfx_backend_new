"""Resolve a SharePoint Online *site* to its id and drive id(s).

Results are cached in-process (module-level), same lazy-singleton style used
throughout this package, since a site/drive's ids never change at runtime.
Cache keys are the (hostname, site_path) pair — this module is used for more
than one site once Site-to-Site migration is in play, so a single shared
cache slot (as it was before) would silently return the wrong site's ids for
every site after the first.

Site-id and default-drive-id resolution are cached *separately* (not as one
combined lookup) — a site's own id is always resolvable, but its *default*
document library may not exist yet (a freshly created, still-blank site has
no drive until a library is created in it). Coupling the two meant simply
selecting such a site in the Site-to-Site picker (which only needs the site
id) failed outright with a 403 from the unrelated, unneeded default-drive
fetch — `list_site_drives` (the actual library lister, further down) is the
right way to discover what libraries, if any, a site currently has.
"""
from .client import GraphError, graph

_site_id_cache: dict[tuple[str, str], str] = {}
_drive_id_cache: dict[tuple[str, str], str] = {}


async def get_site_id(hostname: str, site_path: str) -> str:
    """Return the SharePoint site id for `https://{hostname}/{site_path}` —
    needed for site-column/Term Store lookups, not just drive operations.
    Does not require the site to have a default document library."""
    key = (hostname, site_path)
    if key not in _site_id_cache:
        path = site_path.strip("/")
        # A tenant's root site has no path — `/sites/{host}:/` is not a valid
        # address for it, `/sites/{host}` is.
        site = await graph().get(f"/sites/{hostname}:/{path}" if path else f"/sites/{hostname}")
        _site_id_cache[key] = site["id"]
    return _site_id_cache[key]


async def get_site_drive_id(hostname: str, site_path: str) -> str:
    """Return the *default* document library's drive id for
    `https://{hostname}/{site_path}`.

    `site_path` should be the path after the host, e.g. "sites/VesselDocs"
    (no leading slash).
    """
    key = (hostname, site_path)
    if key not in _drive_id_cache:
        site_id = await get_site_id(hostname, site_path)
        drive = await graph().get(f"/sites/{site_id}/drive")
        _drive_id_cache[key] = drive["id"]
    return _drive_id_cache[key]


async def list_site_drives(site_id: str) -> list[dict]:
    """Every document library (drive) on a site — powers the library picker
    in Site-to-Site migration, where the destination isn't assumed to be the
    default "Documents" library. Returns an empty list (not an error) for a
    site that has no library yet."""
    data = await graph().get(f"/sites/{site_id}/drives")
    return [{"id": d["id"], "name": d.get("name", "Documents")} for d in data.get("value", [])]


async def ensure_site_drives(site_id: str) -> list[dict]:
    """Like `list_site_drives`, but auto-provisions a "Documents" library
    (via `POST /sites/{id}/lists`, `list.template: "documentLibrary"`) if the
    site has none yet — this is what lets a genuinely blank Site-to-Site
    destination be usable without the reviewer first creating a library by
    hand in SharePoint. Idempotent: a site that already has at least one
    library is never touched, and this only ever creates the one library,
    never called again once `list_site_drives` stops coming back empty."""
    drives = await list_site_drives(site_id)
    if drives:
        return drives
    try:
        created = await graph().post(
            f"/sites/{site_id}/lists",
            json={"displayName": "Documents", "list": {"template": "documentLibrary"}},
        )
    except GraphError as e:
        if e.status == 403:
            raise GraphError(
                403,
                "This app can read this site but isn't allowed to create a document "
                "library on it. Its Sites.Selected grant for this site needs the "
                "'write' role (not just 'read') — see README 'Graph API configuration'.",
            ) from e
        raise
    drive = await graph().get(f"/sites/{site_id}/lists/{created['id']}/drive")
    return [{"id": drive["id"], "name": drive.get("name", "Documents")}]


def _site_public(site: dict) -> dict:
    return {
        "site_id": site.get("id", ""),
        "label": site.get("displayName") or site.get("name") or site.get("webUrl", ""),
        "url": site.get("webUrl", ""),
    }


async def search_sites(query: str) -> list[dict]:
    """Tenant-wide site search (`GET /sites?search=`). What comes back depends
    on the app's permissions: with `Sites.Read.All` it is every matching
    site; with `Sites.Selected` Graph only returns sites already granted to
    this app (often none) — `get_site_by_url` is the reliable path then."""
    data = await graph().get("/sites", params={"search": query, "$top": "25"})
    return [_site_public(s) for s in data.get("value", []) if s.get("webUrl")]


async def get_site_by_url(hostname: str, site_path: str) -> dict:
    """Resolve one site from its URL parts — raises GraphError 403/404 when
    the app can't see it, which is exactly the access check the picker
    needs before letting a reviewer select it."""
    path = site_path.strip("/")
    site = await graph().get(f"/sites/{hostname}:/{path}" if path else f"/sites/{hostname}")
    _site_id_cache[(hostname, site_path)] = site["id"]
    return _site_public(site)
