"""Plugin 2 entry point.

Run with::

    uvicorn pas_plugins.plugin2_ifrs17.main:app --port 8002
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from pas_core.app import Platform, bootstrap, create_plugin_app
from pas_core.observability import configure_logging
from pas_plugins.plugin2_ifrs17.api import build_router
from pas_plugins.plugin2_ifrs17.csm import CsmEngine
from pas_plugins.plugin2_ifrs17.connector import AssumptionStore, PasConnector, ValuationService
from pas_plugins.plugin2_ifrs17.library import worked_assumption_set
from pas_plugins.plugin2_ifrs17.mcp_server import MCP_SERVER_NAME, build_registry
from pas_plugins.plugin2_ifrs17.settings import Plugin2Settings

settings = Plugin2Settings()
configure_logging(settings.log_level, fmt=settings.log_format)

platform: Platform = bootstrap(settings)

assumptions = AssumptionStore()
for _tenant in platform.tenants.list():
    assumptions.publish(_tenant.tenant_id, worked_assumption_set())

service = ValuationService(
    engine=CsmEngine(),
    connector=PasConnector(),
    assumptions=assumptions,
    project_years=min(settings.projection_horizon_years, 40),
    write_reserves_back=settings.write_reserves_back_to_pas,
    currency=settings.functional_currency,
)

mcp_registry = build_registry(service)


@asynccontextmanager
async def lifespan(app: FastAPI) -> object:  # noqa: ANN401
    """Ensure every tenant has a usable assumption set at startup.

    A valuation with no assumption version is a 404, and a carrier trying the
    plugin for the first time should get a worked result rather than an error.
    """
    for tenant in platform.tenants.list():
        if not assumptions.versions(tenant.tenant_id):
            assumptions.publish(tenant.tenant_id, worked_assumption_set())
    yield


app = create_plugin_app(
    settings,
    routers=[build_router(platform, service)],
    mcp_registry=mcp_registry,
    platform=platform,
    lifespan=lifespan,
    description=(
        "**IFRS 17 / Regulatory Automation Engine.**\n\n"
        "Groups Life and Annuity contracts under IFRS 17 paragraph 4, measures them under "
        "the GMM, VFA or PAA, and produces the disclosure tables - the work that fewer "
        "than 10% of insurers have automated.\n\n"
        "**What it computes.**\n"
        "- Fulfilment cash flows by expected-value projection over a 30-year horizon.\n"
        "- A risk adjustment by Monte Carlo simulation, defined per IFRS 17 paragraph B88 "
        "as the charge for uncertainty: the confidence percentile less the mean. The best "
        "estimate stays in the fulfilment cash flows and is not charged twice.\n"
        "- The contractual service margin, with the paragraph 103 loss component applied "
        "where a group is onerous, and a roll-forward that reconciles item by item.\n"
        "- Interest accreted at the locked-in rate while insurance finance income uses the "
        "updated rate - the distinction most often got wrong.\n\n"
        "**What it does not compute.** The carrier's statutory reserve. NAIC SSAP 102 is "
        "reported as a mapping alongside the IFRS 17 figure, never as a substitute.\n\n"
        "**Traceability.** Every measurement returns its inputs, its assumption version, "
        "its curve identifiers, its method and its warnings. Every disclosure table "
        "carries a reconciliation status, and an unreconciled table is reported as such "
        "rather than presented as final.\n\n"
        f"MCP server: `{MCP_SERVER_NAME}` over streamable HTTP at `/mcp`."
    ),
    tags=[
        {"name": "ifrs17", "description": "Grouping, measurement, CSM and disclosure"},
        {"name": "platform", "description": "Health, discovery and governance"},
    ],
    extra_components={"valuation_service": service},
)


def run() -> None:  # pragma: no cover - process entry point
    uvicorn.run(app, host="0.0.0.0", port=8002)  # noqa: S104


if __name__ == "__main__":  # pragma: no cover
    run()
