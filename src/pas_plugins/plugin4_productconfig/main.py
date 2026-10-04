"""Plugin 4 entry point: the low-code product configuration sidecar.

Run with::

    uvicorn pas_plugins.plugin4_productconfig.main:app --port 8004
"""

from __future__ import annotations

import uvicorn

from pas_core.app import Platform, bootstrap, create_plugin_app
from pas_plugins.plugin4_productconfig.api import build_router
from pas_plugins.plugin4_productconfig.mcp_server import build_registry
from pas_plugins.plugin4_productconfig.service import default_product_config_service
from pas_plugins.plugin4_productconfig.settings import Plugin4Settings

settings = Plugin4Settings()
platform: Platform = bootstrap(settings)
service = default_product_config_service()
mcp_registry = build_registry(service)

app: Platform = create_plugin_app(
    settings,
    routers=[build_router(platform, service)],
    platform=platform,
    mcp_registry=mcp_registry,
    description=(
        "Low-Code Product Configuration.\n\n"
        "**Problem.** 58% of carriers report that product configuration is a "
        "bottleneck for launching new products, and 67% say it holds back innovation. "
        "The cause is rarely appetite - it is that a new product means code, and code "
        "means a release cycle measured in months.\n\n"
        "**Approach.** A product becomes a document. Covers, benefits, charges, rating "
        "tables, eligibility rules and compliance constraints are all data, validated "
        "structurally on every save; a single fixed engine interprets them. The author "
        "cannot inject arbitrary logic, only the constructs the engine understands - "
        "and that engine is tested and certified once rather than per product.\n\n"
        "Before anything is published, the definition passes guardrails that check band "
        "partitioning, referential integrity, margin and the regulatory constraints a "
        "filing reviewer would check. Publication produces an immutable content-hashed "
        "version; drafts stay mutable, published versions never do. A blocker stops "
        "publication, a warning does not, and whoever accepted a warning is recorded on "
        "the version.\n\n"
        "REST endpoints live under `/products`. The same twelve capabilities are exposed "
        "as MCP tools over streamable HTTP at `/mcp`."
    ),
    tags=[
        {"name": "product-config", "description": "Authoring, validation, simulation and publication"},
        {"name": "platform", "description": "Health, discovery and governance"},
    ],
    extra_components={"mcp_registry": mcp_registry, "service": service},
)


def run() -> None:  # pragma: no cover - process entry point
    uvicorn.run(app, host="0.0.0.0", port=8004)  # noqa: S104


if __name__ == "__main__":  # pragma: no cover
    run()


__all__ = ["app", "mcp_registry", "service", "settings"]