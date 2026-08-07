"""Shared httpx TLS-verification setting.

Corporate networks often intercept TLS with a private root CA. `certifi` (httpx's
default) doesn't know that CA, so downloads from SharePoint's storage host fail
with CERTIFICATE_VERIFY_FAILED. `truststore` uses the OS trust store (where the
corporate root CA is installed), which fixes it securely — no disabling of
verification. Set GRAPH_VERIFY_SSL=false only as a last-resort escape hatch.
"""
import ssl

from ..config import settings

_verify = None


def verify():
    global _verify
    if _verify is not None:
        return _verify
    if not settings.graph_verify_ssl:
        _verify = False
    else:
        try:
            import truststore

            _verify = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            _verify = True  # fall back to certifi
    return _verify
