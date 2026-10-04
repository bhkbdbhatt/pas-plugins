"""Model Context Protocol support.

Every plugin in the suite exposes an MCP server so an AI agent (Claude, an
enterprise LLM, or an agentic workflow) can discover and call the carrier's
business capability without a bespoke integration.

Design decisions
----------------
* **Portable tool specs.** :class:`McpToolSpec` is our own declarative type, so
  tool catalogues can be published as JSON, diffed, audited and tested without an
  SDK in the loop. The official SDK is bridged in :mod:`pas_core.mcp.server`.
* **Tenant binding at session start.** The tenant is resolved from the transport
  credentials and installed for the duration of the call. An agent cannot name a
  different carrier in a tool argument.
* **Every call is audited and metered.** MCP traffic is the newest channel into a
  carrier's book, so it is treated with the same controls as the REST API.
* **Errors are catalogue errors.** A tool failure returns the same
  ``code``/``retryable`` structure as HTTP, so an agent can reason about whether
  to retry, fix its inputs, or escalate to a human.
"""

from __future__ import annotations

from pas_core.mcp.registry import (
    McpPromptSpec,
    McpResourceSpec,
    McpServerInfo,
    McpToolCall,
    McpToolRegistry,
    McpToolResult,
    McpToolSpec,
)
from pas_core.mcp.server import build_mcp_server, mcp_app, register_with_mcp_server

__all__ = [
    "McpPromptSpec",
    "McpResourceSpec",
    "McpServerInfo",
    "McpToolCall",
    "McpToolRegistry",
    "McpToolResult",
    "McpToolSpec",
    "build_mcp_server",
    "mcp_app",
    "register_with_mcp_server",
]
