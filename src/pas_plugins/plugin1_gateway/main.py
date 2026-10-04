"""Plugin 1 entry point.

Run with::

    uvicorn pas_plugins.plugin1_gateway.main:app --port 8001
"""

from __future__ import annotations

import uvicorn

from pas_core.app import Platform, bootstrap, create_plugin_app
from pas_plugins.plugin1_gateway import Plugin1Settings
from pas_plugins.plugin1_gateway.api import build_router
from pas_plugins.plugin1_gateway.config import PLUGIN_INFO
from pas_plugins.plugin1_gateway.mcp_server import build_registry
from pas_plugins.plugin1_gateway.operations import CATALOGUE
from pas_plugins.plugin1_gateway.service import get_workflow_engine

settings = Plugin1Settings()

platform: Platform = bootstrap(settings)
mcp_registry = build_registry(CATALOGUE)
get_workflow_engine()  # initialise the engine at import so /workflows is immediately accurate

app = create_plugin_app(
    settings,
    routers=[build_router(platform)],
    operations=CATALOGUE,
    mcp_registry=mcp_registry,
    platform=platform,
    description=(
        f"{PLUGIN_INFO.display_name}. {PLUGIN_INFO.description}\n\n"
        f"**Problem.** {PLUGIN_INFO.problem_statement}\n\n"
        "Every atomic operation is exposed three ways with one definition: as a REST "
        "endpoint (`/insurance/v1/...`), as an MCP tool over streamable HTTP at `/mcp`, "
        "and as a catalogued entry at `/operations`. The OpenAPI document is generated "
        "from the same declarations, so the spec cannot drift from behaviour."
    ),
    tags=[
        {"name": "operations", "description": "Atomic policy administration operations"},
        {"name": "orchestration", "description": "Business workflows over atomic operations"},
        {"name": "platform", "description": "Health, discovery and governance"},
    ],
    extra_components={"operations": CATALOGUE, "mcp_registry": mcp_registry},
)


def run() -> None:  # pragma: no cover - process entry point
    uvicorn.run(app, host="0.0.0.0", port=8001)  # noqa: S104


if __name__ == "__main__":  # pragma: no cover
    run()
