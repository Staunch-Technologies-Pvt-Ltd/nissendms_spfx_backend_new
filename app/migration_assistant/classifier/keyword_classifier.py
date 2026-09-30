"""Deterministic, keyword-based document classifier — no local LLM involved.

Ship documents in this dataset are unusually well-structured for this: the
equipment/document type is stated explicitly in the text (e.g. "SATELLITE
COMPASS", "CARGO CRANE MANUAL"), and the destination folder names are
similarly descriptive ("Cargo Crane Manual", "Fire Alarm Manual"). Matching
words between the two is fast (milliseconds, no GPU/network involved),
fully deterministic (same input always gives the same output — no retries,
no timeouts, nothing to "fail"), and transparent (you can see exactly which
words drove the match).

Two layers of filtering keep this from producing false positives:

1. A fixed stoplist of generic filing/document vocabulary ("manual", "test",
   "record", "control", "plan", ...) that never counts as a match by itself,
   no matter how rare it happens to be across THIS vessel's folder names —
   these are common English words that can appear in any document
   incidentally, so their rarity among folder names is not real evidence.
2. Among the remaining (genuinely domain-specific) words, a TF-IDF-style
   weighting: a word that appears in only one or two folder names (e.g.
   "compass", "ballast", "crane") is strong, specific evidence. A word
   shared by many folder names is weaker and contributes proportionally
   less — this part adapts automatically to whatever this vessel's actual
   folder names are, no maintenance needed.

A match only counts at all if at least one matched word is genuinely
distinctive (appears in a small minority of folders) — matches built purely
from multiple weak/common words are rejected rather than allowed to add up
to false confidence.

A third layer addresses a real failure found in testing: these documents
consistently state their actual subject prominently near the very top (e.g.
"LEAD-ACID MARINE BATTERIES" within the first few hundred characters), but a
multi-page document can incidentally mention unrelated equipment later on
(specs, cross-references, appendices) — matching against the *whole* raw
text let those incidental later mentions cause false positives (a battery
document matched to "Cooling Water System" because "water"/"cooling"
happened to co-occur somewhere deep in the text). So header words (the
first ~500 characters, where the subject reliably lives) count at full
weight; the same words found only later in the body count at a small
fraction of that — still "reading the whole document," just not letting a
buried coincidence outweigh the stated subject.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict

_WORD_RE = re.compile(r"[a-zA-Z]{3,}")

# Generic filing/document vocabulary — never trusted as a category signal by
# itself, regardless of how rare it happens to be among this vessel's folder
# names, since these are common English words any document could incidentally
# contain. Includes generic-in-this-domain words too ("ship"/"vessel" appear
# in essentially every document in a ship management system, so they carry
# no discriminating signal despite possibly being rare in folder *names*).
_STOPWORDS = {
    "and", "the", "for", "with", "other", "official", "general", "information",
    "system", "systems", "test", "tests", "record", "records", "list", "lists", "part",
    "parts", "control", "controls", "arrangement", "arrangements", "operation",
    "operations", "operating", "maintenance", "report", "reports", "book",
    "books", "diagram", "diagrams", "sheet", "sheets", "data", "index",
    "instruction", "instructions", "specification", "specifications", "shown",
    "document", "documents", "finished", "requested", "handle", "strict",
    "confidence", "used", "copied", "reproduced", "without", "express",
    "permission", "property", "exclusive", "ship", "vessel", "main", "file",
    "files", "type", "types", "line", "lines", "unit", "units",
}

# Manual/drawing words are often the strongest category hint for the filing
# destination itself, so they must still count when they appear explicitly in
# a document name or in OCR text. They are kept out of the generic stopwords
# list and only treated as special hints here, rather than being discarded
# before matching.
_TYPE_HINT_WORDS = {"manual", "drawing", "plan"}


def _normalize_type_hint(token: str) -> str:
    normalized = token.lower()
    if normalized.endswith("s"):
        singular = normalized[:-1]
        if singular in _TYPE_HINT_WORDS:
            return singular
    if normalized in _TYPE_HINT_WORDS:
        return normalized
    return normalized

# A lone single-keyword match is only trusted if the word is at least this
# long — short words are disproportionately likely to be generic English
# rather than a specific equipment/system name, even when (by coincidence)
# they only appear in one folder name.
_MIN_LONE_KEYWORD_LENGTH = 6


def _tokenize(text: str, *, include_type_hints: bool = False) -> set[str]:
    tokens = {_normalize_type_hint(w) for w in _WORD_RE.findall(text or "")}
    if include_type_hints:
        return tokens - (_STOPWORDS - _TYPE_HINT_WORDS)
    return tokens - _STOPWORDS


def _path_keywords(path: str) -> set[str]:
    """Only the last two path segments — the specific descriptor (e.g.
    "Cargo/Cargo Crane Manual") — not the top-level segments ("Drawings and
    Manuals/Manuals") that are shared by nearly every path and would just
    add noise. Keep document-type hints like "manual" and "drawing" here as
    real folder-name evidence, because a manual/drawing document often needs
    that exact category to be chosen."""
    segments = [s for s in path.split("/") if s]
    specific = segments[-2:] if len(segments) >= 2 else segments
    return _tokenize(" ".join(specific), include_type_hints=True)


# A matched word only counts as "distinctive enough to trust" if it appears
# in at most this fraction of all folder paths — otherwise a match is only
# built from common words and is rejected rather than allowed to accumulate
# false confidence.
_MAX_DISTINCTIVE_DOC_FREQ_RATIO = 0.15

# How many characters count as "the header" — where these documents reliably
# state their actual subject (confirmed across many real samples: a short
# preamble of dates/list numbers, then the equipment name in caps, all well
# within this window).
_HEADER_CHARS = 500
# A word found only in the body (never in the header) counts for this small
# fraction of its normal weight — still contributes, but can't outweigh a
# real header-stated subject on its own.
_BODY_ONLY_WEIGHT = 0.15


def _fallback_branch_path(doc_tokens: set[str], hierarchy_paths: list[str]) -> str | None:
    """If a document plainly says it is a manual or drawing, force it into
    its branch even when no deeper subcategory can be proven. This is a
    deliberate mandatory rule for this project: a file with a clear
    manual/drawing signal must not stay in "needs_review" just because its
    specific subsection is unknown.

    The branch-named segment isn't always the first path segment — some
    destinations wrap every category under a shared top folder (e.g.
    "Drawings and Manuals/Drawings/Hull", not just "Drawings/Hull") — so
    this looks for a segment starting with "drawing"/"manual" at whatever
    depth it actually occurs, and returns the path up through that segment
    (never a segment past it, and never the shared wrapper alone)."""
    branch_matches: dict[str, str] = {}
    for path in hierarchy_paths:
        segments = [s for s in path.split("/") if s]
        for i, seg in enumerate(segments):
            # Exact word match, not startswith/substring — a wrapper folder
            # like "Drawings and Manuals" would otherwise also match
            # (it literally starts with "Drawing") despite not being either
            # branch itself.
            low = seg.strip().lower()
            if low in ("drawing", "drawings") and "drawing" in doc_tokens:
                branch_matches.setdefault("drawings", "/".join(segments[: i + 1]))
                break
            if low in ("manual", "manuals") and "manual" in doc_tokens:
                branch_matches.setdefault("manuals", "/".join(segments[: i + 1]))
                break
    if "drawings" in branch_matches:
        return branch_matches["drawings"]
    if "manuals" in branch_matches:
        return branch_matches["manuals"]
    return None


def classify_by_keywords(text: str, filename: str, hierarchy_paths: list[str]) -> dict:
    """Return {"path", "confidence", "reason", "keywords"} — same shape the
    old LLM-based classifier returned, so the rest of the pipeline (status
    fields, preview table, override dropdown) needs no changes."""
    if not hierarchy_paths:
        return {"path": None, "confidence": 0.0, "reason": "No destination folders to match against.", "keywords": []}

    path_keywords = {p: _path_keywords(p) for p in hierarchy_paths}

    doc_freq: dict[str, int] = defaultdict(int)
    for keywords in path_keywords.values():
        for word in keywords:
            doc_freq[word] += 1
    n_paths = len(hierarchy_paths)
    distinctive_cutoff = max(1, int(n_paths * _MAX_DISTINCTIVE_DOC_FREQ_RATIO))
    # log(N/df), no +1 smoothing — a word unique to one path (out of many)
    # gets a much larger weight than one shared by a meaningful fraction of
    # them, giving real separation between "specific" and "common."
    idf = {word: math.log(n_paths / df) for word, df in doc_freq.items()}

    # The filename plus a document's own title/header area is treated the
    # same way (both are "where the subject is announced"), full text
    # (whole document, per requirement) still contributes, just weighted
    # down when a word doesn't also appear near the top.
    # Include filename/OCR type hints like "manual" and "drawing" even
    # though they are generic filing words — they are often the single most
    # reliable clue for choosing a Drawings vs Manuals destination.
    header_tokens = _tokenize(f"{filename} {(text or '')[:_HEADER_CHARS]}", include_type_hints=True)
    doc_tokens = _tokenize(f"{filename} {text}", include_type_hints=True)

    best_path: str | None = None
    best_score = 0.0
    best_matched: list[str] = []
    for path, keywords in path_keywords.items():
        matched = keywords & doc_tokens
        if not matched:
            continue
        # Require at least one genuinely distinctive matched word — a match
        # built only from words that are common across many folder names is
        # not trustworthy evidence, no matter how many of them line up.
        if not any(doc_freq[w] <= distinctive_cutoff for w in matched):
            continue
        # A category with 2+ real keywords needs at least 2 of them to show
        # up together — a single incidental word match isn't enough
        # corroboration. A category with only 1 real keyword can still match
        # on that word alone, but only if it's long enough to plausibly be a
        # specific term rather than generic English.
        if len(keywords) >= 2 and len(matched) < 2:
            continue
        if len(keywords) == 1 and len(matched) == 1:
            (only_word,) = matched
            if len(only_word) < _MIN_LONE_KEYWORD_LENGTH:
                continue
        # The header/body weighting is the key false-positive fix: words only
        # found deep in the document (never near the stated subject) barely
        # count, so a coincidental later mention can't drive a match alone.
        score = sum(idf[w] if w in header_tokens else idf[w] * _BODY_ONLY_WEIGHT for w in matched)
        if not any(w in header_tokens for w in matched):
            # Every matched word for this path was body-only — not trustworthy
            # as a category decision no matter the accumulated score.
            continue
        if score > best_score:
            best_score = score
            best_path = path
            best_matched = sorted(matched, key=lambda w: -idf[w])

    if best_path is None:
        branch_path = _fallback_branch_path(doc_tokens, hierarchy_paths)
        if branch_path:
            return {
                "path": branch_path,
                "confidence": 0.75,
                "reason": "Document clearly indicates a drawing/manual branch; routed to the matching group.",
                "keywords": sorted({"manual", "drawing"} & doc_tokens),
                # The Group (Drawings/Manuals) is known but no specific
                # Category within it could be proven — this alone should
                # never be accepted as a final classification, since Category
                # is equally mandatory; the caller must still resolve a
                # Category (e.g. migration_mover.py's branch/catch-all
                # fallback at Confirm Move) rather than filing directly here.
                "branch_only": True,
            }
        return {
            "path": None, "confidence": 0.0,
            "reason": "No distinctive folder-name keywords were found near the document's stated subject.",
            "keywords": [],
        }

    # Calibrate against a single maximally-distinctive word (unique to one
    # path), found in the header, as "fully confident" on its own; extra
    # corroborating words push confidence further but with diminishing effect.
    single_word_full_confidence = math.log(n_paths / 1)
    confidence = min(1.0, best_score / single_word_full_confidence)
    return {
        "path": best_path,
        "confidence": confidence,
        "reason": f"Matched distinctive keyword(s): {', '.join(best_matched[:6])}",
        "keywords": best_matched[:10],
    }
