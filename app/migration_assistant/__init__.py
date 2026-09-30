"""SharePoint AI Migration Assistant — merged into the Vessel DMS backend.

Previously a standalone FastAPI app (own port, own process). Now mounted by
`app/main.py` via `setup(app, require_session)`, so it starts and stops with
the DMS backend. Own database (SQLite by default) and own Entra app
registration are kept — see `config.py`.
"""
from .api import build_router, setup  # noqa: F401
