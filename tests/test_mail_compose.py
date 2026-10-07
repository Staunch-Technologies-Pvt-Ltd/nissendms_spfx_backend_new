"""Compose email: recipients, SharePoint attachments/links, sender fallback."""
from __future__ import annotations

import asyncio

import pytest

from app.graph.client import GraphError
from app.mail_compose import service

USER_TOKEN = "a.b.c"


class FakeGraph:
    def __init__(self, deny_me=False):
        self.calls: list[tuple] = []
        self.deny_me = deny_me

    async def post(self, path, json=None, access_token=None, **kw):
        self.calls.append(("POST", path, json, access_token))
        if self.deny_me and path.startswith("/me/"):
            raise GraphError(403, "denied")
        if path.endswith("/createLink"):
            return {"link": {"webUrl": "https://share/folder-link"}}
        if path.endswith("/createUploadSession"):
            return {"uploadUrl": "https://upload/session"}
        if path.endswith("/messages"):
            return {"id": "draft1"}
        return {}

    async def get(self, path, **kw):
        self.calls.append(("GET", path, None, kw.get("access_token")))
        return {"id": "f1", "name": "Plans", "webUrl": "https://sp/Plans", "folder": {}}

    async def delete(self, path, **kw):
        self.calls.append(("DELETE", path, None, kw.get("access_token")))


@pytest.fixture
def fake(monkeypatch):
    g = FakeGraph()
    monkeypatch.setattr(service, "graph", lambda: g)
    monkeypatch.setattr(service.settings, "graph_configured", True, raising=False)
    monkeypatch.setattr(service.settings, "graph_sender_mailbox", "dms@contoso.com", raising=False)

    async def download(drive_id, item_id, access_token=None):
        size = 5 * 1024 * 1024 if item_id == "big" else 10
        return b"x" * size, "application/pdf", f"{item_id}.pdf"

    monkeypatch.setattr(service.gd, "download_file", download)
    uploads = []

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def put(self, url, content=None, headers=None):
            uploads.append(headers["Content-Range"])
            class R: status_code = 200
            return R()

    monkeypatch.setattr(service.httpx, "AsyncClient", _Client)
    g.uploads = uploads
    return g


def run(**kw):
    base = dict(sender_email="deepa@contoso.com", sender_name=None, to=["a@contoso.com"], cc=[], bcc=[],
                subject="Drawings", body_html="<p>Hi</p>", importance="normal", items=[], local_files=[],
                user_token=USER_TOKEN)
    base.update(kw)
    return asyncio.run(service.send(**base))


def test_sends_from_user_mailbox_with_attachment_and_folder_link(fake):
    r = run(items=[{"drive_id": "d", "item_id": "small"},
                   {"drive_id": "d", "item_id": "f1", "is_folder": True}])
    assert r["sent_as"] == "user" and r["attachments"] == 1 and r["links"] == 1
    draft = next(c for c in fake.calls if c[1] == "/me/messages")
    assert draft[3] == USER_TOKEN and "https://share/folder-link" in draft[2]["body"]["content"]
    assert ("POST", "/me/messages/draft1/send", None, USER_TOKEN) in fake.calls


def test_large_file_uses_upload_session(fake):
    run(items=[{"drive_id": "d", "item_id": "big"}])
    assert any(c[1].endswith("/createUploadSession") for c in fake.calls)
    assert fake.uploads[0].startswith("bytes 0-") and fake.uploads[-1].endswith(f"/{5 * 1024 * 1024}")


def test_falls_back_to_system_mailbox_with_reply_to(fake):
    fake.deny_me = True
    r = run()
    assert r["sent_as"] == "system"
    draft = next(c for c in fake.calls if c[1] == "/users/dms@contoso.com/messages")
    assert draft[2]["replyTo"][0]["emailAddress"]["address"] == "deepa@contoso.com"


def test_address_validation():
    assert service.clean_addresses(["a@x.com; b@y.org", "A@x.com"], "To") == ["a@x.com", "b@y.org"]
    with pytest.raises(service.MailError):
        service.clean_addresses(["not-an-email"], "Cc")


def test_needs_a_recipient(fake):
    with pytest.raises(service.MailError):
        run(to=[])


def test_people_search_ranks_prefix_first(monkeypatch):
    async def directory():
        return [{"name": "Anand Kumar", "email": "anand@x.com"}, {"name": "Priya Raman", "email": "priya@x.com"},
                {"name": "Raman Iyer", "email": "riyer@x.com"}]
    monkeypatch.setattr(service, "_directory", directory)
    names = [p["name"] for p in asyncio.run(service.search_people("raman"))]
    assert names == ["Priya Raman", "Raman Iyer"]


def test_send_from_approved_shared_mailbox(fake, monkeypatch):
    monkeypatch.setenv("MAIL_FROM_ADDRESSES", "technical@contoso.com")
    r = run(from_address="technical@contoso.com")
    assert r["sent_as"] == "shared" and r["from"] == "technical@contoso.com"
    draft = next(c for c in fake.calls if c[1] == "/users/technical@contoso.com/messages")
    assert draft[3] is None and draft[2]["replyTo"][0]["emailAddress"]["address"] == "deepa@contoso.com"


def test_unapproved_from_address_is_refused(fake, monkeypatch):
    monkeypatch.setenv("MAIL_FROM_ADDRESSES", "technical@contoso.com")
    with pytest.raises(service.MailError) as err:
        run(from_address="ceo@contoso.com")
    assert err.value.status == 403 and not fake.calls
