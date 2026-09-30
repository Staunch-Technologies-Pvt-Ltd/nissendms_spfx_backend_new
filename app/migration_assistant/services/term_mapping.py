"""Per-item metadata migration for Site-to-Site copies: reads every column
value off the source list item and applies it to the destination list item,
resolving Managed Metadata (Term Store) columns to real destination terms
instead of writing plain text.

Column values, other than Managed Metadata, are read via Graph
(`graph/fields.py`) and written via SharePoint REST's
`ValidateUpdateListItem` (`graph/sharepoint_rest.py`) — see that module's
docstring for why the write side needs REST rather than Graph (confirmed
again directly: Graph's `PATCH .../listItem/fields` rejects a taxonomy value
in every payload shape tried, with a bare 400 invalidRequest).

A Managed Metadata field is identified by the *shape of its value* — a dict
containing `TermGuid` — not by the column definition's `termColumn` facet.
This tenant's columns don't expose that facet at all for its Managed
Metadata fields (confirmed: `Category`/`Group`/`Vessel Name` all come back
with no type facet whatsoever from `/columns`, just a bare `defaultValue`),
even though Graph's `listItem/fields` facet *does* return the field's real
`{Label, TermGuid, WssId}` object. Relying on the column facet alone would
silently misclassify every one of these as an ordinary field and — worse
than writing the label as plain text — send Python's raw stringified dict
to SharePoint. Detecting from the value itself sidesteps the unreliable
facet entirely and needs no extra SharePoint REST round-trip to read the
term (Graph already handed over the real id).

The *destination* column's bound term set (needed to know which term set to
search when placing the term there) still comes from the column-definition
facet (`graph/term_store.get_column_term_set_id`-style lookup via
`gf.taxonomy_columns`) — there's no value to inspect on a column that has no
value yet, so this remains the best available signal for that side, with
the same facet-reliability caveat.

No term is ever invented at the destination: a source term that can't be
found in the destination's bound term set (by id, then by exact label) is
left unwritten and reported as needing attention — see `migrate_item_fields`'s
return shape.
"""
from __future__ import annotations

from ..graph import fields as gf
from ..graph import sharepoint_rest as sprest
from ..graph import term_store


def _is_taxonomy_value(value: object) -> bool:
    """A Managed Metadata field's value, as returned by Graph's `fields`
    facet, is a dict with at least a `TermGuid` key — this is the reliable
    signal in this tenant, not the column definition's (missing) facet."""
    return isinstance(value, dict) and "TermGuid" in value


# Columns that are never real document metadata — SharePoint system/computed
# fields that show up in every list's `fields` facet and would otherwise be
# copied as noisy, meaningless "metadata" (or rejected by ValidateUpdateListItem
# since several of these are read-only).
_SYSTEM_FIELDS = {
    "ContentType", "Attachments", "Edit", "LinkTitleNoMenu", "LinkTitle",
    "ItemChildCount", "FolderChildCount", "_ComplianceFlags", "_ComplianceTag",
    "_ComplianceTagWrittenTime", "_ComplianceTagUserId", "AppAuthor", "AppEditor",
    "Created", "Modified", "Author", "Editor", "id", "ContentTypeId", "Title",
    "OData__UIVersionString", "_CheckinComment",
}


async def migrate_item_fields(
    *,
    source_drive_id: str,
    source_item_id: str,
    dest_site_url: str,
    dest_site_id: str,
    dest_drive_id: str,
    dest_item_id: str,
) -> list[dict]:
    """Copy every non-system column value from the source file's list item
    to the destination file's list item (already copied by
    `services/site_to_site_mover.py` by this point). Returns a per-field
    report: [{"field", "status": "applied"|"label_matched"|"unmapped"|"error"|"skipped", "detail"}].
    Never raises for one field's failure — the caller aggregates this into
    the job's overall metadata/Managed-Metadata counters."""
    dest_list_title = await _drive_list_title(dest_drive_id)
    dest_list_item_id = await gf.get_list_item_id(dest_drive_id, dest_item_id)

    source_fields = await gf.get_item_fields(source_drive_id, source_item_id)
    dest_taxonomy_columns = gf.taxonomy_columns(await gf.list_site_columns(dest_site_id))

    report: list[dict] = []
    form_values: list[dict] = []
    expected_taxonomy_labels: dict[str, str] = {}

    for name, value in source_fields.items():
        if name in _SYSTEM_FIELDS or name.startswith("_") or value in (None, ""):
            continue

        if _is_taxonomy_value(value):
            term_id = value.get("TermGuid")
            label = value.get("Label", "")
            if name not in dest_taxonomy_columns:
                report.append({
                    "field": name,
                    "kind": "managed_metadata",
                    "status": "unmapped",
                    "detail": f"Destination has no Managed Metadata column named '{name}' to receive this term",
                })
                continue
            match = await term_store.find_term(
                dest_taxonomy_columns[name], term_id=term_id, label=label, site_id=dest_site_id
            )
            if match is None:
                report.append({
                    "field": name,
                    "kind": "managed_metadata",
                    "status": "unmapped",
                    "detail": f"Source term '{label}' not found in destination term set",
                })
                continue
            form_values.append({"FieldName": name, "FieldValue": f"{match['label']}|{match['id']}"})
            expected_taxonomy_labels[name] = match["label"]
            report.append({
                "field": name,
                "kind": "managed_metadata",
                "status": "applied" if match["matched_by"] == "id" else "label_matched",
                "detail": f"Applied term '{match['label']}'"
                + ("" if match["matched_by"] == "id" else " (matched by label, not shared term identity)"),
            })
        elif isinstance(value, dict):
            # An object-shaped value that isn't a recognized taxonomy shape
            # (no TermGuid) — never stringify an arbitrary dict into a text
            # field; report it instead of guessing.
            report.append({
                "field": name,
                "kind": "plain",
                "status": "error",
                "detail": f"Unrecognized structured field value, not migrated: {value!r}",
            })
        else:
            form_values.append({"FieldName": name, "FieldValue": str(value)})
            report.append({"field": name, "kind": "plain", "status": "applied", "detail": "Copied as-is"})

    if form_values:
        try:
            results = await sprest.validate_update_list_item(
                dest_site_url, dest_list_title, dest_list_item_id, form_values
            )
            errors_by_field = {
                r.get("FieldName"): r.get("ErrorMessage")
                for r in results
                if r.get("ErrorMessage") and r.get("HasException")
            }
            for entry in report:
                if entry["field"] in errors_by_field and entry["status"] != "error":
                    entry["status"] = "error"
                    entry["detail"] = errors_by_field[entry["field"]]

            # A successful REST response is not sufficient evidence that
            # SharePoint persisted a taxonomy value; read each managed field
            # back before reporting metadata migration as complete.
            for field_name, expected_label in expected_taxonomy_labels.items():
                try:
                    actual = await sprest.get_taxonomy_field_value(
                        dest_site_url, dest_list_title, dest_list_item_id, field_name
                    )
                except Exception as exc:
                    report.append({
                        "field": field_name,
                        "kind": "managed_metadata",
                        "status": "error",
                        "detail": f"Verification read failed: {exc}",
                    })
                    continue
                if not actual or actual.get("label", "").casefold() != expected_label.casefold():
                    report.append({
                        "field": field_name,
                        "kind": "managed_metadata",
                        "status": "error",
                        "detail": f"Expected '{expected_label}', found '{(actual or {}).get('label', '')}'",
                    })
                else:
                    report.append({
                        "field": field_name,
                        "kind": "managed_metadata",
                        "status": "verified",
                        "detail": f"Verified term '{expected_label}'",
                    })
        except Exception as e:
            # The whole write failed (e.g. digest/auth problem) rather than
            # one field — mark everything we attempted as failed so it's
            # visible instead of silently reported "applied".
            for entry in report:
                if entry["status"] in ("applied", "label_matched"):
                    entry["status"] = "error"
                    entry["detail"] = f"Write failed: {e}"

    return report


async def _drive_list_title(drive_id: str) -> str:
    from ..graph import drive as gd

    return await gd.get_drive_list_title(drive_id)
