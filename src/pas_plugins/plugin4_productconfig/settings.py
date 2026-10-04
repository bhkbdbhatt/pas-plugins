"""Plugin 4 - Low-Code Product Configuration.

58% of carriers report that product configuration is a bottleneck for launching
new products, and 67% say it holds back innovation. The cause is rarely appetite -
it is that a new product means code, and code means a release cycle measured in
months. This plugin makes a product a *document*: validated, versioned, simulatable
and publishable without a deployment.

The design leans on one decision: a product definition is data, and the engine that
evaluates it is fixed. Everything a product author writes is data - coverage
benefits, rating tables, charges, eligibility and compliance rules - and a single
`ProductEngine` interprets it. That separation is what makes "no-code" honest: the
author cannot express arbitrary logic, only the constructs the engine understands,
and the engine is the thing that gets tested and certified once rather than per
product.

What the author gets without writing code:

1. **A schema-backed editor model** - covers, benefits, charges, eligibility and
   rules, validated structurally on every save.
2. **A rating engine** - base rates by age band, adjusted by multiplicative factors
   and band sums, with a cross-reference table for consistency checking.
3. **Compliance rules** - rate-to-value, face-amount caps, issue-age limits and
   state eligibility, evaluated per state and per product.
4. **Guardrails** - structural checks that catch the mistakes that actually happen
   when products are configured rather than coded: missing charges, negative
   margins, overlapping rate bands, unreferenced benefits.
5. **Versioning and simulation** - draft, publish, diff, and simulate against a
   portfolio before anything reaches a policyholder.

Nothing here writes rates into a live book. Publication produces an immutable
version that downstream valuation and quoting can consume read-only.
"""

from __future__ import annotations

from pas_core.config import PluginSettings


class Plugin4Settings(PluginSettings):
    """Configuration for the low-code product configuration service."""

    plugin_id: str = "plugin4"
    plugin_version: str = "1.0.0"
    service_name: str = "pas-product-config"

    max_products_per_tenant: int = 500
    max_versions_per_product: int = 50
    """Older versions beyond this are pruned; audit keeps the hash chain."""
    min_version_for_publish: int = 1
    require_compliance_pass: bool = True
    """A product cannot be published while a blocking compliance finding stands."""
    allow_negative_margin: bool = False
    """Margin guardrail. Disabling it is permitted but recorded in the version."""
    default_currency: str = "USD"
    effective_rate_tolerance: float = 0.005
    """Relative tolerance when checking rate-to-value compliance (50bp)."""
    documentation_url: str = "https://docs.pas-plugins.io/plugins/product-config"


__all__ = ["Plugin4Settings"]