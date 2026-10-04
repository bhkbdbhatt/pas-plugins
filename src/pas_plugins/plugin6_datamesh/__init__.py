"""Plugin 6 - Unified Data Foundation / AI-Ready Data Mesh.

Customer, policy, claims and billing data sit in silos inside nearly every
carrier, and 54% of carriers spend more than half their IT budget keeping the
existing systems running. This plugin builds the layer *between* those systems -
it does not replace any of them - and makes the result queryable by people and by
AI agents alike.

Components
----------
1. **Ingestion** - batch and streaming pull from any source (via plugin 1's atomic
   operations, CDC streams, or batch extracts) into a unified record model.
2. **Master data management** - deterministic entity resolution and survivorship so
   "Jane Q. Public" appears once, not nine times.
3. **Data quality and lineage** - declarative quality rules scored per dataset,
   and OpenLineage-shaped lineage from source to golden record.
4. **Feature store** - versioned feature definitions with offline/online
   materialisation, TTLs and drift baselines, ready for plugin 3's models.
5. **Unified data API + MCP** - customer 360, policy history, claims and billing in
   one call, over REST and over the Model Context Protocol.
6. **PII masking and access control** - field-level masking, purpose limitation and
   a full access log, HIPAA/SOC 2 ready.
"""

from __future__ import annotations

from pas_plugins.plugin6_datamesh.settings import Plugin6Settings

__all__ = ["Plugin6Settings"]
