"""Plugin metadata: identity, pricing and the description used in every artifact.

Pricing figures mirror the commercial model in the specification.  They live in
code (rather than only in a slide deck) so the marketing site, the quote builder
and the ``/version`` endpoint can never disagree with the commercial position.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PLUGINS: dict[str, "PluginInfo"] = {}


@dataclass(frozen=True, slots=True)
class PluginInfo:
    """Identity and commercial framing for one plugin."""

    plugin_id: str
    sequence: int
    display_name: str
    tagline: str
    description: str
    problem_statement: str
    mcp_server_name: str
    mcp_instructions: str
    documentation_url: str
    base_price_range_usd: tuple[int, int] = (0, 0)
    pricing_unit: str = ""
    pricing_model: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    depends_on_pas: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "pluginId": self.plugin_id,
            "sequence": self.sequence,
            "displayName": self.display_name,
            "tagline": self.tagline,
            "description": self.description,
            "problemStatement": self.problem_statement,
            "mcpServerName": self.mcp_server_name,
            "documentationUrl": self.documentation_url,
            "basePriceRangeUsd": list(self.base_price_range_usd),
            "pricingUnit": self.pricing_unit,
            "pricingModel": self.pricing_model,
            "dependsOn": list(self.depends_on),
            "requiresPas": self.depends_on_pas,
        }


PLUGIN_INFO = PluginInfo(
    plugin_id="plugin1",
    sequence=1,
    display_name="AI-Ready API Gateway & MCP Orchestrator",
    tagline="Make any legacy PAS AI-ready in weeks, without replacing it.",
    description=(
        "Decomposes a carrier's monolithic, proprietary Policy Administration APIs into "
        "atomic OpenAPI 3.1 operations, translates them live to each vendor's dialect, "
        "exposes them over the Model Context Protocol for AI agents, and orchestrates them "
        "into business workflows with retries and compensation."
    ),
    problem_statement=(
        "Legacy PAS APIs are monolithic, use proprietary DSLs and are not discoverable by "
        "AI agents. An agent cannot call an operation it cannot discover, describe or "
        "authorise separately - which is why 90% of carriers' AI pilots stall at the "
        "integration layer rather than at the model."
    ),
    mcp_server_name="pas-policy-operations",
    mcp_instructions=(
        "You are connected to a carrier's Policy Administration System through an "
        "atomicity-first gateway. Every tool maps to exactly one business intent with a "
        "complete contract.\n\n"
        "How to work:\n"
        "  1. Call `catalogue_list_operations` first. Never guess a tool name.\n"
        "  2. Read the product with `product_get` before quoting it; respect filed issue "
        "ages, face-amount bounds and states.\n"
        "  3. `policy_premium_calculate` is pure computation - safe to call while "
        "exploring options. It creates nothing.\n"
        "  4. `policy_bind` issues a real policy. It requires an accepted quote and "
        "\"confirm\": true. Treat it as irreversible.\n"
        "  5. For anything that changes policy state, state the intended change to the "
        "user and obtain approval before calling the tool.\n"
        "  6. On error, read the `code`. Retry only when `retryable` is true; otherwise fix "
        "the inputs or escalate to a human.\n\n"
        "All dates are ISO-8601. Amounts are decimal numbers in the stated currency. "
        "Coverage terminology follows ACORD NGDS Life and Annuity."
    ),
    documentation_url="https://docs.pas-plugins.io/plugins/1-gateway",
    base_price_range_usd=(50_000, 150_000),
    pricing_unit="per PAS integration (one-time), plus metered usage",
    pricing_model={
        "perPasIntegrationLicense": {"low": 50_000, "high": 150_000, "currency": "USD"},
        "perApiCall": {"low": 0.01, "high": 0.05, "currency": "USD", "unit": "call"},
        "mcpAccessPerCarrier": {"low": 5_000, "high": 20_000, "currency": "USD", "unit": "month"},
    },
    depends_on_pas=True,
)
PLUGINS[PLUGIN_INFO.plugin_id] = PLUGIN_INFO

MCP_INSTRUCTIONS = PLUGIN_INFO.mcp_instructions
