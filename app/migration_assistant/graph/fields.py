"""Reading a driveItem's SharePoint list-item column values, and reading a
site's column *definitions* (to tell which columns are Managed Metadata).

Deliberately read-only and Graph-only: writing is a separate concern (see
`sharepoint_rest.py`'s docstring for why plain-column writes and Managed
Metadata writes don't go through the same path).
"""
from .client import graph


async def get_list_item_id(drive_id: str, item_id: str) -> int:
    """The SharePoint list item's own numeric id for a driveItem — distinct
    from Graph's drive-item id string, and what `sharepoint_rest.py` needs
    to address the item (SharePoint REST addresses list items by this
    number, not the Graph drive-item id)."""
    data = await graph().get(f"/drives/{drive_id}/items/{item_id}/listItem?$select=id")
    return int(data["id"])


async def get_item_fields(drive_id: str, item_id: str) -> dict:
    """The SharePoint column values for one driveItem, keyed by internal
    column name. Graph's `fields` facet reliably returns text/choice/number/
    date/person columns, but for a Managed Metadata (taxonomy) column it
    only ever returns the display label as plain text — never the term's
    stable id — which is why term mapping (`services/term_mapping.py`) reads
    taxonomy columns through SharePoint REST instead of this function."""
    data = await graph().get(f"/drives/{drive_id}/items/{item_id}/listItem?$expand=fields")
    return (data.get("fields") or {})


async def get_list_item_id_and_fields(drive_id: str, item_id: str) -> tuple[int, dict]:
    """`get_list_item_id` + `get_item_fields` in one round-trip — worth it
    when both are needed for every file of a large scan."""
    data = await graph().get(f"/drives/{drive_id}/items/{item_id}/listItem?$expand=fields")
    return int(data["id"]), (data.get("fields") or {})


async def list_site_columns(site_id: str) -> list[dict]:
    """Every column defined on a site, including its type facets — used to
    find which internal column names are Managed Metadata (`termColumn`)
    columns and which term set backs each one (`termColumn.termSetId`),
    both needed before a value from that column can be treated as taxonomy
    rather than plain text."""
    columns, url = [], f"/sites/{site_id}/columns?$top=200"
    while url:
        data = await graph().get(url)
        columns.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return columns


def taxonomy_columns(columns: list[dict]) -> dict[str, str]:
    """Filter `list_site_columns`' result down to {internal_name: term_set_id}
    for just the Managed Metadata columns."""
    result = {}
    for col in columns:
        term_column = col.get("termColumn")
        if term_column and term_column.get("termSetId"):
            result[col["name"]] = term_column["termSetId"]
    return result
