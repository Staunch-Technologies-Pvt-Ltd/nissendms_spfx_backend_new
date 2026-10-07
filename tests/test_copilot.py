"""Documents Copilot answers from the cached document index (no SharePoint)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.copilot import service

NOW = datetime.now(timezone.utc)


def _doc(name, vessel, path, days_ago=1, by="Priya Raman", size=1000):
    ext = name.rsplit(".", 1)[-1].lower()
    ep = int((NOW - timedelta(days=days_ago)).timestamp() * 1000)
    return {
        "id": name, "name": name, "ext": ext, "fileType": service._TYPE_LABELS and {
            "pdf": "pdf", "docx": "word", "xlsx": "excel", "dwg": "drawing"}.get(ext, "other"),
        "vessel": vessel, "site": "nksdocman", "siteName": "NKSDocMan",
        "subFolderPath": path, "modifiedEpoch": ep, "createdEpoch": ep,
        "modifiedBy": by, "createdBy": by, "sizeBytes": size, "fileSize": f"{size} B",
        "webUrl": f"https://example/{name}", "type": path.split(" > ")[-1],
    }


DOCS = [
    _doc("Main Engine Manual.pdf", "Belle Lune", "Technical and Crewing New > Belle Lune > Manuals > Engine", 2),
    _doc("Boiler Manual.pdf", "Belle Lune", "Technical and Crewing New > Belle Lune > Manuals", 40, by="John Tan"),
    _doc("General Arrangement.dwg", "Belle Lune", "Technical and Crewing New > Belle Lune > Drawings", 3, size=9000),
    _doc("Scan 001.pdf", "Belle Lune", "Technical and Crewing New > Belle Lune > To Be Classified", 1),
    _doc("Hull Plan.pdf", "Bow Fighter", "Technical and Crewing New > Bow Fighter > Drawings", 10),
    _doc("Crew List.xlsx", "Not Listed", "Shared", 5),
]


class _Backend:
    async def get_dashboard_documents(self, force_refresh=False, site_key=None):
        return {"docs": list(DOCS), "pending": False}


@pytest.fixture(autouse=True)
def _fake_backend(monkeypatch):
    monkeypatch.setattr(service, "get_backend", lambda: _Backend())
    monkeypatch.setattr(service.cfg, "is_configured", lambda: False)


def ask(q, context=None):
    return asyncio.run(service.ask(q, "all", context))


def test_vessel_and_group():
    r = ask("show manuals for MV Belle Lune")
    assert r["filters"]["vessel"] == "Belle Lune" and r["filters"]["group"] == "manuals"
    assert [x["name"] for x in r["results"]] == ["Main Engine Manual.pdf", "Boiler Manual.pdf"]
    assert r["results"][0]["web_url"].startswith("https://")


def test_keywords_match_names_and_folders():
    r = ask("main engine manual belle lune")
    assert r["results"][0]["name"] == "Main Engine Manual.pdf"


def test_count_question():
    r = ask("how many drawings do we have?")
    assert r["total"] == 2 and r["filters"]["intent"] == "count"
    assert "2" in r["answer"]


def test_vessel_overview():
    r = ask("Belle Lune")
    assert r["filters"]["intent"] == "overview" and r["total"] == 4
    assert r["breakdown"] == {"drawings": 1, "manuals": 2, "to_be_classified": 1, "other": 0}


def test_period_type_and_person():
    assert ask("pdfs uploaded in the last 7 days")["total"] == 2
    assert [x["name"] for x in ask("documents by John")["results"]] == ["Boiler Manual.pdf"]


def test_to_be_classified_and_largest():
    assert ask("what still needs sorting for belle lune")["filters"]["group"] == "to_be_classified"
    assert ask("largest files")["results"][0]["name"] == "General Arrangement.dwg"


def test_follow_up_keeps_vessel():
    first = ask("Belle Lune")
    r = ask("only drawings", context=first["filters"])
    assert r["filters"]["vessel"] == "Belle Lune" and [x["name"] for x in r["results"]] == ["General Arrangement.dwg"]


def test_no_match():
    r = ask("insurance certificate for bow fighter")
    assert r["total"] in (0, 1)
    assert r["answer"]


def test_sites_question_splits_per_site(monkeypatch):
    other = dict(_doc("Ext Plan.pdf", "Bow Fighter", "type of vessel > Bow Fighter > Drawings", 2))
    other.update(site="external", siteName="NissenKaiunExternal")
    monkeypatch.setattr(service, "DOCS_EXTRA", [other], raising=False)

    class _Two(_Backend):
        async def get_dashboard_documents(self, force_refresh=False, site_key=None):
            docs = list(DOCS) + [other]
            if site_key:
                docs = [d for d in docs if d["site"] == site_key]
            return {"docs": docs, "pending": False}

    monkeypatch.setattr(service, "get_backend", lambda: _Two())
    r = asyncio.run(service.ask("site?", "nksdocman", None))
    assert [b["name"] for b in r["by_site"]] == ["NKSDocMan", "NissenKaiunExternal"]
    assert "2 sites" in r["answer"]
    # Naming a site searches it even when another is selected.
    r = asyncio.run(service.ask("drawings in NissenKaiunExternal", "nksdocman", None))
    assert [x["name"] for x in r["results"]] == ["Ext Plan.pdf"]


def test_vague_question_gives_overview():
    r = ask("hello")
    assert "documents across" in r["answer"]


def test_ai_answer_uses_data_and_history(monkeypatch):
    calls = []

    async def fake_chat(system, messages, max_tokens=600, json_only=False):
        calls.append(messages)
        if json_only:
            return '{"vessel": "Belle Lune", "group": "manuals", "intent": "list", "keywords": []}'
        return "Belle Lune has **2** manuals; the newest is *Main Engine Manual.pdf*."

    monkeypatch.setattr(service.cfg, "is_configured", lambda: True)
    monkeypatch.setattr(service, "_chat", fake_chat)
    hist = [{"question": "Belle Lune", "answer": "Belle Lune has 4 documents."}]
    ctx = {"vessel": "Belle Lune", "group": None, "file_type": None}  # sent by the panel
    r = asyncio.run(service.ask("what about its manuals?", "all", ctx, hist))
    assert r["mode"] == "ai" and r["answer"].startswith("Belle Lune has **2** manuals")
    assert r["filters"]["vessel"] == "Belle Lune" and len(r["results"]) == 2
    # the rules understood it, so only one AI call (the answer) was made
    assert len(calls) == 1
    # history reaches the answer call; the data sent is the real search result
    assert calls[-1][0]["content"] == "Belle Lune" and "Main Engine Manual.pdf" in calls[-1][-1]["content"]

    # A question the rules can't place goes to the AI for interpretation first.
    calls.clear()
    r = asyncio.run(service.ask("engine stuff on the moon ship", "all", None, []))
    assert len(calls) == 2 and r["filters"]["vessel"] == "Belle Lune"


def test_ai_failure_falls_back_to_rules(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("offline")

    monkeypatch.setattr(service.cfg, "is_configured", lambda: True)
    monkeypatch.setattr(service, "_chat", boom)
    r = asyncio.run(service.ask("manuals for belle lune", "all", None, []))
    assert r["total"] == 2 and r["answer"]
