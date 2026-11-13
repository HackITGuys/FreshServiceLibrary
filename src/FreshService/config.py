"""Credentials and tunables for the Freshservice API v2.

Read from the environment and the `.env` in the current working directory, only when a
settings object (or a client) is created, never at import. Callers with their own
configuration can construct `FreshServiceSettings(...)` directly, or point at a different
file with `FreshServiceSettings(_env_file="path/to/.env")`.

The API key acts as the agent it belongs to: everything this library can do is bounded by
that agent's role. A key for an agent whose role cannot delete assets cannot delete assets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split(value: Any) -> Any:
    """'Hardware, Network' -> ['Hardware', 'Network']; lists pass through."""
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


class FreshServiceSettings(BaseSettings):
    """FRESH_* settings. Never log or serialise an instance of this."""

    model_config = SettingsConfigDict(
        env_prefix="FRESH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    domain: str = ""
    """yourcompany.freshservice.com. The API does not answer on a custom CNAME."""
    api_key: SecretStr = Field(
        default=SecretStr(""), validation_alias=AliasChoices("FRESH_API_KEY", "FRESH_KEY")
    )
    workspace_id: int | None = None
    """Unset: the primary workspace. 0 on list calls: every workspace the key can see."""

    timeout: int = 60
    max_attempts: int = 5
    page_size: int = Field(default=100, ge=1, le=100)
    """Records per page for paged endpoints (the API's maximum is 100)."""
    max_workers: int = Field(default=8, ge=1, le=16)
    """Parallel requests: pages of one listing, and fan-outs (installations per software,
    assets by id). Every request still counts against the account's per-minute limit."""

    agent_id: int | None = None
    """The agent that `--mine` / list_my_articles() means (see your profile URL, /agents/<id>).
    There is no /agents/me, and looking an agent up by email needs the "Manage Agents" role."""
    agent_email: str | None = None
    """Used to look up the agent id when FRESH_AGENT_ID is not set (needs "Manage Agents")."""
    portal_url: str | None = None
    """Base for links in reports (e.g. a portal CNAME). Default: https://{domain}."""
    device_asset_types: Annotated[list[str], NoDecode] = ["Hardware"]
    """Asset types (names or ids) that count as devices; their child types are included."""
    unknown_label: str = "Unknown"
    """Placeholder vendor name, and product-name prefix, for assets that have none."""

    template_file: Path | None = Field(
        default=None, validation_alias=AliasChoices("FRESH_TEMPLATE_FILE", "FRESH_TEMPLATE_FILEPATH")
    )
    default_email: str | None = Field(
        default=None, validation_alias=AliasChoices("FRESH_DEFAULT_EMAIL", "FRESH_DEFAULT_CONTACT_EMAIL")
    )
    default_department_id: int | None = Field(
        default=None,
        validation_alias=AliasChoices("FRESH_DEFAULT_DEPARTMENT_ID", "FRESH_DEFAULT_DEPT_ID"),
    )
    default_group_id: int | None = None
    default_category: str | None = None
    default_subject: str = "Ticket created by FreshServiceLibrary"

    @field_validator("domain")
    @classmethod
    def _bare_domain(cls, value: str) -> str:
        value = value.strip().removeprefix("https://").removeprefix("http://")
        return value.split("/", 1)[0]

    @field_validator("device_asset_types", mode="before")
    @classmethod
    def _split_types(cls, value: Any) -> Any:
        return _split(value)

    @field_validator("default_department_id", "default_group_id", "workspace_id", "agent_id", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        return None if value in ("", None) else value

    @field_validator("default_department_id", "default_group_id")
    @classmethod
    def _zero_id_is_none(cls, value: int | None) -> int | None:
        # The old env.sample used 0 for "not set"; 0 is never a valid department or group.
        return value or None

    @property
    def api_root(self) -> str:
        return f"https://{self.domain}/api/v2"

    @property
    def portal_root(self) -> str:
        return (self.portal_url or f"https://{self.domain}").rstrip("/")

    def missing(self) -> list[str]:
        """The required settings that are empty."""
        missing = []
        if not self.domain:
            missing.append("FRESH_DOMAIN")
        if not self.api_key.get_secret_value():
            missing.append("FRESH_API_KEY")
        return missing
