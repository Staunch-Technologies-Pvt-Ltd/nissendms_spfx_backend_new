"""Standalone Azure OpenAI configuration for the Documents Copilot.

Deliberately kept separate from ../config.py's `Settings` class: this is one
small, optional, account-wide feature (not a per-site SharePoint setting),
so it reads its own four keys directly from the same backend/.env file
instead of extending the shared multi-site Settings resolver.

Add to backend/.env to switch the Copilot from plain keyword search to
LLM-powered question understanding:

    AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com
    AZURE_OPENAI_API_KEY=<key>
    AZURE_OPENAI_DEPLOYMENT=<chat model deployment name>
    AZURE_OPENAI_API_VERSION=2024-08-01-preview   # optional, this is the default

Until those are set, `is_configured()` is False and service.py falls back
to a plain keyword/vessel-name search — the Copilot box still works, it
just can't parse a full sentence into filters.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/copilot/config.py -> backend/.env (same file app/config.py uses)
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


class _CopilotEnv(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), env_file_encoding="utf-8", extra="allow")
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_deployment: str = ""
    azure_openai_api_version: str = "2024-08-01-preview"


_env = _CopilotEnv()

AZURE_OPENAI_ENDPOINT: str = _env.azure_openai_endpoint.strip().rstrip("/")
AZURE_OPENAI_API_KEY: str = _env.azure_openai_api_key.strip()
AZURE_OPENAI_DEPLOYMENT: str = _env.azure_openai_deployment.strip()
AZURE_OPENAI_API_VERSION: str = _env.azure_openai_api_version.strip() or "2024-08-01-preview"


def is_configured() -> bool:
    return bool(AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY and AZURE_OPENAI_DEPLOYMENT)
