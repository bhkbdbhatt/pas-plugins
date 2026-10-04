"""Typed configuration shared by every plugin.

Settings are read from the process environment using the ``PAS_`` prefix (see
``.env.example``).  Each plugin subclasses :class:`PluginSettings` and appends its
own fields, which keeps a single, greppable configuration surface for operators.
"""

from __future__ import annotations

import functools
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production"]
Backend = Literal["memory", "redis"]


class Settings(BaseSettings):
    """Common runtime settings for the whole suite."""

    model_config = SettingsConfigDict(
        env_prefix="PAS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    env: Environment = Field(
        default="development",
        description="Deployment environment. Production enables strict auth and signed audit logs.",
    )
    service_name: str = Field(default="pas-plugins", description="Logical service name.")
    log_level: str = Field(default="INFO")
    log_format: Literal["json", "console"] = "json"

    database_url: str = Field(
        default="postgresql+asyncpg://pas:pas@localhost:5432/pas_plugins",
        description="Async SQLAlchemy URL. Use sqlite+aiosqlite:///:memory: for tests.",
    )
    redis_url: str = Field(default="redis://localhost:6379/0")
    factory_backend: Backend = Field(
        default="memory",
        description="Backing store for idempotency keys, rate-limit buckets and workflow state.",
    )

    # --- identity ----------------------------------------------------------
    oidc_issuer: str = "https://keycloak.local/realms/pas-plugins"
    oidc_audience: str = "pas-plugins-api"
    oidc_jwks_url: str | None = None
    oidc_required: bool = Field(
        default=False,
        description="When true, unauthenticated requests are rejected. Forced on in production.",
    )
    tenant_header: str = "X-PAS-Tenant-Id"
    api_key_header: str = "X-PAS-Api-Key"

    # --- operational limits -------------------------------------------------
    request_timeout_seconds: float = 30.0
    max_request_bytes: int = 8 * 1024 * 1024
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["*"])
    docs_enabled: bool = True

    @field_validator("oidc_required")
    @classmethod
    def _force_auth_in_production(cls, value: bool, info: object) -> bool:
        data = getattr(info, "data", {}) or {}
        if data.get("env") == "production":
            return True
        return value

    @property
    def is_production(self) -> bool:
        return self.env == "production"


class PluginSettings(Settings):
    """Base class for plugin-specific settings."""

    plugin_id: str = "plugin0"
    plugin_version: str = "1.0.0"

    @property
    def jwks_url(self) -> str:
        if self.oidc_jwks_url:
            return self.oidc_jwks_url
        return f"{self.oidc_issuer.rstrip('/')}/protocol/openid-connect/certs"


@functools.lru_cache(maxsize=32)
def get_settings(cls: type[PluginSettings] = Settings) -> PluginSettings:  # noqa: UP006
    """Return a cached settings instance for the requested settings class."""
    return cls()  # type: ignore[return-value]


def reset_settings_cache() -> None:
    """Clear the settings cache (used by tests that patch the environment)."""
    get_settings.cache_clear()
