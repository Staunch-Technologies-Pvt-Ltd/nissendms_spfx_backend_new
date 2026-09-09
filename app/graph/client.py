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

    def _sp_token(self) -> str:
        """Acquire a SharePoint-scoped token (aud = tenant SharePoint root).

        SharePoint REST API endpoints reject the Graph-audience token with 401
        even when all permissions are granted and consented.  A separate token
        with scope ``https://<tenant>.sharepoint.com/.default`` is required.
        """
        sp_scope = settings.sharepoint_scope
        if not sp_scope:
            raise GraphError(400, "sharepoint_scope not configured — add {ENV}_SHAREPOINT_SITE_URL to .env")
        result = self._app.acquire_token_silent([sp_scope], account=None)
        if not result:
            result = self._app.acquire_token_for_client(scopes=[sp_scope])
        if "access_token" not in result:
            raise GraphError(
                401,
                result.get("error_description", result.get("error", "SP token failure")),
            )
        return result["access_token"]

    def _headers(self, extra: dict | None = None, access_token: str | None = None) -> dict:
        h = {"Authorization": f"Bearer {access_token or self._token()}"}
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
        access_token: str | None = None,
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{settings.graph_base_url}{path}"
        # Graph can throttle a burst for longer than the usual short retry
        # window. Keep retries bounded, but give 429 responses enough time to
        # recover before surfacing the error to the API caller.
        # Network-level errors (wsarecv / connection forcibly closed) are also
        # retried — the stale keep-alive connection is discarded and a fresh
        # client is created for the next attempt.
        _NETWORK_ERRORS = (
            httpx.ReadError,
            httpx.ConnectError,
            httpx.RemoteProtocolError,
            httpx.WriteError,
            httpx.PoolTimeout,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
        )
        for attempt in range(6):
            client = self._client()
            try:
                resp = await client.request(
                    method,
                    url,
                    json=json,
                    content=content,
                    headers=self._headers(headers, access_token),
                    params=params,
                )
            except _NETWORK_ERRORS as exc:
                if attempt >= 5:
                    raise
                # Discard the broken client so the next attempt gets a fresh connection
                try:
                    await self._http.aclose()
                except Exception:
                    pass
                self._http = None
                delay = min(2 ** attempt, 16) + random.random()
                await asyncio.sleep(delay)
                continue
            # SharePoint Embedded throttles bursts (429) / transient 503.
            if resp.status_code in (429, 503) and attempt < 5:
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = min(float(retry_after), 30) if retry_after else min(2 ** attempt, 16)
                except (TypeError, ValueError):
                    delay = min(2 ** attempt, 16)
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

    async def sp_request(
        self,
        method: str,
        url: str,
        *,
        json: dict | None = None,
        access_token: str | None = None,
    ) -> httpx.Response:
        """Call a SharePoint REST API endpoint using a SP-scoped token.

        The ``url`` must be the full URL (e.g.
        ``https://tenant.sharepoint.com/sites/NKSDocMan/_api/...``).
        If ``access_token`` is a delegated user token it is used as-is;
        otherwise the app acquires its own SP-scoped client-credentials token.
        """
        token = access_token or self._sp_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json;odata=nometadata",
            "Content-Type": "application/json;odata=nometadata",
        }
        _NETWORK_ERRORS = (
            httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError,
            httpx.WriteError, httpx.PoolTimeout, httpx.ConnectTimeout, httpx.ReadTimeout,
        )
        for attempt in range(4):
            client = self._client()
            try:
                resp = await client.request(method, url, json=json, headers=headers)
            except _NETWORK_ERRORS as exc:
                if attempt >= 3:
                    raise
                try:
                    await self._http.aclose()
                except Exception:
                    pass
                self._http = None
                import asyncio as _asyncio, random as _random
                await _asyncio.sleep(min(2 ** attempt, 8) + _random.random())
                continue
            if resp.status_code in (429, 503) and attempt < 3:
                import asyncio as _asyncio, random as _random
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = min(float(retry_after), 30) if retry_after else min(2 ** attempt, 16)
                except (TypeError, ValueError):
                    delay = min(2 ** attempt, 16)
                await _asyncio.sleep(delay + _random.random())
                continue
            break
        if resp.status_code >= 400:
            raise GraphError(resp.status_code, resp.text)
        return resp

    async def sp_post(self, url: str, *, json: dict | None = None, access_token: str | None = None) -> dict:
        """POST to a SharePoint REST API endpoint; returns parsed JSON."""
        r = await self.sp_request("POST", url, json=json, access_token=access_token)
        return r.json() if r.content else {}


_client: GraphClient | None = None
_site_clients: dict[str, GraphClient] = {}  # Cache clients per site


def graph(site_name: str | None = None) -> GraphClient:
    """Lazily-constructed singleton (only valid when Graph is configured).
    
    Args:
        site_name: If provided, return a client for that site. Otherwise use default.
    """
    global _client, _site_clients
    
    # If site_name is specified, use per-site caching
    if site_name:
        if site_name not in _site_clients:
            # Create a client for this site
            _site_clients[site_name] = GraphClient()
        return _site_clients[site_name]
    
    # Default: use global client
    if _client is None:
        _client = GraphClient()
    return _client


async def reset_graph_client(site_name: str | None = None):
    """Reset the cached graph client. Call this when switching sites.
    
    Args:
        site_name: If provided, reset only that site's client. Otherwise reset all.
    """
    global _client, _site_clients
    if site_name:
        if site_name in _site_clients:
            await _site_clients[site_name].aclose()
            del _site_clients[site_name]
    else:
        if _client is not None:
            await _client.aclose()
        _client = None
        for client in _site_clients.values():
            await client.aclose()
        _site_clients.clear()
