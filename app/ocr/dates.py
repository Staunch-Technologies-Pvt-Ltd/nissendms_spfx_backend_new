"""Month detection from document text.

Pure, dependency-light parsing so it can be unit-tested without PaddleOCR.
Given OCR'd (or embedded) text, find the document's reporting month.

Strategy: collect (year, month) candidates from several date patterns, give a
small weight boost to candidates that appear right after context keywords
(month / period / report / invoice / statement / date), and return the
highest-weighted candidate (ties broken by earliest appearance).
"""
import re

MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
MONTH_NAMES = [m.capitalize() for m in MONTHS]
_LOOKUP = {m: i + 1 for i, m in enumerate(MONTHS)}
_LOOKUP.update({m[:3]: i + 1 for i, m in enumerate(MONTHS)})

_KEYWORDS = ("month", "period", "report", "invoice", "statement", "dated", "date", "for the")

# Monthname YYYY  (e.g. "July 2026", "Jul-2026", "July, 2026")
_RE_NAME_YEAR = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s*[-/ ]?\s*(20\d{2})\b",
    re.IGNORECASE,
)
# YYYY-MM  (e.g. 2026-07, 2026/07, 2026.07)
_RE_YEAR_MONTH = re.compile(r"\b(20\d{2})[-/.](0[1-9]|1[0-2])\b")
# MM-YYYY  (e.g. 07-2026)
_RE_MONTH_YEAR = re.compile(r"\b(0[1-9]|1[0-2])[-/.](20\d{2})\b")
# DD-MM-YYYY  (e.g. 31/07/2026) — day then month then year
_RE_DMY = re.compile(r"\b(0?[1-9]|[12]\d|3[01])[-/.](0?[1-9]|1[0-2])[-/.](20\d{2})\b")


def _keyword_boost(text: str, pos: int) -> int:
    window = text[max(0, pos - 40):pos].lower()
    return 2 if any(k in window for k in _KEYWORDS) else 0


def parse_month(text: str) -> tuple[int, int] | None:
    """Return (year, month) for the most likely reporting month, or None."""
    if not text:
        return None
    t = text.lower()
    # candidate -> [weight, first_position]
    cand: dict[tuple[int, int], list[int]] = {}

    def add(year: int, month: int, pos: int, base: int = 1):
        if not (1 <= month <= 12) or not (2000 <= year <= 2099):
            return
        key = (year, month)
        w = base + _keyword_boost(text, pos)
        if key not in cand:
            cand[key] = [0, pos]
        cand[key][0] += w
        cand[key][1] = min(cand[key][1], pos)

    for m in _RE_NAME_YEAR.finditer(t):
        add(int(m.group(2)), _LOOKUP[m.group(1).lower()[:3]], m.start(), base=2)
    for m in _RE_YEAR_MONTH.finditer(t):
        add(int(m.group(1)), int(m.group(2)), m.start())
    for m in _RE_MONTH_YEAR.finditer(t):
        add(int(m.group(2)), int(m.group(1)), m.start())
    for m in _RE_DMY.finditer(t):
        add(int(m.group(3)), int(m.group(2)), m.start())

    if not cand:
        return None
    # Highest weight, then earliest position.
    best = min(cand.items(), key=lambda kv: (-kv[1][0], kv[1][1]))
    return best[0]


def month_label(year: int, month: int) -> str:
    return f"{MONTH_NAMES[month - 1]} {year}"
