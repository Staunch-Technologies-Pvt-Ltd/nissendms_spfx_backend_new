"""Server-side helpers to capture client IP address and User-Agent.

IP and User-Agent are ALWAYS read from the HTTP request object — never from
the request body. This prevents clients from spoofing their own metadata.

Configuration
─────────────
TRUSTED_PROXY_HOPS controls how many reverse proxies (nginx, Azure App
Gateway, load balancer, etc.) sit in front of FastAPI.  Set it to 0 if
FastAPI is directly Internet-facing (uses request.client.host only).

With proxies, we read X-Forwarded-For and pick the first IP that is NOT from
a trusted proxy hop — i.e. the right-most untrusted entry:

  X-Forwarded-For: <real-client>, <proxy-1>, <proxy-2>
  TRUSTED_PROXY_HOPS = 2  →  picks <real-client>

Getting this value wrong lets a client forge X-Forwarded-For and spoof their
IP, so it must match your actual deployment topology.
"""
from __future__ import annotations

from fastapi import Request

from ..config import settings


def get_client_ip(request: Request) -> str | None:
    """Return the best-guess real client IP address.

    Strategy:
    1. If ``trusted_proxy_hops`` > 0, parse X-Forwarded-For and return the
       hop just before the trusted boundary (right-most untrusted entry).
    2. Otherwise fall back to ``request.client.host``.
    """
    hops = settings.trusted_proxy_hops
    xff = request.headers.get("x-forwarded-for", "")
    if hops > 0 and xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            # index of the first entry we trust = len(parts) - hops
            idx = max(len(parts) - hops, 0)
            return parts[idx]

    # Direct connection or no proxy header available
    if request.client:
        return request.client.host
    return None


def get_user_agent(request: Request) -> str | None:
    """Return the raw User-Agent header value sent by the browser."""
    return request.headers.get("user-agent") or None
