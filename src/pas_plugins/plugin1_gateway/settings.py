"""Configuration for plugin 1 - the AI-ready API gateway and MCP orchestrator."""

from __future__ import annotations

from pydantic import Field

from pas_core.config import PluginSettings


class Plugin1Settings(PluginSettings):
    """Environment-driven configuration for the gateway."""

    plugin_id: str = "plugin1"
    plugin_version: str = "1.0.0"
    service_name: str = "pas-ai-gateway"

    gateway_name: str = "pas-ai-gateway"
    gateway_policy: str = Field(
        default="per-tenant-rls",
        description="Gateway rate-limit policy name; enforced per tenant by pas_core.ratelimit.",
    )
    mcp_enabled: bool = True
    mcp_transport: str = "streamable-http"
    workflow_engine: str = "local"
    default_pas_vendor: str = "simulated"
    pas_base_url: str | None = None
    pas_timeout_seconds: float = 30.0
    retry_max_attempts: int = 3
    documentation_url: str = "https://docs.pas-plugins.io/plugins/gateway"


__all__ = ["Plugin1Settings"]
