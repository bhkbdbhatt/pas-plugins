"""PAS Plugins platform core.

A shared, vendor-neutral foundation that every plugin in the suite builds on:

* :mod:`pas_core.config`       - typed, environment-driven settings
* :mod:`pas_core.tenancy`       - per-carrier isolation primitives
* :mod:`pas_core.errors`        - RFC 9457 problem details + insurance error catalogue
* :mod:`pas_core.security`      - OAuth 2.1 / OIDC authentication and scopes
* :mod:`pas_core.audit`         - tamper-evident, tenant-scoped audit trail
* :mod:`pas_core.ratelimit`     - per-tenant token buckets (Redis or in-memory)
* :mod:`pas_core.observability` - metrics, tracing and correlation IDs
* :mod:`pas_core.acord`         - ACORD NGDS canonical data model
* :mod:`pas_core.pas`           - PAS vendor adapters + translation rules engine
* :mod:`pas_core.rules`         - versioned, auditable rule DSL
* :mod:`pas_core.mcp`           - Model Context Protocol server construction
* :mod:`pas_core.workflow`      - deterministic workflow orchestration
* :mod:`pas_core.app`           - FastAPI application factory
"""

from __future__ import annotations

__all__ = ["__version__", "PRODUCT_NAME"]

__version__ = "1.0.0"
PRODUCT_NAME = "PAS Plugins"
