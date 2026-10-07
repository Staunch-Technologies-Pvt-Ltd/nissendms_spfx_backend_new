"""Configuration for the Documents Copilot.

Company policy: no external or free AI services. The Copilot answers from the
app's own document index with its built-in rules, and optionally uses the
company's OWN Azure OpenAI resource (data stays in the company's Azure
tenant and is not used for training). Questions about document *contents*
go to Microsoft 365 Copilot (the company's licence) through the "Open in
Microsoft 365 Copilot" link.

backend/.env (all optional):

    # Microsoft 365 Copilot: a SharePoint Copilot agent link for these
    # sites, or leave empty to open Microsoft 365 Copilot chat.
    COPILOT_M365_URL=https://<tenant>.sharepoint.com/sites/<site>/...agent link...

    # Company Azure OpenAI (only if approved by IT)
    AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com
    AZURE_OPENAI_API_KEY=<key>
    AZURE_OPENAI_DEPLOYMENT=<chat model deployment name>
    AZURE_OPENAI_API_VERSION=2024-08-01-preview   # optional, this is the default
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/copilot/config.py -> backend/.env (same file app/config.py uses)
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"

M365_COPILOT_DEFAULT_URL = "https://m365.cloud.microsoft/chat"


class _CopilotEnv(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), env_file_encoding="utf-8", extra="allow")
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_deployment: str = ""
    azure_openai_api_version: str = "2024-08-01-preview"
    copilot_m365_url: str = ""


_env = _CopilotEnv()

AZURE_OPENAI_ENDPOINT: str = _env.azure_openai_endpoint.strip().rstrip("/")
AZURE_OPENAI_API_KEY: str = _env.azure_openai_api_key.strip()
AZURE_OPENAI_DEPLOYMENT: str = _env.azure_openai_deployment.strip()
AZURE_OPENAI_API_VERSION: str = _env.azure_openai_api_version.strip() or "2024-08-01-preview"
M365_COPILOT_URL: str = _env.copilot_m365_url.strip() or M365_COPILOT_DEFAULT_URL


def provider() -> str | None:
    """'azure' when the company's Azure OpenAI is configured, else None."""
    if AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY and AZURE_OPENAI_DEPLOYMENT:
        return "azure"
    return None


def is_configured() -> bool:
    return provider() is not None
