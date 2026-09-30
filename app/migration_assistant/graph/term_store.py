"""Read-only access to the tenant's Managed Metadata Term Store, via Graph's
`/termStore` API (this is a *different* API surface from driveItems/lists —
it manages term sets/terms themselves, not list-item field values).

Used only to answer "does this term set contain this term id/label" for
`services/term_mapping.py` and `services/migration_tagging.py` — never to
create or edit terms (no write support here; the POC deliberately never
creates new Term Store terms, see CLAUDE.md / the migration plan).

Two things about this tenant that the endpoints below encode:

- Graph v1.0 exposes the Term Store **only under a site**
  (`/sites/{siteId}/termStore/...`). The tenant-root `/termStore/...` path
  returns `400 Resource not found for the segment 'termStore'`, so every
  call here is site-scoped; callers that don't care which site get the
  destination site's id.
- Labels in the store don't always match the text being looked up: folder
  names say "Drawing" where the term is "Drawings", and the vessel term is
  spelled "Maersk EI Banco" (capital i) where the folder has "Maersk El
  Banco" (lowercase L). `find_term` falls back through progressively looser
  comparisons and reports which one matched, rather than reporting a term
  that plainly exists as missing.
"""
from __future__ import annotations

import re
import unicodedata

from .client import graph

_MAX_DEPTH = 5  # terms can nest; flatten a few levels rather than assume a flat term set

_terms_cache: dict[tuple[str, str], list[dict]] = {}  # (base, term_set_id) -> [{"id", "label", ...}]
_default_site_id: str | None = None

# Visually identical (or near enough) characters folded together, so a lookup
# isn't defeated by a typo no human reading the two strings would notice.
_CONFUSABLES = str.maketrans({"l": "i", "1": "i", "|": "i", "0": "o"})


async def _base(site_id: str | None = None) -> str:
    global _default_site_id
    if site_id:
        return f"/sites/{site_id}/termStore"
    if _default_site_id is None:
        from ..services import migration_common  # local import: avoids a graph <-> services cycle

        from .site import get_site_id

        site = migration_common.get_destination_site()
        _default_site_id = await get_site_id(site["hostname"], site["site_path"])
    return f"/sites/{_default_site_id}/termStore"


def _labels(term: dict) -> list[str]:
    return [label["name"] for label in (term.get("labels") or []) if label.get("name")]


def _default_label(term: dict) -> str:
    labels = term.get("labels") or []
    return next((l["name"] for l in labels if l.get("isDefault")), labels[0]["name"] if labels else "")


def _normalize(value: str) -> str:
    """Case/whitespace/punctuation/width-insensitive form of a label."""
    folded = unicodedata.normalize("NFKD", value).casefold()
    return re.sub(r"[^a-z0-9]+", "", folded)


def _loose(value: str) -> str:
    """`_normalize` plus plural and look-alike-character folding."""
    normalized = _normalize(value).translate(_CONFUSABLES)
    return normalized[:-1] if normalized.endswith("s") and len(normalized) > 3 else normalized


async def _walk_children(
    base: str, term_set_id: str, node_url: str, depth: int, path: str,
    ancestors: tuple[str, ...], out: list[dict],
) -> None:
    if depth >= _MAX_DEPTH:
        return
    data = await graph().get(node_url)
    for term in data.get("value", []):
        label = _default_label(term)
        term_path = f"{path} > {label}" if path else label
        out.append({
            "id": term["id"],
            "label": label,
            "labels": _labels(term),
            "depth": depth,
            "path": term_path,
            "ancestors": ancestors,
        })
        await _walk_children(
            base, term_set_id, f"{base}/sets/{term_set_id}/terms/{term['id']}/children",
            depth + 1, term_path, ancestors + (term["id"],), out,
        )


async def get_term_set_terms(
    term_set_id: str, *, site_id: str | None = None, force_refresh: bool = False
) -> list[dict]:
    """Every term in a term set (flattened, depth-limited), as
    [{"id", "label", "labels", "depth", "path", "ancestors"}, ...]."""
    base = await _base(site_id)
    key = (base, term_set_id)
    if not force_refresh and key in _terms_cache:
        return _terms_cache[key]
    out: list[dict] = []
    await _walk_children(base, term_set_id, f"{base}/sets/{term_set_id}/children", 0, "", (), out)
    _terms_cache[key] = out
    return out


def _unique(matches: list[dict]) -> dict | None:
    """A looser comparison is only trustworthy when it picks out exactly one
    term — two terms folding onto the same form can't be told apart, and
    guessing would tag the wrong one."""
    ids = {m["id"] for m in matches}
    return matches[0] if len(ids) == 1 else None


async def find_term(
    term_set_id: str,
    *,
    term_id: str | None,
    label: str | None,
    site_id: str | None = None,
    max_depth: int | None = None,
    ancestor_id: str | None = None,
) -> dict | None:
    """Resolve a term within `term_set_id`, trying in order: exact id (the
    reliable case — the same tenant Term Store means a term keeps its id
    wherever it's used), exact label, normalized label (case/space/
    punctuation), then loose label (plural + look-alike characters). Every
    label comparison considers a term's alternate labels, not just its
    default one. Returns None if nothing matches — the term genuinely isn't
    in this term set — or if a looser comparison matched more than one term
    ambiguously.

    `max_depth=0` restricts the search to top-level terms (the Group
    column's terms are this term set's roots). `ancestor_id` restricts it to
    one term's descendants — the same label can exist under two parents
    ("Electrical" sits under both Drawings and Manuals), and the already
    resolved parent is what tells them apart."""
    terms = await get_term_set_terms(term_set_id, site_id=site_id)
    if max_depth is not None:
        terms = [t for t in terms if t["depth"] <= max_depth]
    if ancestor_id:
        terms = [t for t in terms if ancestor_id in t["ancestors"]]

    if term_id:
        by_id = next((t for t in terms if t["id"] == term_id), None)
        if by_id:
            return {**by_id, "matched_by": "id"}
    if not label:
        return None

    for how, transform in (
        ("label", lambda v: v.strip().casefold()),
        ("normalized_label", _normalize),
        ("loose_label", _loose),
    ):
        wanted = transform(label)
        if not wanted:
            continue
        match = _unique([t for t in terms if any(transform(l) == wanted for l in t["labels"])])
        if match:
            return {**match, "matched_by": how}
    return None
