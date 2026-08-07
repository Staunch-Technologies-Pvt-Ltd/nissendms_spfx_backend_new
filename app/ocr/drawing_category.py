"""Drawing sub-category detection from document text.

Pure, dependency-light keyword matching so it can be unit-tested without
PaddleOCR — mirrors the approach in `dates.py`. Given text from a document
already known to belong to the Drawings category, guess which drawing
sub-folder (Archive / Basic / Electrical / Engine / Hull / Safety) it belongs
to. Returns `None` when nothing matches confidently, in which case the caller
should file the document under "Other Drawings" rather than guessing.
"""

_CATEGORY_KEYWORDS = {
    "Archive": ("archive", "superseded", "obsolete", "historical", "as-built", "as built"),
    "Basic": ("basic design", "general arrangement", "ga drawing", "outline drawing", "concept design"),
    "Electrical": ("electrical", "single line diagram", "wiring diagram", "cable schedule", "switchboard", "circuit diagram"),
    "Engine": ("engine", "piping diagram", "propulsion", "main engine", "aux engine", "cooling water", "fuel oil system"),
    "Hull": ("hull", "shell expansion", "midship section", "structural drawing", "framing plan", "hull arrangement"),
    "Safety": ("safety plan", "fire control", "fire fighting", "lifeboat", "muster list", "escape route", "sopep", "life-saving"),
}


def classify_drawing_category(text: str) -> str | None:
    """Return the best-matching sub-category name, or None if no category's
    keywords appear in `text` (case-insensitive substring counts, highest
    total wins; ties keep the earlier-declared category)."""
    if not text:
        return None
    t = text.lower()
    best = None
    best_score = 0
    for category, keywords in _CATEGORY_KEYWORDS.items():
        score = sum(t.count(k) for k in keywords)
        if score > best_score:
            best_score = score
            best = category
    return best
