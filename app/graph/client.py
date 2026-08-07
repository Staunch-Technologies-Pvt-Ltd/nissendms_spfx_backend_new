"""Microsoft Graph client — app-only (client-credentials) auth.

Acquires a token for the backend Entra app via MSAL and exposes thin async
request helpers. Tokens are cached by MSAL and refreshed automatically.
"""
import asyncio
import random

import httpx
import msal

from ..config import settings


class GraphError(RuntimeError):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"Graph {status}: {message}")


class GraphClient:
    def __init__(self):
        self._app = msal.ConfidentialClientApplication(
            client_id=settings.graph_client_id,
            authority=settings.authority_url,
            client_credential=settings.graph_client_secret,
        )
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        # One pooled, keep-alive client reused across calls (avoids a new TLS
        # handshake per folder creation — the main provisioning bottleneck).
        if self._http is None or self._http.is_closed:
            from .http import verify

            self._http = httpx.AsyncClient(
                timeout=120,
                verify=verify(),
                limits=httpx.Limits(max_connections=30, max_keepalive_connections=30),
            )
        return self._http

    async def aclose(self):
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    def _token(self) -> str:
        result = self._app.acquire_token_silent([settings.graph_scope], account=None)
        if not result:
            result = self._app.acquire_token_for_client(scopes=[settings.graph_scope])
        if "access_token" not in result:
            raise GraphError(
                401,
                result.get("error_description", result.get("error", "token failure")),
            )
        return result["access_token"]

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Authorization": f"Bearer {self._token()}"}
        if extra:
            h.update(extra)
        return h

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        content: bytes | None = None,
        headers: dict | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{settings.graph_base_url}{path}"
        client = self._client()
        for attempt in range(4):
            resp = await client.request(
                method,
                url,
                json=json,
                content=content,
                headers=self._headers(headers),
                params=params,
            )
            # SharePoint Embedded throttles bursts (429) / transient 503.
            if resp.status_code in (429, 503) and attempt < 3:
                retry_after = resp.headers.get("Retry-After")
                delay = min(float(retry_after),10) if retry_after else min(2 ** attempt, 8)
                await asyncio.sleep(delay + random.random())
                continue
            break
        if resp.status_code >= 400:
            raise GraphError(resp.status_code, resp.text)
        return resp

    async def get(self, path: str, **kw) -> dict:
        return (await self.request("GET", path, **kw)).json()

    async def post(self, path: str, **kw) -> dict:
        r = await self.request("POST", path, **kw)
        return r.json() if r.content else {}

    async def patch(self, path: str, **kw) -> dict:
        r = await self.request("PATCH", path, **kw)
        return r.json() if r.content else {}

    async def delete(self, path: str, **kw) -> None:
        await self.request("DELETE", path, **kw)


_client: GraphClient | None = None


def graph() -> GraphClient:
    """Lazily-constructed singleton (only valid when Graph is configured)."""
    global _client
    if _client is None:
        _client = GraphClient()
    return _client
