"""Plugin 7 entry point: the policy lifecycle ledger sidecar.

Run with::

    uvicorn pas_plugins.plugin7_blockchain.main:app --port 8007
"""

from __future__ import annotations

import uvicorn

from pas_core.app import Platform, bootstrap, create_plugin_app
from pas_plugins.plugin7_blockchain.api import build_router
from pas_plugins.plugin7_blockchain.mcp_server import build_registry
from pas_plugins.plugin7_blockchain.service import (
    default_ledger_service,
)
from pas_plugins.plugin7_blockchain.settings import Plugin7Settings

settings = Plugin7Settings()
platform: Platform = bootstrap(settings)
service = default_ledger_service()
mcp_registry = build_registry(service)

app: Platform = create_plugin_app(
    settings,
    routers=[build_router(platform, service)],
    platform=platform,
    mcp_registry=mcp_registry,
    description=(
        "Blockchain-Based Policy Lifecycle Layer.\n\n"
        "**Problem.** Policy records fragment. A policy is issued by one system, "
        "amended by another and serviced by a third, and when a carrier migrates "
        "platforms the beneficiary history is what gets lost. That produces slow "
        "beneficiary updates, unreconstructable annuity positions, and an assurance "
        "file nobody can prove.\n\n"
        "**What this is, precisely.** A real hash chain over policy events, a real "
        "Merkle tree per block, real Ed25519 signatures on identities, and a real "
        "endorsement policy: a transaction commits only once the organisations its "
        "channel policy names have signed it. What it is *not* is a Fabric gateway - "
        "Fabric is the deployment target and `LedgerBackend` is where it plugs in - "
        "and it does not use proof of work, because a permissioned network with named "
        "endorsers does not need one. What a carrier is buying is tamper-evidence and "
        "joint endorsement, not public-chain consensus, and pretending otherwise would "
        "be a worse design than the real thing.\n\n"
        "**Capabilities.** Policy lifecycle with legal-transition enforcement; "
        "beneficiary changes requiring multi-signature and recorded consent; annuity "
        "drawdown schedules whose obligation is recorded before any payment; W3C-style "
        "DIDs and verifiable credentials for beneficiaries; and signed portability "
        "packages another carrier can verify without trusting this ledger.\n\n"
        "Query endpoints live under `/blockchain/v1`. The same capabilities are exposed "
        "as MCP tools over streamable HTTP at `/mcp`."
    ),
    tags=[
        {"name": "policy-ledger", "description": "Lifecycle, beneficiaries, claims, annuities, identity"},
        {"name": "platform", "description": "Health, discovery and governance"},
    ],
    extra_components={"mcp_registry": mcp_registry, "ledger_service": service},
)


def run() -> None:  # pragma: no cover - process entry point
    uvicorn.run(app, host="0.0.0.0", port=8007)  # noqa: S104


if __name__ == "__main__":  # pragma: no cover
    run()


__all__ = ["app", "mcp_registry", "service", "settings"]