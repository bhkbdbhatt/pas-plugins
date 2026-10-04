"""Plugin 5 entry point: the embedded distribution sidecar.

Run with::

    uvicorn pas_plugins.plugin5_embedded.main:app --port 8005
"""

from __future__ import annotations

import uvicorn

from pas_core.app import Platform, bootstrap, create_plugin_app
from pas_plugins.plugin5_embedded.api import build_router
from pas_plugins.plugin5_embedded.engine import DistributionEngine
from pas_plugins.plugin5_embedded.examples import catalog, demo_partner, starter_partner
from pas_plugins.plugin5_embedded.mcp_server import build_registry
from pas_plugins.plugin5_embedded.settings import Plugin5Settings

settings = Plugin5Settings()
platform: Platform = bootstrap(settings)

# Seeded so the service is demonstrable on first boot. A real deployment loads
# these from the carrier's product and partner registries.
engine = DistributionEngine(settings)
engine.register_partner(demo_partner())
engine.register_partner(starter_partner())
for product in catalog():
    engine.register_product(product)

mcp_registry = build_registry(engine)

app: Platform = create_plugin_app(
    settings,
    routers=[build_router(platform, engine)],
    platform=platform,
    mcp_registry=mcp_registry,
    description=(
        "Embedded Insurance Distribution.\n\n"
        "**Problem.** Carriers have spent two decades building point-of-sale platforms "
        "partners cannot use, and most distribution still runs on a phone call and a PDF. "
        "A partner integrating a carrier's platform should not need a bespoke integration "
        "per carrier.\n\n"
        "**Approach.** Distribution as an API. A partner gets a licensed catalog filtered "
        "by their own entitlement, quotes that name the premium *and* what they earn, "
        "onboarding with suitability and KYC gating, payment intents, and a commission "
        "statement that claws back inside free look. The sequence is the same one a "
        "carrier already runs - lead, quote, suitability, KYC, bind, pay, commission - so "
        "a partner is not learning a second business model.\n\n"
        "**The money rule.** Nothing here is a ledger of record. An intent records that "
        "money was requested; a payment records that it arrived; the carrier's finance "
        "system stays authoritative. `reconcile` exists so both sides compare against the "
        "same figures rather than each guessing.\n\n"
        "REST endpoints live under `/distribution`. the same fourteen capabilities are "
        "exposed as MCP tools over streamable HTTP at `/mcp`."
    ),
    tags=[
        {"name": "embedded-distribution", "description": "Catalog, quoting, onboarding, payments and commissions"},
        {"name": "platform", "description": "Health, discovery and governance"},
    ],
    extra_components={"mcp_registry": mcp_registry, "engine": engine},
)


def run() -> None:  # pragma: no cover - process entry point
    uvicorn.run(app, host="0.0.0.0", port=8005)  # noqa: S104


if __name__ == "__main__":  # pragma: no cover
    run()


__all__ = ["app", "engine", "mcp_registry", "settings"]