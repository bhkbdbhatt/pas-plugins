"""The seven PAS Plugins.

Each plugin is an independent, sellable sidecar module that augments - never
replaces - an existing Life & Annuary Policy Administration System.

============================  ====================================================
Plugin                        Purpose
============================  ====================================================
``plugin1_gateway``           AI-ready API gateway and MCP orchestrator
``plugin2_ifrs17``            IFRS 17 / regulatory automation engine
``plugin3_auw``               AI-powered accelerated underwriting workbench
``plugin4_productconfig``     Low-code product configuration engine
``plugin5_embedded``          Embedded insurance / API-first distribution
``plugin6_datamesh``          Unified data foundation / AI-ready data mesh
``plugin7_blockchain``        Blockchain policy lifecycle layer
============================  ====================================================

Build order (dependency chain): 1 -> 6 -> 2 -> 3 -> 4 -> 5 -> 7.
Plugin 6 depends on plugin 1 for ingestion; plugin 7 consumes plugin 1's events.
Plugins 2-5 are otherwise independent and can be licensed separately.
"""

from __future__ import annotations

__all__ = [
    "plugin1_gateway",
    "plugin2_ifrs17",
    "plugin3_auw",
    "plugin4_productconfig",
    "plugin5_embedded",
    "plugin6_datamesh",
    "plugin7_blockchain",
]
