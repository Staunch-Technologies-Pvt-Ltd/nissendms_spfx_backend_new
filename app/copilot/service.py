"""Documents Copilot: turn a plain-English question into a search against
the Documents module's existing search, and a one-line answer.

Coupling to the rest of the app is intentionally narrow — only:
  * ..services.get_backend()      -> the existing `search(q, vessel_id)`
                                      and `list_vessels()` (real_backend.py /
                                      stub_backend.py — unchanged)
  * ..services.tag_config.get_view() -> the Group/Category vocabulary, so
                                      the LLM (or the fallback) only ever
                                      names a Group/Category that actually
                                      exists, never an invented one.

No new database table, no change to auth, no change to Graph scopes: this
module only ever calls the backend's own already-existing `search()`.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from . import config as cfg
from ..services import get_backend
from ..services import tag_config
from ..services.tag_config import match_norm as _norm

log = logging.getLogger(__name__)

_HTTP_TIMEOUT = 20.0
_MAX_RESULTS = 30


# ---------------------------------------------------------------- LLM path
_SYSTEM_TEMPLATE = """You turn a question about a shipping company's document library into \
search filters. Only ever use a vessel, group, or category name from the lists below \
verbatim — never invent, translate, or guess one that isn't listed. If nothing in a list \
matches, use null for that field.

Known vessels: {vessels}
Known document groups: {groups}
Known document categories: {categories}

Reply with ONLY a JSON object, no other text:
{{"keywords": "<remaining free-text search words, or empty string>", \
"vessel_name": "<one exact name from Known vessels, or null>", \
"group": "<one exact name from Known document groups, or null>", \
"category": "<one exact name from Known document categories, or null>"}}"""


async def _extract_with_llm(question: str, vessel_names: list[str], group_names: list[str],
                             category_names: list[str]) -> dict:
    system = _SYSTEM_TEMPLATE.format(
        vessels=", ".join(vessel_names) or "(none registered yet)",
        groups=", ".join(group_names) or "(none configured yet)",
        categories=", ".join(category_names) or "(none configured yet)",
    )
    url = (
        f"{cfg.AZURE_OPENAI_ENDPOINT}/openai/deployments/{cfg.AZURE_OPENAI_DEPLOYMENT}"
        f"/chat/completions?api-version={cfg.AZURE_OPENAI_API_VERSION}"
    )
    body = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 300,
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(
            url,
            headers={"api-key": cfg.AZURE_OPENAI_API_KEY, "Content-Type": "application/json"},
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
    content = data["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    return {
        "keywords": str(parsed.get("keywords") or "").strip(),
        "vessel_name": parsed.get("vessel_name") or None,
        "group": parsed.get("group") or None,
        "category": parsed.get("category") or None,
    }


# ----------------------------------------------------------- fallback path
# backend.search() matches keywords with a plain ILIKE "%...%" against
# Folder.name/Folder.path (real_backend.py search()), i.e. a literal
# substring test — not a tokenized/full-text search. So the fallback must
# hand it a short phrase that might actually appear in a path, not the
# raw sentence: a folder is never literally named "crew certificates for
# mv horizon".
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "documents", "document",
    "files", "file", "find", "for", "from", "get", "in", "into", "is", "it",
    "me", "of", "on", "or", "please", "search", "show", "that", "the", "to",
    "with", "about", "regarding", "any", "all", "give",
}


def _extract_without_llm(question: str, vessels: list[dict]) -> dict:
    """No Azure OpenAI configured: spot a known vessel name mentioned in the
    question (if any), strip it plus filler words out of the question, and
    pass what's left through as a free-text search phrase. Cruder than the
    LLM path, but it gives backend.search() a substring that can actually
    occur in a folder/file name instead of the whole sentence verbatim."""
    ql = _norm(question)
    hit_vessel = next((v["name"] for v in vessels if _norm(v["name"]) and _norm(v["name"]) in ql), None)

    remainder = question
    if hit_vessel:
        remainder = re.sub(re.escape(hit_vessel), " ", remainder, flags=re.IGNORECASE)
    words = [w for w in re.findall(r"[\w'-]+", remainder) if w.lower() not in _STOPWORDS]
    keywords = " ".join(words).strip()

    return {"keywords": keywords, "vessel_name": hit_vessel, "group": None, "category": None}


# --------------------------------------------------------------- answer
def _summarize(filters: dict, results: list[dict]) -> str:
    if not results:
        return "I couldn't find any documents matching that. Try different or fewer words."
    bits = [f"vessel **{filters['vessel_name']}**"] if filters.get("vessel_name") else []
    if filters.get("group"):
        bits.append(f"group **{filters['group']}**")
    if filters.get("category"):
        bits.append(f"category **{filters['category']}**")
    scope = f" ({', '.join(bits)})" if bits else ""
    plural = "document" if len(results) == 1 else "documents"
    return f"Found {len(results)} {plural}{scope}."


# ------------------------------------------------------------------ ask()
async def ask(question: str, vessel_id: str | None = None) -> dict:
    """Answer one Documents-module question.

    Returns {"answer": str, "mode": "ai" | "keyword", "filters": dict,
    "results": [...same shape services.*_backend.search() returns...]}.
    """
    backend = get_backend()
    view = tag_config.get_view()
    vessels = await backend.list_vessels()

    group_names = [g["display_name"] for g in view.items("group", active_only=True)]
    category_names = [c["display_name"] for c in view.items("category", active_only=True)]

    mode = "keyword"
    filters: dict = {}
    if cfg.is_configured():
        try:
            filters = await _extract_with_llm(
                question, [v["name"] for v in vessels], group_names, category_names,
            )
            mode = "ai"
        except Exception as exc:  # network/parse failure -> degrade, don't fail the request
            log.warning("[copilot] LLM extraction failed, using keyword fallback: %s", exc)
    if mode == "keyword":
        filters = _extract_without_llm(question, vessels)

    resolved_vessel_id = vessel_id
    if not resolved_vessel_id and filters.get("vessel_name"):
        hit = next((v for v in vessels if _norm(v["name"]) == _norm(filters["vessel_name"])), None)
        if hit:
            resolved_vessel_id = hit["id"]

    query_text = filters.get("keywords") or filters.get("vessel_name") or question.strip()
    raw_results = await backend.search(query_text, resolved_vessel_id)

    group_key = _norm(filters.get("group")) if filters.get("group") else None
    category_key = _norm(filters.get("category")) if filters.get("category") else None
    if group_key or category_key:
        results = []
        for item in raw_results:
            trail_keys = {_norm(t.get("name")) for t in item.get("trail", [])}
            if group_key and group_key not in trail_keys:
                continue
            if category_key and category_key not in trail_keys:
                continue
            results.append(item)
    else:
        results = raw_results

    results = results[:_MAX_RESULTS]
    return {
        "answer": _summarize(filters, results),
        "mode": mode,
        "filters": filters,
        "results": results,
    }
