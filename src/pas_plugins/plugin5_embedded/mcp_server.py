"""MCP tools for embedded distribution.

Fourteen tools covering the whole partner lifecycle: browse the catalog, quote,
screen, bind, take payment, and read the commission statement.

The one tool an agent must not drive unattended is `distribution_bind`. Binding
issues a real contract of insurance on a real person, so it is annotated
destructive and requires `confirm=true`. Everything else - catalog browsing,
quoting, screening - is either read-only or writes only to a lead or a quote,
which are drafts in the same sense that a product draft is: cheap to abandon.

Screening is deliberately read-only here. An agent may ask whether a consumer
clears, but the answer it gets back is "potential match, requires manual review"
for anything ambiguous, and it cannot turn that into a clear.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pas_core.mcp.registry import (
    McpResourceSpec,
    McpServerInfo,
    McpToolAnnotations,
    McpToolRegistry,
    McpToolSpec,
)
from pas_core.tenancy import TenantContext
from pas_plugins.plugin5_embedded.engine import DistributionEngine, DistributionError
from pas_plugins.plugin5_embedded.examples import catalog as demo_catalog
from pas_plugins.plugin5_embedded.models import ApplicantProfile
from pas_plugins.plugin5_embedded.settings import Plugin5Settings

PLUGIN_INFO = McpServerInfo(
    name="pas-embedded-distribution",
    version=Plugin5Settings().plugin_version,
    title="PAS Embedded Distribution",
    description=(
        "Embedded insurance distribution: a partner-visible catalog, quotes that name the "
        "partner's commission, suitability-assessed onboarding with KYC, payment intents, "
        "and commission statements with free-look clawback."
    ),
)


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_APPLICANT = {
    "firstName": {"type": "string"},
    "lastName": {"type": "string"},
    "dateOfBirth": {"type": "string", "description": "ISO date, YYYY-MM-DD."},
    "stateOfResidence": {"type": "string", "minLength": 2, "maxLength": 2},
    "smokingStatus": {
        "type": "string",
        "enum": ["never", "former", "current", "occasional"],
        "default": "never",
    },
    "coverageNeed": {
        "type": "number",
        "description": "The consumer's own stated need. Drives suitability, not price.",
    },
    "existingCoverage": {"type": "number", "default": 0},
    "annualIncome": {"type": "number", "default": 0},
    "dependents": {"type": "integer", "default": 0},
}


def build_registry(engine: DistributionEngine | None = None) -> McpToolRegistry:
    """Construct the registry of embedded distribution tools."""
    eng = engine or DistributionEngine()
    registry = McpToolRegistry(PLUGIN_INFO)

    def _applicant(arguments: dict[str, Any]) -> ApplicantProfile:
        return ApplicantProfile.model_validate(arguments.get("applicant") or {})

    async def list_catalog(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        products = eng.list_catalog(
            str(arguments["partnerId"]),
            state=arguments.get("state"),
            category=arguments.get("category"),
        )
        return {
            "partnerId": arguments["partnerId"],
            "count": len(products),
            "products": [p.to_dict() for p in products],
        }

    async def get_product(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        product = eng.products.get(str(arguments["productId"]))
        if product is None:
            return {"error": "productNotFound"}
        return {"product": product.to_dict()}

    async def get_partner(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        partner = eng.get_partner(str(arguments["partnerId"]))
        if partner is None:
            return {"error": "partnerNotFound"}
        return {"partner": partner.to_dict()}

    async def quote(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        try:
            result = eng.quote(
                tenant_id=ctx.tenant.tenant_id,
                partner_id=str(arguments["partnerId"]),
                product_id=str(arguments["productId"]),
                applicant=_applicant(arguments),
                face_amount=float(arguments["faceAmount"]),
                term_years=arguments.get("termYears"),
                lead_id=arguments.get("leadId"),
            )
        except DistributionError as exc:
            return {"error": "cannotQuote", "detail": str(exc)}
        return {"quote": result.to_dict()}

    async def get_quote(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        found = eng.get_quote(str(arguments["quoteId"]))
        if found is None:
            return {"error": "quoteNotFound"}
        return {"quote": found.to_dict()}

    async def assess_suitability(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        """Test a proposed cover against the consumer's own stated need."""
        applicant = _applicant(arguments)
        product = eng.products.get(str(arguments["productId"]))
        if product is None:
            return {"error": "productNotFound"}
        assessment = eng._assess_suitability(  # noqa: SLF001
            applicant, float(arguments["faceAmount"]), product
        )
        return {"suitability": assessment.to_dict()}

    async def screen(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        try:
            result = eng.screen(
                str(arguments["quoteId"]),
                str(arguments.get("documentType", "unknown")),
                str(arguments.get("documentReference", "unknown")),
            )
        except DistributionError as exc:
            return {"error": "screeningFailed", "detail": str(exc)}
        return {"quoteId": arguments["quoteId"], "kyc": result.to_dict()}

    async def bind(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        try:
            policy = eng.bind(str(arguments["quoteId"]))
        except DistributionError as exc:
            return {"error": "cannotBind", "detail": str(exc)}
        return {"policy": policy.to_dict()}

    async def get_policy(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        policy = eng.get_policy(str(arguments["policyId"]))
        if policy is None:
            return {"error": "policyNotFound"}
        return {"policy": policy.to_dict()}

    async def create_intent(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        from pas_plugins.plugin5_embedded.models import PaymentMethod  # noqa: PLC0415

        try:
            intent = eng.create_payment_intent(
                str(arguments["quoteId"]),
                PaymentMethod(arguments.get("method", "ach")),
            )
        except DistributionError as exc:
            return {"error": "cannotCreateIntent", "detail": str(exc)}
        return {"intent": intent.to_dict()}

    async def reconcile(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        rows = eng.reconcile(str(arguments["partnerId"]))
        return {
            "partnerId": arguments["partnerId"],
            "rowCount": len(rows),
            "totalOutstanding": round(sum(r.outstanding for r in rows if r.outstanding > 0), 2),
            "rows": [r.to_dict() for r in rows],
        }

    async def statement(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        try:
            result = eng.commission_statement(
                ctx.tenant.tenant_id,
                str(arguments["partnerId"]),
                date.fromisoformat(str(arguments["periodStart"])),
                date.fromisoformat(str(arguments["periodEnd"])),
            )
        except (DistributionError, ValueError) as exc:
            return {"error": "cannotBuildStatement", "detail": str(exc)}
        return {"statement": result.to_dict()}

    async def events(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        found = eng.events(arguments.get("partnerId"))
        return {
            "count": len(found),
            "deliveredCount": sum(1 for e in found if e.delivered),
            "events": [e.to_dict() for e in found],
        }

    async def meta(_: dict[str, Any], __: TenantContext) -> dict[str, Any]:
        from pas_plugins.plugin5_embedded.engine import (  # noqa: PLC0415
            INSTANT_DECISION_FACE_LIMIT,
            OVER_INSURED_BLOCK_RATIO,
            OVER_INSURED_REVIEW_RATIO,
        )
        from pas_plugins.plugin5_embedded.models import (  # noqa: PLC0415
            PartnerTier,
            PaymentMethod,
        )

        settings = eng.settings
        return {
            "quoteTtlMinutes": settings.quote_ttl_minutes,
            "freeLookDays": settings.free_look_days,
            "gates": ["suitability", "kyc", "bind", "pay", "commission"],
            "suitabilityThresholds": {
                "reviewRatio": OVER_INSURED_REVIEW_RATIO,
                "blockRatio": OVER_INSURED_BLOCK_RATIO,
                "instantDecisionFaceLimit": INSTANT_DECISION_FACE_LIMIT,
            },
            "partnerTiers": {
                str(tier): {
                    "defaultCommissionBps": tier.default_commission_bps,
                    "settlementDays": tier.settlement_days,
                }
                for tier in PartnerTier
            },
            "paymentMethods": [str(m) for m in PaymentMethod],
        }

    async def catalog_resource(_: TenantContext) -> dict[str, Any]:
        return {"products": [p.to_dict() for p in demo_catalog()]}

    partner_id = {"partnerId": {"type": "string", "description": "The distribution partner."}}
    quote_id = {"quoteId": {"type": "string"}}

    specs = [
        McpToolSpec(
            name="distribution_get_capabilities",
            title="Get Distribution Capabilities",
            description=(
                "The rules this deployment enforces: quote lifetime, free-look period, the "
                "binding gate order, suitability thresholds, partner tiers and commission rates."
            ),
            input_schema=_schema({}, []),
            handler=meta,
            required_scopes=("distribution:read",),
            plugin_id="plugin5",
            operation_id="distribution_get_capabilities",
            tags=("distribution", "discovery"),
        ),
        McpToolSpec(
            name="distribution_list_catalog",
            title="List Partner Catalog",
            description=(
                "Products a partner is licensed to sell, filtered by their entitlement, tier "
                "and the consumer's state. A partner never sees a product they may not sell."
            ),
            input_schema=_schema(
                {
                    **partner_id,
                    "state": {"type": "string", "minLength": 2, "maxLength": 2},
                    "category": {"type": "string"},
                },
                ["partnerId"],
            ),
            handler=list_catalog,
            required_scopes=("distribution:read",),
            plugin_id="plugin5",
            operation_id="distribution_list_catalog",
            tags=("distribution", "catalog"),
        ),
        McpToolSpec(
            name="distribution_get_product",
            title="Get Product Detail",
            description="One product with its rates, eligibility, commission basis and gating flags.",
            input_schema=_schema({"productId": {"type": "string"}}, ["productId"]),
            handler=get_product,
            required_scopes=("distribution:read",),
            plugin_id="plugin5",
            operation_id="distribution_get_product",
            tags=("distribution", "catalog"),
        ),
        McpToolSpec(
            name="distribution_get_partner",
            title="Get Partner",
            description="A partner's licence, tier, product entitlements and annual quota.",
            input_schema=_schema(partner_id, ["partnerId"]),
            handler=get_partner,
            required_scopes=("distribution:read",),
            plugin_id="plugin5",
            operation_id="distribution_get_partner",
            tags=("distribution", "partners"),
        ),
        McpToolSpec(
            name="distribution_assess_suitability",
            title="Assess Suitability",
            description=(
                "Test whether a proposed cover suits the consumer's own stated need. "
                "Selling cover far beyond a stated need is a conduct problem even when the "
                "premium is correctly calculated."
            ),
            input_schema=_schema(
                {
                    "productId": {"type": "string"},
                    "faceAmount": {"type": "number", "exclusiveMinimum": 0},
                    "applicant": _APPLICANT,
                },
                ["productId", "faceAmount", "applicant"],
            ),
            handler=assess_suitability,
            required_scopes=("distribution:read",),
            plugin_id="plugin5",
            operation_id="distribution_assess_suitability",
            tags=("distribution", "conduct"),
        ),
        McpToolSpec(
            name="distribution_quote",
            title="Quote Product",
            description=(
                "Price a policy for a consumer, itemising base premium, tobacco loading and "
                "the partner's commission, with a suitability assessment and risk disclosures."
            ),
            input_schema=_schema(
                {
                    **partner_id,
                    "productId": {"type": "string"},
                    "faceAmount": {"type": "number", "exclusiveMinimum": 0},
                    "termYears": {"type": "integer", "minimum": 1, "maximum": 80},
                    "leadId": {"type": "string"},
                    "applicant": _APPLICANT,
                },
                ["partnerId", "productId", "faceAmount", "applicant"],
            ),
            handler=quote,
            required_scopes=("distribution:read",),
            plugin_id="plugin5",
            operation_id="distribution_quote",
            tags=("distribution", "pricing"),
        ),
        McpToolSpec(
            name="distribution_get_quote",
            title="Get Quote",
            description="A quote with its suitability assessment, disclosures and expiry.",
            input_schema=_schema(quote_id, ["quoteId"]),
            handler=get_quote,
            required_scopes=("distribution:read",),
            plugin_id="plugin5",
            operation_id="distribution_get_quote",
            tags=("distribution", "pricing"),
        ),
        McpToolSpec(
            name="distribution_screen_kyc",
            title="Screen Identity and Sanctions",
            description=(
                "Screen a quote for identity, sanctions and PEP status. A confirmed match is "
                "never a clear, and a potential match always requires a human."
            ),
            input_schema=_schema(
                {
                    **quote_id,
                    "documentType": {"type": "string"},
                    "documentReference": {"type": "string"},
                },
                ["quoteId"],
            ),
            handler=screen,
            required_scopes=("distribution:kyc",),
            plugin_id="plugin5",
            operation_id="distribution_screen_kyc",
            tags=("distribution", "kyc", "compliance"),
        ),
        McpToolSpec(
            name="distribution_bind_policy",
            title="Bind Policy",
            description=(
                "Issue a policy from a quote. Destructive: it binds a real contract of "
                "insurance on a real person, so the caller must pass confirm=true. Refused "
                "without a bindable quote, a clear KYC result and a non-unsuitable "
                "assessment."
            ),
            input_schema=_schema({**quote_id, "confirm": {"type": "boolean", "default": False}}, ["quoteId", "confirm"]),
            handler=bind,
            required_scopes=("distribution:write",),
            annotations=McpToolAnnotations(read_only=False, destructive=True, idempotent=False),
            plugin_id="plugin5",
            operation_id="distribution_bind_policy",
            tags=("distribution", "lifecycle", "human-in-the-loop"),
        ),
        McpToolSpec(
            name="distribution_get_policy",
            title="Get Policy",
            description=(
                "A bound policy including free-look expiry and whether its commission is "
                "currently payable."
            ),
            input_schema=_schema({"policyId": {"type": "string"}}, ["policyId"]),
            handler=get_policy,
            required_scopes=("distribution:read",),
            plugin_id="plugin5",
            operation_id="distribution_get_policy",
            tags=("distribution", "lifecycle"),
        ),
        McpToolSpec(
            name="distribution_create_payment_intent",
            title="Create Payment Intent",
            description=(
                "Request the first premium against a bound policy. The intent records that "
                "money was asked for; it is not a payment."
            ),
            input_schema=_schema(
                {**quote_id, "method": {"type": "string", "default": "ach"}}, ["quoteId"]
            ),
            handler=create_intent,
            required_scopes=("distribution:write",),
            annotations=McpToolAnnotations(read_only=False, destructive=False, idempotent=False),
            plugin_id="plugin5",
            operation_id="distribution_create_payment_intent",
            tags=("distribution", "payments"),
        ),
        McpToolSpec(
            name="distribution_reconcile",
            title="Reconcile Quote to Cash",
            description=(
                "Compare what was quoted, what was bound and what was collected, so a partner "
                "and a carrier are not working from two different numbers."
            ),
            input_schema=_schema(partner_id, ["partnerId"]),
            handler=reconcile,
            required_scopes=("distribution:read",),
            plugin_id="plugin5",
            operation_id="distribution_reconcile",
            tags=("distribution", "payments", "finance"),
        ),
        McpToolSpec(
            name="distribution_commission_statement",
            title="Commission Statement",
            description=(
                "What a partner earned over a period, split by product, with commission on "
                "policies inside free look shown as accrued rather than payable."
            ),
            input_schema=_schema(
                {
                    **partner_id,
                    "periodStart": {"type": "string", "description": "ISO date."},
                    "periodEnd": {"type": "string", "description": "ISO date."},
                },
                ["partnerId", "periodStart", "periodEnd"],
            ),
            handler=statement,
            required_scopes=("distribution:read",),
            plugin_id="plugin5",
            operation_id="distribution_commission_statement",
            tags=("distribution", "commission", "finance"),
        ),
        McpToolSpec(
            name="distribution_list_events",
            title="List Partner Events",
            description="The webhook events a partner would be notified about, for replay or reconciliation.",
            input_schema=_schema({"partnerId": {"type": "string"}}, []),
            handler=events,
            required_scopes=("distribution:read",),
            plugin_id="plugin5",
            operation_id="distribution_list_events",
            tags=("distribution", "events"),
        ),
    ]
    for spec in specs:
        registry.register_tool(spec)

    registry.register_resource(
        McpResourceSpec(
            uri_template="pas://distribution/catalog",
            name="Distribution catalog",
            title="Example listed products",
            description="Three example products with different gating: instant term, paramed whole life, annuity.",
            mime_type="application/json",
            handler=catalog_resource,
            plugin_id="plugin5",
            tags=("distribution", "catalog"),
        )
    )
    return registry


__all__ = ["PLUGIN_INFO", "build_registry"]