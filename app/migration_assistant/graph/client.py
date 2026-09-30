"""Microsoft Graph client — app-only (client-credentials) auth.

Acquires a token for the registered Entra app via MSAL and exposes thin async
request helpers. Tokens are cached by MSAL and refreshed automatically.

MSAL's own token/tenant-discovery calls go through `requests`, not `httpx` —
so they don't pick up `graph/http.py`'s truststore-based fix on their own.
`truststore.inject_into_ssl()` patches the stdlib `ssl` module globally so
anything built on it (including `requests`/`urllib3`, which MSAL uses) also
picks up the OS trust store — this is the fix for CERTIFICATE_VERIFY_FAILED
on corporate networks that intercept TLS with a private root CA.
"""
import asyncio
import random

import httpx
import msal

from ..config import settings

if settings.graph_verify_ssl:
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        pass  # fall back to certifi's default trust store


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
            # Escape hatch mirrors graph/http.py's verify() for the httpx
            # client — only relevant if GRAPH_VERIFY_SSL=false, since
            # inject_into_ssl() above already fixes verification otherwise.
            verify=settings.graph_verify_ssl,
        )
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        # One pooled, keep-alive client reused across calls (avoids a new TLS
        # handshake per request).
        if self._http is None or self._http.is_closed:
            from .http import verify

            self._http = httpx.AsyncClient(
                timeout=60,
                verify=verify(),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=10),
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
        for attempt in range(6):
            try:
                resp = await client.request(
                    method,
                    url,
                    json=json,
                    content=content,
                    headers=self._headers(headers),
                    params=params,
                )
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.TimeoutException) as e:
                # A transient network hiccup (DNS blip, dropped connection,
                # ...) never even gets a response to inspect, so this can't
                # be handled by the status-code retry below — without this,
                # one flaky network moment fails the whole document/move
                # outright instead of quietly retrying like a 429/503 does.
                if attempt >= 5:
                    raise GraphError(0, f"Network error calling Graph: {e}") from e
                await asyncio.sleep(min(2**attempt, 30) + random.random())
                continue
            # SharePoint throttles bursts (429) / transient 503.
            if resp.status_code in (429, 503) and attempt < 5:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else min(2**attempt, 30)
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

    async def batch(self, requests: list[dict]) -> list[dict]:
        """Execute up to 20 independent requests in one HTTP round-trip via
        Graph's `$batch` endpoint. Each item in `requests` is
        {"id", "method", "url", "body"?, "headers"?} where `url` is relative
        to the service root (e.g. "/drives/x/items/y"). Returns Graph's
        "responses" list, each {"id", "status", "body"} — a sub-request
        failing does not raise; the caller inspects each response's status.
        """
        if len(requests) > 20:
            raise ValueError("Graph $batch supports at most 20 requests per call")
        data = await self.post("/$batch", json={"requests": requests})
        return data.get("responses", [])


_client: GraphClient | None = None


def graph() -> GraphClient:
    """Lazily-constructed singleton (only valid when Graph is configured)."""
    global _client
    if _client is None:
        _client = GraphClient()
    return _client
