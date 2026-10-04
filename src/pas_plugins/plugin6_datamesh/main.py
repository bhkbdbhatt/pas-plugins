"""Plugin 6 entry point.

Run with::

    uvicorn pas_plugins.plugin6_datamesh.main:app --port 8006
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from pas_core.app import Platform, bootstrap, create_plugin_app
from pas_core.observability import configure_logging
from pas_core.pii import build_policy
from pas_plugins.plugin6_datamesh.api import build_router
from pas_plugins.plugin6_datamesh.mcp_server import MCP_SERVER_NAME, build_registry
from pas_plugins.plugin6_datamesh.service import DataMeshService
from pas_plugins.plugin6_datamesh.settings import Plugin6Settings

settings = Plugin6Settings()
configure_logging(settings.log_level, fmt=settings.log_format)

platform: Platform = bootstrap(settings)

service = DataMeshService(
    pii_policy=build_policy(settings),
    match_threshold=settings.golden_record_match_threshold,
    quality_floor=settings.quality_gate_threshold,
    default_feature_ttl=settings.feature_ttl_seconds,
    lineage_enabled=settings.lineage_enabled,
)
service.register_demo_sources()

mcp_registry = build_registry(service)


@asynccontextmanager
async def lifespan(app: FastAPI) -> object:  # noqa: ANN401
    """Register the bundled features for every tenant on startup.

    Without this the catalogue would be empty until the first write, and an agent
    asking "what features exist?" would get an unhelpful answer on a fresh install.
    """
    from pas_core.tenancy import SYSTEM_PRINCIPAL, build_context  # noqa: PLC0415

    for tenant in platform.tenants.list():
        service.seed_features(build_context(tenant, SYSTEM_PRINCIPAL))
    yield


app = create_plugin_app(
    settings,
    routers=[build_router(platform, service)],
    mcp_registry=mcp_registry,
    platform=platform,
    lifespan=lifespan,
    description=(
        "**Unified Data Foundation / AI-Ready Data Mesh.**\n\n"
        "Builds the layer *between* a carrier's Policy Administration System, claims, "
        "billing and CRM - it replaces none of them - and serves one reconciled, "
        "quality-scored, lineage-tracked view of customer, policy, claims and premium "
        "data over REST and the Model Context Protocol.\n\n"
        "**What it guarantees.**\n"
        "- *Per-carrier isolation*: every source, record, feature and cache key is "
        "tenant-scoped.\n"
        "- *Purpose limitation*: every read must state why; a read with no purpose is "
        "refused, not served.\n"
        "- *PII masking by classification*: sensitive fields are masked unless the caller "
        "holds the unmasking scope, and the response says which fields were masked.\n"
        "- *Bronze / silver / gold*: only deduplicated, survivorship-resolved golden "
        "records are queryable, so an agent cannot present two conflicting versions of the "
        "same person as fact.\n\n"
        f"MCP server: `{MCP_SERVER_NAME}` over streamable HTTP at `/mcp`."
    ),
    tags=[
        {"name": "data-mesh", "description": "Ingestion, MDM, quality, lineage and features"},
        {"name": "platform", "description": "Health, discovery and governance"},
    ],
    extra_components={"data_mesh_service": service},
)


def run() -> None:  # pragma: no cover - process entry point
    uvicorn.run(app, host="0.0.0.0", port=8006)  # noqa: S104


if __name__ == "__main__":  # pragma: no cover
    run()
