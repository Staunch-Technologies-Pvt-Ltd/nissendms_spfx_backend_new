from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable

DRAWING = "Drawing"
MANUAL = "Manual"
GROUPS = (DRAWING, MANUAL)

_GROUP_KEYWORDS = {
    DRAWING: {
        "drawing", "drawings", "dwg", "diagram", "diagrams", "layout", "arrangement",
        "single", "line", "profile", "plan", "plans", "isometric", "piping", "electrical",
        "fabrication", "hull", "structural", "outline", "schematic"
    },
    MANUAL: {
        "manual", "manuals", "instruction", "instructions", "maintenance", "operation",
        "operations", "procedure", "procedures", "handbook", "guide", "guides",
        "handbook", "startup", "shutdown", "commissioning", "troubleshooting"
    },
}


def _normalize_name(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(part for part in value.split() if part)


def _tokenize(value: str | None) -> set[str]:
    if not value:
        return set()
    text = _normalize_name(value)
    return {token for token in text.split() if len(token) >= 2}


def _candidate_vessel_name(source_folder: str, destination_vessels: Iterable[str]) -> str | None:
    candidates = [
        v.strip() for v in destination_vessels if isinstance(v, str) and v.strip()
    ]
    if not candidates:
        return source_folder.strip("/") or None

    normalized_targets = { _normalize_name(v): v for v in candidates }
    source_name = _normalize_name(source_folder)
    for vessel in candidates:
        vessel_norm = _normalize_name(vessel)
        if vessel_norm and vessel_norm in source_name:
            return vessel
        if source_name and source_name in vessel_norm:
            return vessel
    for vessel in candidates:
        vessel_norm = _normalize_name(vessel)
        tokens = set(vessel_norm.split())
        if source_name and tokens and not set(source_name.split()) - tokens:
            return vessel
    return source_folder.strip("/") or None


def _resolve_vessel(source_folder: str, destination_vessels: Iterable[str]) -> tuple[str, str]:
    vessels = [v.strip() for v in destination_vessels if isinstance(v, str) and v.strip()]
    if not vessels:
        return source_folder.strip("/") or "UNKNOWN", "source_folder"

    normalized_vessels = { _normalize_name(v): v for v in vessels }
    source_name = _normalize_name(source_folder)

    if source_name in normalized_vessels:
        return normalized_vessels[source_name], "existing_vessel"

    for vessel in vessels:
        vessel_norm = _normalize_name(vessel)
        if vessel_norm and vessel_norm in source_name:
            return vessel, "existing_vessel"
        if source_name and source_name in vessel_norm:
            return vessel, "existing_vessel"

    if source_folder and source_folder.strip("/"):
        return source_folder.strip("/"), "source_folder"

    return vessels[0], "source_folder"


def load_destination_taxonomy(paths: Iterable[str]) -> dict:
    taxonomy = {DRAWING: [], MANUAL: []}
    for path in paths:
        if not path:
            continue
        normalized = path.strip("/")
        parts = [part.strip() for part in normalized.split("/") if part.strip()]
        if len(parts) < 2:
            continue
        group_index = next(
            (
                index for index, part in enumerate(parts)
                if _normalize_name(part) in {"drawing", "drawings", "manual", "manuals"}
            ),
            None,
        )
        if group_index is None or len(parts) <= group_index + 1:
            continue
        category_name = parts[-1]
        group_name = _normalize_name(parts[group_index])
        group = DRAWING if group_name in {"drawing", "drawings"} else MANUAL
        if category_name not in taxonomy[group]:
            taxonomy[group].append(category_name)
    return taxonomy


def resolve_category_path(group: str, category: str, paths: Iterable[str]) -> str | None:
    """Resolve a classified category to its existing live folder path.

    Group folders may be nested below a shared wrapper, so the path is never
    reconstructed from hard-coded ``Drawings/`` or ``Manuals/`` prefixes.
    """
    matches = []
    for path in paths:
        parts = [part.strip() for part in path.strip("/").split("/") if part.strip()]
        group_index = next(
            (index for index, part in enumerate(parts)
             if (_normalize_name(part) in {"drawing", "drawings"} and group == DRAWING)
             or (_normalize_name(part) in {"manual", "manuals"} and group == MANUAL)),
            None,
        )
        if group_index is None or len(parts) <= group_index + 1:
            continue
        if parts[-1].casefold() == category.casefold():
            matches.append(path.strip("/"))
    return matches[0] if len(matches) == 1 else None


def _group_evidence_score(
    text: str,
    filename: str,
    source_folder: str,
    taxonomy: dict[str, list[str]],
) -> dict[str, float]:
    combined = f"{filename} {text} {source_folder}".upper()
    tokens = set(_tokenize(f"{filename} {text} {source_folder}"))
    scores = {group: 0.0 for group in GROUPS}

    for group, keywords in _GROUP_KEYWORDS.items():
        matched = tokens & keywords
        if matched:
            scores[group] += 0.65 * len(matched)
            if group == MANUAL and any(term in combined.lower() for term in ("manual", "operation", "maintenance", "procedure", "guide")):
                scores[group] += 0.8
            if group == DRAWING and any(term in combined.lower() for term in ("drawing", "diagram", "layout", "arrangement", "single line", "plan", "profile")):
                scores[group] += 0.8

        # Existing category labels inform Group selection, but category
        # selection itself still happens only after this step.
        for category in taxonomy.get(group, []):
            overlap = len(_tokenize(category) & tokens)
            if overlap >= 2:
                scores[group] += 3.0 * overlap

    if "manual" in combined.lower() and "drawing" not in combined.lower():
        scores[MANUAL] += 1.3
    if ("drawing" in combined.lower() or "diagram" in combined.lower()) and "manual" not in combined.lower():
        scores[DRAWING] += 1.3

    return scores


def _classify_group(
    text: str,
    filename: str,
    source_folder: str,
    taxonomy: dict[str, list[str]],
) -> tuple[str | None, float, str]:
    scores = _group_evidence_score(text, filename, source_folder, taxonomy)
    combined = f"{filename} {text} {source_folder}"
    group_choice = max(scores, key=scores.get)
    best_score = scores[group_choice]
    runner_up = sorted(scores.values(), reverse=True)[1]

    if best_score <= 0.1:
        reason = "No direct Group evidence; selected the first valid Group from the live taxonomy."
    elif best_score <= runner_up + 0.1:
        reason = "Group evidence was ambiguous; selected the highest-ranked valid Group."
    else:
        reason = f"Selected {group_choice} using document, source, and live taxonomy evidence."
    confidence = min(1.0, best_score / 3.5) if best_score else 0.0
    return group_choice, confidence, reason


def _category_score(category_name: str, group: str, text: str, filename: str, source_folder: str, taxonomy: dict[str, list[str]]) -> float:
    category_norm = _normalize_name(category_name)
    if not category_norm:
        return 0.0

    doc_text = f"{filename} {text} {source_folder}".lower()
    doc_tokens = _tokenize(doc_text)
    category_tokens = _tokenize(category_norm)
    score = 0.0

    if category_norm in _normalize_name(doc_text):
        score += 3.0

    overlap = len(category_tokens & doc_tokens)
    score += overlap * 0.9

    if overlap == 0:
        # Semantic fallback: compare word similarity and phrase closeness for the valid taxonomy only.
        doc_words = set(doc_tokens)
        for token in category_tokens:
            for other in doc_words:
                if token == other:
                    score += 0.3
                else:
                    score += 0.15 * SequenceMatcher(None, token, other).ratio()

    if any(word in doc_text for word in ["main engine", "engine", "propulsion", "maintenance"] ) and "main engine" in category_norm:
        score += 0.9
    if any(word in doc_text for word in ["electrical", "switchboard", "distribution", "circuit"]) and "electrical" in category_norm:
        score += 1.6
    if any(word in doc_text for word in ["fire", "alarm", "sprinkler", "deluge"]) and "fire" in category_norm:
        score += 1.8
    if any(word in doc_text for word in ["piping", "pipe", "valve", "isometric"]) and "piping" in category_norm:
        score += 1.5
    if any(word in doc_text for word in ["hull", "structure", "shell"]) and "hull" in category_norm:
        score += 1.4

    # Ensure the category belongs to the chosen group in the authoritative taxonomy.
    if category_name not in taxonomy.get(group, []):
        score -= 100.0

    return score


def _select_category_for_group(group: str, text: str, filename: str, source_folder: str, taxonomy: dict[str, list[str]]) -> tuple[str | None, float, str]:
    candidates = taxonomy.get(group, [])
    if not candidates:
        return None, 0.0, f"No valid categories are available in the {group} group."

    scored = []
    for category in candidates:
        score = _category_score(category, group, text, filename, source_folder, taxonomy)
        scored.append((category, score))

    best_category, best_score = max(scored, key=lambda item: item[1])
    second_best = sorted((s for _, s in scored), reverse=True)[1] if len(scored) > 1 else 0.0
    if best_score <= 0.75:
        reason = f"No exact match; selected the closest valid {group} category using fallback similarity."
    elif best_score <= second_best + 0.2:
        reason = f"Category evidence was close; selected the highest-ranked valid {group} category."
    else:
        reason = f"Selected the closest valid {group} category using contextual similarity."
    return best_category, min(1.0, best_score / 4.5), reason


def classify_document(
    *,
    filename: str,
    document_text: str,
    source_folder: str,
    destination_vessels: Iterable[str],
    taxonomy: dict[str, list[str]] | None = None,
    selected_vessel: str | None = None,
) -> dict:
    """Return a single, authoritative classification payload for one file.

    The returned payload is intentionally shaped to be applied consistently to:
    - group
    - category
    - vessel
    - destination folder
    - term store tags
    - file rename logic
    """
    taxonomy = taxonomy or load_destination_taxonomy([])
    if not taxonomy:
        taxonomy = {DRAWING: [], MANUAL: []}

    group, group_confidence, group_reason = _classify_group(
        document_text, filename, source_folder, taxonomy
    )
    if group is None:
        return {
            "group": None,
            "category": None,
            "vessel": selected_vessel or _resolve_vessel(source_folder, destination_vessels)[0],
            "group_confidence": 0.0,
            "category_confidence": 0.0,
            "category_reason": group_reason,
            "vessel_source": "source_folder",
            "status": "review_required",
            "reason": group_reason,
            "destination_path": None,
            "termStoreTagsResolved": False,
            "taggingSucceeded": False,
        }

    category, category_confidence, category_reason = _select_category_for_group(
        group, document_text, filename, source_folder, taxonomy
    )
    if category is None:
        return {
            "group": group,
            "category": None,
            "vessel": selected_vessel or _resolve_vessel(source_folder, destination_vessels)[0],
            "group_confidence": group_confidence,
            "category_confidence": 0.0,
            "category_reason": category_reason,
            "vessel_source": "source_folder",
            "status": "review_required",
            "reason": category_reason,
            "destination_path": None,
            "termStoreTagsResolved": False,
            "taggingSucceeded": False,
        }

    vessel, vessel_source = _resolve_vessel(source_folder, destination_vessels)
    if selected_vessel:
        vessel = selected_vessel
        vessel_source = "selected_vessel"

    return {
        "group": group,
        "category": category,
        "vessel": vessel,
        "group_confidence": group_confidence,
        "category_confidence": category_confidence,
        "category_reason": category_reason,
        "vessel_source": vessel_source,
        "status": "classified",
        "reason": "Classification complete and valid taxonomy category selected.",
        "destination_path": f"{group}s/{category}",
        "termStoreTagsResolved": False,
        "taggingSucceeded": False,
    }
