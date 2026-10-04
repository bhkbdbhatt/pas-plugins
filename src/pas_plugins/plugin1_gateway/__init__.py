"""Plugin 1 - AI-Ready API Gateway & MCP Orchestrator.

This is the foundation of the suite: every other plugin, and every AI agent,
reaches the carrier's Policy Administration System through it.

Responsibilities
----------------
1. **API discovery and translation** - decompose a monolithic PAS interface into
   atomic, single-purpose OpenAPI 3.1 operations and translate them to each
   vendor's dialect through the YAML rules engine.
2. **MCP server layer** - expose those atomic operations, plus resources and
   prompts, over the Model Context Protocol so agents can discover and call them.
3. **Workflow orchestration** - compose atomic operations into business processes
   with retries, compensation and approval gates.
4. **Security and governance** - OAuth 2.1, per-tenant rate limits, audit trail.

Module map
----------
``settings``     environment-driven configuration
``config``       plugin identity, problem statement and commercial model
``models``       request/response contracts for every atomic operation
``operations``   the atomic operation catalogue
``workflows``    business processes over atomic operations
``mcp_server``   MCP tools, resources and prompts
``service``      shared singletons (catalogue, invoker, workflow engine)
``api``          orchestration and diagnostics HTTP surface
``main``         application entry point
"""

from __future__ import annotations

from pas_plugins.plugin1_gateway.settings import Plugin1Settings

__all__ = ["Plugin1Settings"]
