"""Plugin 3 entry point: the AUW workbench sidecar.

Run with::

    uvicorn pas_plugins.plugin3_auw.main:app --port 8003
"""

from __future__ import annotations

import uvicorn

from pas_core.app import Platform, bootstrap, create_plugin_app
from pas_plugins.plugin3_auw.api import build_router
from pas_plugins.plugin3_auw.mcp_server import build_registry
from pas_plugins.plugin3_auw.service import default_workbench_service
from pas_plugins.plugin3_auw.settings import Plugin3Settings

settings = Plugin3Settings()
platform: Platform = bootstrap(settings)
mcp_registry = build_registry()
service = default_workbench_service()

app: Platform = create_plugin_app(
    settings,
    routers=[build_router(platform, service)],
    platform=platform,
    mcp_registry=mcp_registry,
    description=(
        "AI-Powered Accelerated Underwriting (AUW) Workbench.\n\n"
        "**Problem.** Underwriting cycle time is the dominant cost and the dominant "
        "customer-experience complaint in life insurance. Most of that time is spent on "
        "work a rule could have done: rekeying applications, chasing evidence, and "
        "routing straightforward cases to an underwriter who then declines to change "
        "anything.\n\n"
        "**Approach.** A single decision path from submission to issue: complexity triage "
        "routes the case, document extraction removes the rekeying, consent-gated "
        "enrichment adds MIB and pharmacy signals, a boosted model scores risk with exact "
        "feature attributions, and a versioned appetite ruleset decides. Every automated "
        "outcome carries the rules considered, the rules that fired, and the model's "
        "contributions, because a decline is the decision a carrier gets challenged on. "
        "Overrides are recorded, never silently applied.\n\n"
        "REST endpoints live under `/auw`. The same eleven capabilities are exposed as MCP "
        "tools over streamable HTTP at `/mcp`."
    ),
    tags=[
        {"name": "auw-workbench", "description": "Triage, extraction, scoring and decisions"},
        {"name": "platform", "description": "Health, discovery and governance"},
    ],
    extra_components={"mcp_registry": mcp_registry},
)


def run() -> None:  # pragma: no cover - process entry point
    uvicorn.run(app, host="0.0.0.0", port=8003)  # noqa: S104


if __name__ == "__main__":  # pragma: no cover
    run()


__all__ = ["app", "mcp_registry", "service", "settings"]