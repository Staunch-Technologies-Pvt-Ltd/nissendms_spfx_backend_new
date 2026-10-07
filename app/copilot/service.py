"""Documents Copilot: answer a plain-English question about the document
library — "latest manuals for Belle Lune", "how many drawings does Bow
Fighter have", "PDFs uploaded last week by Priya" — from the same cached
per-site document index the Dashboard and Vessels pages use.

Searching that index (every site in Site Management, every vessel folder,
with the vessel, group, type, dates and people already worked out) means
answers are instant, cover all sites, and each result carries a link that
opens the file in SharePoint. Nothing here writes anywhere.

Understanding the question:
  * rule-based by default: vessel names (from the index), document group
    (drawings / manuals / to be classified), file type, time period,
    person, and what is asked (list / how many / latest / largest /
    overview); whatever is left is matched against file and folder names;
  * when the company's own Azure OpenAI is configured (AZURE_OPENAI_* in
    .env) it helps interpret unusual questions and writes the answer from
    the same data. No external or free AI service is used.
"""
from __future__ import annotations

import calendar
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import config as cfg
from ..services import get_backend
from ..services.doc_groups import GROUP_LABELS, group_of

log = logging.getLogger(__name__)

_HTTP_TIMEOUT = 20.0
# Per attempt with an OpenAI-compatible API; the next fallback model is tried
# after this, so a busy free-tier model can't stall the answer for long.
_MAX_RESULTS = 25
_DAY_MS = 86_400_000


def _norm(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


# ------------------------------------------------------------ vocabularies
_GROUP_PATTERNS = [
    ("to_be_classified", r"\bto[\s-]*be[\s-]*classif\w*|\bunclassif\w*|\bunsorted\b|\bnot\s+classified\b|\bneeds?\s+sorting\b"),
    ("drawings", r"\bdrawings?\b|\bplans?\b|\bblueprints?\b"),
    ("manuals", r"\bmanuals?\b|\bhandbooks?\b|\binstruction\s*books?\b"),
]

_TYPE_PATTERNS = [
    ("pdf", r"\bpdfs?\b"),
    ("word", r"\bword\b|\bdocx?\b"),
    ("excel", r"\bexcel\b|\bxlsx?\b|\bspreadsheets?\b|\bcsv\b"),
    ("powerpoint", r"\bpowerpoint\b|\bpptx?\b|\bslides?\b|\bpresentations?\b"),
    ("image", r"\bimages?\b|\bphotos?\b|\bpictures?\b|\bjpe?g\b|\bpngs?\b"),
    ("drawing", r"\bdwg\b|\bdxf\b|\bcad\b|\bautocad\b"),
    ("email", r"\bemails?\b|\bmails?\b|\bmsg\b"),
    ("archive", r"\bzip\b|\barchives?\b|\brar\b"),
]
_TYPE_LABELS = {
    "pdf": "PDF", "word": "Word", "excel": "Excel", "powerpoint": "PowerPoint", "image": "image",
    "drawing": "CAD", "email": "email", "archive": "archive", "text": "text", "other": "other",
}

_INTENT_PATTERNS = [
    ("sites", r"\bsites?\b|\blibrar(?:y|ies)\b|\bwhere\s+are\b"),
    ("count", r"\bhow\s+many\b|\bcount\b|\bnumber\s+of\b|\btotal\b|\bmost\b|\bleast\b|\btop\b"),
    ("largest", r"\blargest\b|\bbiggest\b|\bheaviest\b"),
    ("oldest", r"\boldest\b|\bearliest\b"),
    ("latest", r"\blatest\b|\bnewest\b|\bmost\s+recent\b|\brecent(?:ly)?\b|\blast\s+(?:uploaded|added|modified|updated)\b"),
    ("overview", r"\bsummary\b|\boverview\b|\bbreakdown\b|\bstatus\b"),
]

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "doc", "docs", "document",
    "documents", "file", "files", "find", "for", "from", "get", "give", "have", "has", "i", "in", "into",
    "is", "it", "list", "me", "my", "of", "on", "or", "our", "please", "search", "see", "show", "that",
    "the", "their", "there", "this", "to", "uploaded", "updated", "modified", "added", "vessel",
    "vessels", "ship", "ships", "what", "which", "each", "per", "every", "across", "between", "where", "with", "about", "any", "all", "only",
    "just", "also", "now", "then", "those", "these", "them", "same", "we", "you", "tell", "open",
    "mv", "m/v", "how", "many", "much", "new", "type", "kind", "folder", "folders", "site",
    "anything", "something", "everything", "stuff", "things", "items", "latest", "recent",
    "its", "it's", "their", "they", "has", "have", "had", "most", "least", "top", "highest", "lowest",
    "hello", "hi", "hey", "thanks", "thank", "please", "help", "can", "could", "would", "want", "need", "know",
}

_FOLLOW_UP = re.compile(
    r"^\s*(only|just|and|also|now|what\s+about|how\s+about|those|these|same|then|filter|show\s+only|narrow)\b"
    r"|\b(those|these|them|its|it|their|same\s+vessel|that\s+vessel|this\s+vessel)\b",
    re.IGNORECASE,
)


# ----------------------------------------------------------- time periods
def _period(question: str, now: datetime) -> tuple[tuple[int, int] | None, str | None, str]:
    """(from_ms, to_ms) for a time phrase in the question, its label, and the
    question with that phrase removed."""
    q = question
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def ms(dt: datetime) -> int:
        return int(dt.timestamp() * 1000)

    rules: list[tuple[str, callable]] = [
        (r"\btoday\b", lambda m: ((ms(day0), ms(now) + 1), "today")),
        (r"\byesterday\b", lambda m: ((ms(day0 - timedelta(days=1)), ms(day0)), "yesterday")),
        (r"\bthis\s+week\b", lambda m: ((ms(day0 - timedelta(days=day0.weekday())), ms(now) + 1), "this week")),
        (r"\blast\s+week\b", lambda m: ((ms(day0 - timedelta(days=day0.weekday() + 7)),
                                         ms(day0 - timedelta(days=day0.weekday()))), "last week")),
        (r"\bthis\s+month\b", lambda m: ((ms(day0.replace(day=1)), ms(now) + 1), "this month")),
        (r"\blast\s+month\b", lambda m: _month_range(day0.year if day0.month > 1 else day0.year - 1,
                                                     day0.month - 1 or 12, "last month")),
        (r"\bthis\s+year\b", lambda m: ((ms(day0.replace(month=1, day=1)), ms(now) + 1), "this year")),
        (r"\blast\s+year\b", lambda m: ((ms(day0.replace(year=day0.year - 1, month=1, day=1)),
                                         ms(day0.replace(month=1, day=1))), "last year")),
        (r"\b(?:last|past)\s+(\d{1,3})\s+(day|week|month)s?\b", lambda m: (
            (ms(now - timedelta(days=int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2).lower()])),
             ms(now) + 1),
            f"last {m.group(1)} {m.group(2).lower()}s")),
        (r"\b(?:in|during|for)?\s*(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b\.?\s*(\d{4})?\b",
         lambda m: _month_range(int(m.group(2)) if m.group(2) else (
             now.year if _MONTHS[m.group(1).lower()] <= now.month else now.year - 1),
             _MONTHS[m.group(1).lower()], None)),
        (r"\b(?:in|during|for|from)\s+(20\d{2})\b", lambda m: (
            (ms(datetime(int(m.group(1)), 1, 1, tzinfo=timezone.utc)),
             ms(datetime(int(m.group(1)) + 1, 1, 1, tzinfo=timezone.utc))), m.group(1))),
    ]
    for pattern, build in rules:
        m = re.search(pattern, q, re.IGNORECASE)
        if not m:
            continue
        # "may" is a month and a verb; only treat it as a month next to a year
        # or after in/during/for.
        if m.group(0).strip().lower() == "may":
            continue
        rng, label = build(m)
        return rng, label, (q[:m.start()] + " " + q[m.end():])
    return None, None, q


def _month_range(year: int, month: int, label: str | None):
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone.utc)
    return (int(start.timestamp() * 1000), int(end.timestamp() * 1000)), label or f"{calendar.month_name[month]} {year}"


# ------------------------------------------------------- question parsing
def _parse(question: str, vessel_names: list[str], people: list[str], now: datetime,
           site_names: list[str] | None = None) -> dict:
    q = " " + question.strip() + " "
    parsed: dict = {"vessel": None, "group": None, "file_type": None, "period": None, "period_label": None,
                    "person": None, "intent": "list", "keywords": [], "site": None}

    # Site: a Site Management site named in the question ("in NKSDocMan").
    qn0 = _norm(q)
    for name in sorted(site_names or [], key=lambda n: len(_norm(n)), reverse=True):
        if len(_norm(name)) >= 4 and _norm(name) in qn0:
            parsed["site"] = name
            q = re.sub(re.escape(name), " ", q, flags=re.IGNORECASE)
            for word in re.findall(r"[\w'-]+", name):
                q = re.sub(rf"\b{re.escape(word)}\b", " ", q, flags=re.IGNORECASE)
            break

    # Vessel: the longest known name that appears in the question.
    qn = _norm(q)
    best = None
    for name in vessel_names:
        key = _norm(name)
        if len(key) >= 3 and key in qn and (best is None or len(key) > len(_norm(best))):
            best = name
    if best:
        parsed["vessel"] = best
        # Drop the vessel's words from the free text.
        for word in re.findall(r"[\w'-]+", best):
            q = re.sub(rf"\b{re.escape(word)}\b", " ", q, flags=re.IGNORECASE)

    rng, label, q = _period(q, now)
    if rng:
        parsed["period"], parsed["period_label"] = rng, label

    # Person: "by <name>" / "from <name>" matched to someone in the index.
    m = re.search(r"\b(?:by|from|of)\s+([a-z][a-z.'-]+(?:\s+[a-z][a-z.'-]+)?)", q, re.IGNORECASE)
    if m:
        cand = _norm(m.group(1))
        hit = next((p for p in people if cand and (cand in _norm(p) or _norm(p.split()[0]) == cand)), None)
        if hit:
            parsed["person"] = hit
            q = q[:m.start()] + " " + q[m.end():]

    for key, pattern in _GROUP_PATTERNS:
        if re.search(pattern, q, re.IGNORECASE):
            parsed["group"] = key
            q = re.sub(pattern, " ", q, flags=re.IGNORECASE)
            break
    for key, pattern in _TYPE_PATTERNS:
        if re.search(pattern, q, re.IGNORECASE):
            parsed["file_type"] = key
            q = re.sub(pattern, " ", q, flags=re.IGNORECASE)
            break
    for key, pattern in _INTENT_PATTERNS:
        if re.search(pattern, q, re.IGNORECASE):
            parsed["intent"] = key
            q = re.sub(pattern, " ", q, flags=re.IGNORECASE)
            break

    parsed["keywords"] = [
        w for w in (t.strip("'-.").lower() for t in re.findall(r"[\w'.-]+", q))
        if len(w) >= 2 and w not in _STOPWORDS
    ]
    if parsed["intent"] == "list" and parsed["vessel"] and not (
        parsed["group"] or parsed["file_type"] or parsed["period"] or parsed["person"] or parsed["keywords"]
    ):
        parsed["intent"] = "overview"
    return parsed


# ------------------------------------------------------------- LLM (optional)
_LLM_SYSTEM = """You read a question about a ship-management company's document library and \
extract search filters. Use a vessel name ONLY if it appears in Known vessels (copy it exactly); \
otherwise null. Reply with ONLY a JSON object:
{{"vessel": <exact name or null>,
 "group": "drawings" | "manuals" | "to_be_classified" | null,
 "file_type": "pdf" | "word" | "excel" | "powerpoint" | "image" | "drawing" | "email" | "archive" | null,
 "intent": "list" | "count" | "latest" | "oldest" | "largest" | "overview",
 "keywords": [<short words to match in file or folder names, e.g. "main engine", "certificate">]}}

Known vessels: {vessels}"""


async def _chat(system: str, messages: list[dict], max_tokens: int = 600, json_only: bool = False) -> str:
    """One chat completion from the company's Azure OpenAI resource."""
    prov = cfg.provider()
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        if prov == "azure":
            url = (f"{cfg.AZURE_OPENAI_ENDPOINT}/openai/deployments/{cfg.AZURE_OPENAI_DEPLOYMENT}"
                   f"/chat/completions?api-version={cfg.AZURE_OPENAI_API_VERSION}")
            body = {"messages": [{"role": "system", "content": system}, *messages],
                    "temperature": 0.2, "max_tokens": max_tokens}
            if json_only:
                body["response_format"] = {"type": "json_object"}
            resp = await client.post(url, json=body,
                                     headers={"api-key": cfg.AZURE_OPENAI_API_KEY, "Content-Type": "application/json"})
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"] or ""
    raise RuntimeError("No AI provider configured")


def _json_from(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    return json.loads(m.group(0)) if m else {}


async def _llm_parse(question: str, vessel_names: list[str], history: list[dict]) -> dict:
    prior = "\n".join(f"Earlier question: {h.get('question')}" for h in history[-3:] if h.get("question"))
    content = (prior + "\n" if prior else "") + f"Question: {question}"
    text = await _chat(_LLM_SYSTEM.format(vessels=", ".join(vessel_names) or "(none)"),
                       [{"role": "user", "content": content}], max_tokens=300, json_only=True)
    return _json_from(text)


_ANSWER_SYSTEM = """You are Vessel DMS Copilot, the assistant inside a ship-management company's \
document management system (SharePoint-based). Users ask about their vessels' documents: drawings, \
manuals, documents still to be classified, certificates, who uploaded what, when, and on which site.

Answer the user's latest question using ONLY the DATA provided (search results and counts computed \
from the live document index). Rules:
- Be direct and conversational, like a helpful colleague. 1-4 short sentences, or a few bullets when listing.
- Use exact numbers and names from DATA. Never invent documents, vessels, people, dates or counts.
- Mention specific documents by file name (in *italics*) when they answer the question.
- If DATA doesn't contain the answer, say so plainly and suggest what the user could ask instead.
- If the question isn't about documents (greetings, small talk), reply briefly and say what you can help with.
- Use **bold** for key numbers or names. No headings, no tables, no links (the app shows result cards).
"""


async def _llm_answer(question: str, history: list[dict], data: dict) -> str:
    messages: list[dict] = []
    for h in history[-4:]:
        if h.get("question") and h.get("answer"):
            messages.append({"role": "user", "content": str(h["question"])[:500]})
            messages.append({"role": "assistant", "content": str(h["answer"])[:1200]})
    messages.append({"role": "user", "content": f"DATA:\n{json.dumps(data, default=str)[:12000]}\n\nQuestion: {question}"})
    return (await _chat(_ANSWER_SYSTEM, messages, max_tokens=500)).strip()


def _merge_llm(parsed: dict, llm: dict, vessel_names: list[str]) -> None:
    by_norm = {_norm(n): n for n in vessel_names}
    if llm.get("vessel") and _norm(llm["vessel"]) in by_norm:
        parsed["vessel"] = by_norm[_norm(llm["vessel"])]
    if llm.get("group") in GROUP_LABELS:
        parsed["group"] = llm["group"]
    if llm.get("file_type") in _TYPE_LABELS:
        parsed["file_type"] = llm["file_type"]
    if llm.get("intent") in {"list", "count", "latest", "oldest", "largest", "overview"}:
        parsed["intent"] = llm["intent"]
    words = llm.get("keywords")
    if isinstance(words, list):
        vessel_words = {w.lower() for w in re.findall(r"[\w'-]+", parsed.get("vessel") or "")}
        parsed["keywords"] = [
            t for w in words for t in re.findall(r"[\w'.-]+", str(w).lower())
            if len(t) >= 2 and t not in _STOPWORDS and t not in vessel_words
        ]


# ------------------------------------------------------------- matching
def _doc_matches(d: dict, p: dict) -> bool:
    if p["vessel"] and _norm(d.get("vessel")) != _norm(p["vessel"]):
        return False
    if p["group"] and group_of(d) != p["group"]:
        return False
    if p["file_type"] and d.get("fileType") != p["file_type"]:
        return False
    if p["period"]:
        ep = d.get("modifiedEpoch") or 0
        if not (p["period"][0] <= ep < p["period"][1]):
            return False
    if p["person"]:
        who = _norm(p["person"])
        if who not in (_norm(d.get("modifiedBy")), _norm(d.get("createdBy"))):
            return False
    return True


def _keyword_score(d: dict, keywords: list[str], require_all: bool) -> int:
    if not keywords:
        return 1
    name = (d.get("name") or "").lower()
    path = f"{d.get('subFolderPath') or ''} {d.get('type') or ''}".lower()
    score, hits = 0, 0
    for k in keywords:
        if k in name:
            score += 3
            hits += 1
        elif k in path:
            score += 1
            hits += 1
    if require_all and hits < len(keywords):
        return 0
    return score


def _fmt_date(epoch_ms: int | None) -> str:
    if not epoch_ms:
        return "—"
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime("%d %b %Y")


def _result_row(d: dict) -> dict:
    return {
        "id": d.get("id"),
        "name": d.get("name"),
        "kind": "file",
        "vessel": None if d.get("vessel") in (None, "", "Not Listed") else d.get("vessel"),
        "group": GROUP_LABELS[group_of(d)],
        "file_type": d.get("fileType"),
        "site": d.get("site"),
        "site_name": d.get("siteName"),
        "path": d.get("subFolderPath"),
        "modified": _fmt_date(d.get("modifiedEpoch")),
        "modified_epoch": d.get("modifiedEpoch") or 0,
        "modified_by": d.get("modifiedBy"),
        "size": d.get("fileSize"),
        "size_bytes": d.get("sizeBytes") or 0,
        "web_url": d.get("webUrl"),
        "trail": [],
    }


def _breakdown(docs: list[dict]) -> dict:
    out = {"drawings": 0, "manuals": 0, "to_be_classified": 0, "other": 0}
    for d in docs:
        out[group_of(d)] += 1
    return out


def _describe(p: dict, total: int) -> str:
    noun = {"drawings": "drawing", "manuals": "manual", "to_be_classified": "document to be classified"}.get(
        p["group"] or "", "document")
    if p["file_type"]:
        noun = f"{_TYPE_LABELS[p['file_type']]} {noun}"
    if total != 1:
        noun = noun.replace("document to be classified", "documents to be classified") \
            if "to be classified" in noun else noun + "s"
    bits = [f"**{total:,}** {noun}"]
    if p["keywords"]:
        bits.append(f"matching “{' '.join(p['keywords'])}”")
    if p["vessel"]:
        bits.append(f"for **{p['vessel']}**")
    if p["person"]:
        bits.append(f"by {p['person']}")
    if p["period_label"]:
        bits.append(f"({p['period_label']})")
    return " ".join(bits)


def _suggestions(p: dict, vessel: str | None, sample_vessels: list[str],
                 sites: list[str] | None = None) -> list[str]:
    s: list[str] = []
    if sites:
        for name in sites[:2]:
            s.append(f"Drawings in {name}")
            s.append(f"What still needs classifying in {name}?")
        return s[:4]
    if vessel:
        if p["group"] != "manuals":
            s.append(f"Manuals for {vessel}")
        if p["group"] != "drawings":
            s.append(f"Drawings for {vessel}")
        if p["group"] != "to_be_classified":
            s.append(f"What still needs classifying for {vessel}?")
        if p["intent"] != "latest":
            s.append(f"Latest documents for {vessel}")
    else:
        for v in sample_vessels[:2]:
            s.append(f"Overview of {v}")
        s.append("Documents uploaded this week")
        s.append("How many drawings do we have?")
    return s[:4]


# ------------------------------------------------------------------ ask()
async def ask(question: str, site_key: str | None = None, context: dict | None = None,
              history: list[dict] | None = None) -> dict:
    """Answer one question. Returns
    {"answer", "mode", "filters", "results", "total", "breakdown",
     "top_vessels", "suggestions", "scan_pending"}."""
    started = time.monotonic()
    backend = get_backend()
    scope = site_key if site_key and site_key != "all" else None
    docs: list[dict] = []
    scan_pending = False
    if hasattr(backend, "get_dashboard_documents"):
        result = await backend.get_dashboard_documents(force_refresh=False, site_key=scope)
        if isinstance(result, dict):
            docs = list(result.get("docs") or [])
            scan_pending = bool(result.get("pending"))
        else:
            docs = list(result or [])

    # Every site's documents (cached), so a question can name another site
    # than the one selected in the panel, or compare sites.
    all_docs = docs
    if scope is not None and hasattr(backend, "get_dashboard_documents"):
        try:
            res_all = await backend.get_dashboard_documents(force_refresh=False, site_key=None)
            all_docs = list((res_all.get("docs") if isinstance(res_all, dict) else res_all) or []) or docs
        except Exception as exc:  # noqa: BLE001 - fall back to the selected site only
            log.debug("[copilot] all-sites index unavailable: %s", exc)
    site_names = sorted({d.get("siteName") or d.get("site") for d in all_docs if d.get("siteName") or d.get("site")})

    vessel_names = sorted({d["vessel"] for d in all_docs if d.get("vessel") and d["vessel"] != "Not Listed"},
                          key=str.casefold)
    people = sorted({n for d in all_docs for n in (d.get("modifiedBy"), d.get("createdBy")) if n})
    now = datetime.now(timezone.utc)

    p = _parse(question, vessel_names, people, now, site_names)
    mode = "ai" if cfg.is_configured() else "keyword"
    # The rules understand most questions; only ask the AI to interpret one
    # they couldn't (saves a round trip - free-tier models can be slow).
    understood = any(p.get(k) for k in ("vessel", "group", "file_type", "period", "person", "site")) \
        or p["intent"] != "list"
    if mode == "ai" and not understood and p["keywords"]:
        try:
            _merge_llm(p, await _llm_parse(question, vessel_names, history or []), vessel_names)
        except Exception as exc:  # network/parse failure -> rules only
            log.warning("[copilot] AI parsing failed, using rules: %s", exc)

    # A follow-up ("only drawings", "those from last week") keeps the
    # previous question's vessel / group / type unless it names new ones.
    ctx = context or {}
    if ctx and _FOLLOW_UP.search(question):
        for key in ("vessel", "group", "file_type", "site"):
            if not p.get(key) and ctx.get(key):
                p[key] = ctx[key]
        if p["intent"] == "overview" and (p["group"] or p["file_type"] or p["keywords"]):
            p["intent"] = "list"

    if p["site"]:
        docs = [d for d in all_docs if (d.get("siteName") or d.get("site")) == p["site"]]
    elif p["intent"] == "sites":
        docs = all_docs
    scope_label = p["site"] or (docs[0].get("siteName") if scope and docs else None)

    if scan_pending and not docs:
        return {
            "answer": "I'm still indexing this site's documents — ask me again in a minute.",
            "mode": mode, "filters": _filters_out(p), "results": [], "total": 0,
            "breakdown": None, "top_vessels": [], "suggestions": [], "scan_pending": True,
        }

    matched = [d for d in docs if _doc_matches(d, p)]
    relaxed = False
    scored = [(s, d) for d in matched if (s := _keyword_score(d, p["keywords"], True))]
    if not scored and p["keywords"]:
        scored = [(s, d) for d in matched if (s := _keyword_score(d, p["keywords"], False))]
        relaxed = bool(scored)
    hits = [d for _, d in scored]

    intent = p["intent"]
    if intent == "largest":
        scored.sort(key=lambda sd: sd[1].get("sizeBytes") or 0, reverse=True)
    elif intent == "oldest":
        scored.sort(key=lambda sd: sd[1].get("modifiedEpoch") or 0)
    elif intent == "latest" or not p["keywords"]:
        scored.sort(key=lambda sd: sd[1].get("modifiedEpoch") or 0, reverse=True)
    else:
        scored.sort(key=lambda sd: (sd[0], sd[1].get("modifiedEpoch") or 0), reverse=True)
    results = [_result_row(d) for _, d in scored[:_MAX_RESULTS]]

    total = len(hits)
    breakdown = _breakdown(hits)
    vessel_counts: dict[str, int] = {}
    for d in hits:
        v = d.get("vessel")
        if v and v != "Not Listed":
            vessel_counts[v] = vessel_counts.get(v, 0) + 1
    top_vessels = [{"name": n, "count": c} for n, c in
                   sorted(vessel_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]]

    site_counts: dict[str, dict] = {}
    for d in hits:
        sn = d.get("siteName") or d.get("site") or "SharePoint"
        row = site_counts.setdefault(sn, {"name": sn, "count": 0, "vessels": set()})
        row["count"] += 1
        if d.get("vessel") and d["vessel"] != "Not Listed":
            row["vessels"].add(d["vessel"])
    by_site = [{"name": r["name"], "count": r["count"], "vessels": len(r["vessels"])}
               for r in sorted(site_counts.values(), key=lambda r: r["count"], reverse=True)]

    generic = intent == "list" and not (p["vessel"] or p["group"] or p["file_type"] or p["period"]
                                        or p["person"] or p["keywords"])
    if intent == "sites":
        answer = _sites_answer(p, by_site, total)
    elif generic:
        answer = _scope_answer(scope_label, total, breakdown, top_vessels, len(vessel_counts))
    else:
        answer = _answer(p, intent, total, breakdown, hits, results, top_vessels, relaxed)
    if mode == "ai":
        try:
            ai_answer = await _llm_answer(question, history or [], {
                "understood": _filters_out(p),
                "searched": p["site"] or scope_label or "all sites",
                "total_matching_documents": total,
                "by_group": breakdown,
                "by_site": by_site,
                "top_vessels": top_vessels,
                "all_vessels_in_scope": vessel_names[:80],
                "results_shown": [
                    {k: r[k] for k in ("name", "vessel", "group", "site_name", "path", "modified", "modified_by", "size")}
                    for r in results[:15]
                ],
                "rule_based_summary": answer,
            })
            if ai_answer:
                answer = ai_answer
        except Exception as exc:  # keep the rule-based answer
            log.warning("[copilot] AI answer failed, using rule-based answer: %s", exc)
    log.info("[copilot] q=%r site=%s -> %d hit(s) in %.0f ms (%s)", question, scope or "all", total,
             (time.monotonic() - started) * 1000, mode)
    return {
        "answer": answer,
        "mode": mode,
        "filters": _filters_out(p),
        "results": results,
        "total": total,
        "breakdown": breakdown if total else None,
        "top_vessels": top_vessels if not p["vessel"] else [],
        "suggestions": _suggestions(p, p["vessel"], [t["name"] for t in top_vessels] or vessel_names,
                                    [b["name"] for b in by_site] if intent == "sites" else []),
        "by_site": by_site if (intent == "sites" or len(by_site) > 1) else [],
        "scan_pending": scan_pending,
    }


def _filters_out(p: dict) -> dict:
    return {
        "vessel": p["vessel"], "group": p["group"], "file_type": p["file_type"],
        "period": p["period_label"], "person": p["person"], "intent": p["intent"], "site": p["site"],
        "keywords": " ".join(p["keywords"]),
    }


def _sites_answer(p: dict, by_site: list[dict], total: int) -> str:
    if not by_site:
        return "I couldn't find any documents for that on your sites."
    what = _describe(p, total).split(" ", 1)[1] if (p["vessel"] or p["group"] or p["file_type"]) else "documents"
    lines = "; ".join(
        f"**{b['name']}**: {b['count']:,}" + (f" across {b['vessels']} vessels" if b["vessels"] else "")
        for b in by_site
    )
    if len(by_site) == 1:
        return f"All {what} are on 1 site: {lines}."
    return f"Your {what} are spread over {len(by_site)} sites: {lines}."


def _scope_answer(scope_label: str | None, total: int, breakdown: dict, top_vessels: list[dict],
                  vessel_count: int) -> str:
    where, verb = (f"**{scope_label}**", "has") if scope_label else ("Your sites", "have")
    split = ", ".join(f"{breakdown[k]:,} {GROUP_LABELS[k].lower()}"
                      for k in ("drawings", "manuals", "to_be_classified") if breakdown.get(k))
    line = f"{where} {verb} **{total:,}** documents across {vessel_count} vessels" + (f": {split}" if split else "") + "."
    if top_vessels:
        line += " Most documents: " + ", ".join(f"{v['name']} ({v['count']:,})" for v in top_vessels[:3]) + "."
    return line + " Ask about a vessel, a type of document, a date or a person to narrow it down."


def _answer(p, intent, total, breakdown, hits, results, top_vessels, relaxed) -> str:
    if not total:
        tip = "Try fewer words" + (f", or check the spelling of the vessel" if not p["vessel"] else "") + "."
        return f"I couldn't find any {_describe(p, 0).split(' ', 1)[1]}. {tip}"

    singular = {"drawings": "drawing", "manuals": "manual", "to_be_classified": "to be classified"}
    split = ", ".join(
        f"{breakdown[k]:,} {GROUP_LABELS[k].lower() if breakdown[k] != 1 else singular[k]}"
        for k in ("drawings", "manuals", "to_be_classified") if breakdown[k]
    )
    newest = max(hits, key=lambda d: d.get("modifiedEpoch") or 0)
    newest_line = (f"Last updated {_fmt_date(newest.get('modifiedEpoch'))}"
                   + (f" by {newest.get('modifiedBy')}" if newest.get("modifiedBy") else "")
                   + f" — *{newest.get('name')}*.")

    if intent == "overview" and p["vessel"]:
        return (f"**{p['vessel']}** has **{total:,}** documents"
                + (f": {split}" if split else "") + f". {newest_line}")
    if intent == "count":
        line = f"There are {_describe(p, total)}."
        if split and not p["group"]:
            line += f" That's {split}."
        if top_vessels and not p["vessel"]:
            line += " Most are on " + ", ".join(f"{v['name']} ({v['count']:,})" for v in top_vessels[:3]) + "."
        return line
    lead = "No exact match — closest results: " if relaxed else "Found "
    line = f"{lead}{_describe(p, total)}."
    if intent == "largest" and results:
        line += f" Largest is *{results[0]['name']}* ({results[0]['size']})."
    elif intent == "oldest" and results:
        line += f" Oldest is *{results[0]['name']}* ({results[0]['modified']})."
    else:
        line += f" {newest_line}"
    if total > len(results):
        line += f" Showing the top {len(results)}."
    return line
