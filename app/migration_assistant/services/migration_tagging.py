"""Managed Metadata (Term Store) tagging for the same-site migration mode —
applies Category / Sub-category / Vessel Name terms to a moved file's list
item on Confirm Move (see services/migration_mover.py).

Built entirely from the same primitives Site-to-Site migration already uses
(`graph/term_store.py`, `graph/sharepoint_rest.py`) — see that mode's
`services/term_mapping.py` for the two-site term-mapping case this is a
simpler cousin of. There's no "source term" here (these are legacy files with
no existing Managed Metadata value to carry over): the label is simply this
item's own classification result (its category/sub-category/vessel name), and
this module looks up the matching term in the configured field's bound term
set and writes it.

Term-set ids are configured explicitly (config/settings.py's
`category_term_set_id` etc.), not discovered dynamically from the column's
`termColumn` facet the way `services/term_mapping.py` does for Site-to-Site —
that facet is known to be missing for this tenant's own Category/Group/
Vessel Name columns (see term_mapping.py's docstring), so relying on it here
would silently tag nothing.
"""
from __future__ import annotations

from ..config import settings
from ..graph import fields as gf
from ..graph import sharepoint_rest as sprest
from ..graph import term_store

# tag key -> (field internal name, bound term-set id) setting pair.
_FIELD_SETTINGS = {
    "group": ("group_field_name", "group_term_set_id"),
    "category": ("category_field_name", "category_term_set_id"),
    "subcategory": ("subcategory_field_name", "subcategory_term_set_id"),
    "vessel": ("vessel_field_name", "vessel_term_set_id"),
}


async def apply_term_tags(
    site_url: str, list_title: str, list_item_id: int, tags: dict[str, str | dict | None],
    bindings: dict[str, dict] | None = None,
) -> list[dict]:
    """Look up and write each of `tags` (keys among "category", "subcategory",
    "vessel"; values are the label to tag with, e.g. "Drawings" or "Peissy",
    or an already-resolved {"label", "id"} term from `graph.term_store`)
    as a Managed Metadata value on the given list item. Returns a per-field
    report: [{"field", "status": "applied"|"unmapped"|"skipped"|"error", "detail"}].
    Never raises for one field's failure — mirrors term_mapping.py's own
    never-raise contract so the caller can aggregate this into the job's
    overall counters without special-casing tagging failures."""
    report: list[dict] = []
    form_values: list[dict] = []

    for key, value in tags.items():
        field_setting, term_set_setting = _FIELD_SETTINGS.get(key, (None, None))
        binding = (bindings or {}).get(key, {})
        field_name = binding.get("field") or (getattr(settings, field_setting) if field_setting else "")
        term_set_id = binding.get("term_set_id") or (getattr(settings, term_set_setting) if term_set_setting else "")
        label = value.get("label") if isinstance(value, dict) else value

        if not label:
            report.append({"field": key, "status": "skipped", "detail": "No value to tag"})
            continue
        if not field_name:
            report.append({"field": key, "status": "skipped", "detail": f"'{key}' tagging is not configured"})
            continue

        # An already-resolved term is used as-is: re-looking it up by label
        # could land on a different term with the same label under another
        # parent (see graph/term_store.find_term's ancestor_id).
        match = value if isinstance(value, dict) and value.get("id") else None
        if match is None:
            if not term_set_id:
                report.append({"field": key, "status": "skipped", "detail": f"'{key}' tagging is not configured"})
                continue
            match = await term_store.find_term(term_set_id, term_id=None, label=label)
        if match is None:
            report.append({
                "field": key, "status": "unmapped",
                "detail": f"'{label}' was not found in the configured term set for '{key}'",
            })
            continue

        form_values.append({"FieldName": field_name, "FieldValue": f"{match['label']}|{match['id']}"})
        report.append({"field": key, "status": "applied", "detail": f"Applied term '{match['label']}'"})

    if form_values:
        try:
            results = await sprest.validate_update_list_item(site_url, list_title, list_item_id, form_values)
            errors_by_field = {
                r.get("FieldName"): r.get("ErrorMessage")
                for r in results
                if r.get("ErrorMessage") and r.get("HasException")
            }
            for entry in report:
                field_name = _FIELD_SETTINGS.get(entry["field"], (None, None))[0]
                internal_name = (bindings or {}).get(entry["field"], {}).get("field") or (getattr(settings, field_name) if field_name else None)
                if internal_name in errors_by_field and entry["status"] == "applied":
                    entry["status"] = "error"
                    entry["detail"] = errors_by_field[internal_name]
        except Exception as e:
            # The whole write failed (digest/auth problem, etc.) rather than
            # one field — mark everything we attempted as failed so it's
            # visible instead of silently reported "applied".
            for entry in report:
                if entry["status"] == "applied":
                    entry["status"] = "error"
                    entry["detail"] = f"Write failed: {e}"

    return report


async def verify_term_tags_via_graph(
    drive_id: str, item_id: str, expected: dict[str, dict], bindings: dict[str, dict] | None = None,
) -> list[dict]:
    """Read the item back through Graph and confirm each field now holds the
    exact term that was written. `expected` is {tag key: {"label", "id"}}.

    Graph's `fields` facet returns `{Label, TermGuid}` for this tenant's
    taxonomy columns (see graph/fields.py), so the term's *id* can be
    compared rather than its label — and unlike the SharePoint REST read,
    this works with the Graph-only permissions the app already has."""
    report: list[dict] = []
    item_fields = await gf.get_item_fields(drive_id, item_id)
    for key, term in expected.items():
        field_setting, _ = _FIELD_SETTINGS.get(key, (None, None))
        field_name = (bindings or {}).get(key, {}).get("field") or (getattr(settings, field_setting) if field_setting else "")
        if not term or not field_name:
            report.append({"field": key, "status": "error", "detail": "Required tag is not configured"})
            continue
        actual = item_fields.get(field_name)
        actual_id = actual.get("TermGuid", "") if isinstance(actual, dict) else ""
        if actual_id.casefold() == str(term["id"]).casefold():
            report.append({"field": key, "status": "verified", "detail": f"Verified term '{term['label']}'"})
        else:
            report.append({
                "field": key, "status": "error",
                "detail": f"Expected '{term['label']}' ({term['id']}), found {actual!r}",
            })
    return report


async def verify_term_tags(
    site_url: str, list_title: str, list_item_id: int, tags: dict[str, str | None],
    bindings: dict[str, dict] | None = None,
) -> list[dict]:
    """Read the destination taxonomy values back and compare their labels."""
    report: list[dict] = []
    for key, expected in tags.items():
        field_setting, _ = _FIELD_SETTINGS.get(key, (None, None))
        field_name = (bindings or {}).get(key, {}).get("field") or (getattr(settings, field_setting) if field_setting else "")
        if not expected or not field_name:
            report.append({"field": key, "status": "error", "detail": "Required tag is not configured"})
            continue
        try:
            actual = await sprest.get_taxonomy_field_value(
                site_url, list_title, list_item_id, field_name
            )
        except Exception as exc:
            report.append({"field": key, "status": "error", "detail": f"Verification read failed: {exc}"})
            continue
        if not actual or actual.get("label", "").casefold() != expected.casefold():
            report.append({
                "field": key,
                "status": "error",
                "detail": f"Expected '{expected}', found '{(actual or {}).get('label', '')}'",
            })
        else:
            report.append({"field": key, "status": "verified", "detail": f"Verified term '{expected}'"})
    return report
