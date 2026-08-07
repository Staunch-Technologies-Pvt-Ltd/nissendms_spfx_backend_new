"""Backend service selection.

Exposes a single `get_backend()` returning either the real SharePoint Embedded +
PostgreSQL backend (when configured) or the in-memory stub. Both implement the
same async interface so the API layer has one code path.
"""
from ..config import settings

_real_backend = None
_stub_backend = None


def get_backend():
    global _real_backend, _stub_backend
    if settings.graph_configured and settings.db_configured:
        if _real_backend is None:
            from .real_backend import RealBackend

            _real_backend = RealBackend()
        return _real_backend
    else:
        if _stub_backend is None:
            from .stub_backend import StubBackend

            _stub_backend = StubBackend()
        return _stub_backend


def backend_mode() -> str:
    return get_backend().__class__.__name__.replace("Backend", "").lower()
