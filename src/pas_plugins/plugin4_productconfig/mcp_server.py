"""MCP tools for low-code product configuration.

Twelve tools that let a product author work entirely through an agent: build a
definition, validate it, simulate its economics, publish it and price it.

The confirmation rule is stricter here than elsewhere in the suite, because
publication is the one irreversible act in this plugin. `products_publish` is
destructive and requires `confirm=true`; every other tool either writes only to a
draft - a scratchpad, cheap to lose - or reads. That is the distinction: drafting
needs no confirmation, and publication needs it precisely because it is the
moment a product becomes real.
"""

from __future__ import annotations

from typing import Any

from pas_core.mcp.registry import (
    McpResourceSpec,
    McpServerInfo,
    McpToolAnnotations,
    McpToolRegistry,
    McpToolSpec,
)
from pas_core.tenancy import TenantContext
from pas_plugins.plugin4_productconfig.compliance import ALLOWED_RULE_FIELDS, MIN_MARGIN_BPS_WARNING
from pas_plugins.plugin4_productconfig.examples import demo_definition
from pas_plugins.plugin4_productconfig.models import (
    EligibilityRule,
    ProductDefinition,
    QuoteRequest,
    SimulationProfile,
)
from pas_plugins.plugin4_productconfig.service import (
    ASSUMED_BENEFIT_COST_PER_THOUSAND,
    ProductConfigError,
    ProductConfigService,
    default_product_config_service,
)
from pas_plugins.plugin4_productconfig.settings import Plugin4Settings

PLUGIN_INFO = McpServerInfo(
    name="pas-product-config",
    version=Plugin4Settings().plugin_version,
    title="PAS Low-Code Product Configuration",
    description=(
        "Configure, validate, simulate and publish insurance products without code. "
        "Covers, benefits, charges, rating tables, eligibility rules and compliance "
        "constraints are data; the pricing engine that interprets them is fixed."
    ),
)

_DEFINITION = {"definition": {"type": "object", "description": "A complete product definition."}}


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def build_registry(service: ProductConfigService | None = None) -> McpToolRegistry:
    """Construct the registry of product configuration tools."""
    svc = service or default_product_config_service()
    registry = McpToolRegistry(PLUGIN_INFO)

    async def save_draft(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        definition = ProductDefinition.model_validate(arguments["definition"])
        return {"draft": svc.save_draft(definition).to_dict()}

    async def get_draft(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        draft = svc.get_draft(str(arguments["productId"]))
        if draft is None:
            return {"error": "draftNotFound", "productId": arguments["productId"]}
        return {"draft": draft.to_dict()}

    async def validate(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        if arguments.get("definition"):
            definition = ProductDefinition.model_validate(arguments["definition"])
        else:
            definition = svc.get_draft(str(arguments["productId"]))
        if definition is None:
            return {"error": "draftNotFound", "productId": arguments.get("productId")}
        return {"report": svc.validate(definition).to_dict()}

    async def simulate(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        if arguments.get("definition"):
            definition = ProductDefinition.model_validate(arguments["definition"])
        else:
            definition = svc.get_draft(str(arguments["productId"]))
        if definition is None:
            return {"error": "draftNotFound", "productId": arguments.get("productId")}
        profile = SimulationProfile.model_validate(arguments.get("profile") or {})
        result = svc.simulate(
            definition, profile, policies=int(arguments.get("policies", 500))
        )
        return {"simulation": result.to_dict()}

    async def publish(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        try:
            published = svc.publish(str(arguments["productId"]), str(arguments.get("actor") or ctx.principal.subject))
        except ProductConfigError as exc:
            return {"error": "cannotPublish", "detail": str(exc)}
        return {"published": published.to_dict()}

    async def list_versions(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        versions = svc.list_published(str(arguments["productId"]))
        return {
            "productId": arguments["productId"],
            "count": len(versions),
            "versions": [
                {
                    "version": v.version,
                    "status": str(v.status),
                    "contentHash": v.content_hash,
                    "publishedAt": v.published_at.isoformat(),
                    "publishedBy": v.published_by,
                }
                for v in versions
            ],
        }

    async def get_version(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        published = svc.get_published(str(arguments["productId"]), int(arguments["version"]))
        if published is None:
            return {"error": "versionNotFound"}
        return {"published": published.to_dict()}

    async def diff_versions(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        try:
            diff = svc.diff(
                str(arguments["productId"]), int(arguments["fromVersion"]), int(arguments["toVersion"])
            )
        except ProductConfigError as exc:
            return {"error": "versionNotFound", "detail": str(exc)}
        return {"diff": diff.to_dict()}

    async def retire(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        try:
            retired = svc.retire(str(arguments["productId"]), int(arguments["version"]))
        except ProductConfigError as exc:
            return {"error": "versionNotFound", "detail": str(exc)}
        return {"published": retired.to_dict()}

    async def quote(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        request = QuoteRequest.model_validate({**arguments, "tenantId": ctx.tenant.tenant_id})
        try:
            return {"quote": svc.quote(request).to_dict()}
        except (ProductConfigError, ValueError) as exc:
            return {"error": "cannotQuote", "detail": str(exc)}

    async def meta(_: dict[str, Any], __: TenantContext) -> dict[str, Any]:
        from pas_plugins.plugin4_productconfig.models import (  # noqa: PLC0415
            BenefitCategory,
            ChargeFrequency,
            ProductCategory,
        )

        return {
            "productCategories": [str(c) for c in ProductCategory],
            "benefitCategories": [str(c) for c in BenefitCategory],
            "chargeFrequencies": [str(c) for c in ChargeFrequency],
            "eligibilityOperators": [str(o) for o in EligibilityRule.Operator],
            "eligibilityEffects": [str(e) for e in EligibilityRule.Effect],
            "eligibilityFields": sorted(ALLOWED_RULE_FIELDS),
            "complianceKinds": [
                "maxFaceAmount",
                "minFaceAmount",
                "minIssueAge",
                "maxIssueAge",
                "stateEligibility",
                "rateToValue",
                "surrenderCharge",
                "minPremium",
            ],
            "thresholds": {
                "minMarginBpsWarning": MIN_MARGIN_BPS_WARNING,
                "assumedBenefitCostPerThousand": ASSUMED_BENEFIT_COST_PER_THOUSAND,
            },
        }

    async def example(_: dict[str, Any], __: TenantContext) -> dict[str, Any]:
        """A complete worked definition an author can copy and modify."""
        return {"definition": demo_definition().to_dict()}

    product_id = {"productId": {"type": "string", "description": "The product identifier."}}

    specs = [
        McpToolSpec(
            name="products_get_capabilities",
            title="Get Product Configuration Capabilities",
            description=(
                "List every construct a product author can configure: categories, benefit "
                "and charge vocabularies, the eligibility operators, the compliance rule "
                "kinds, and the guardrail thresholds."
            ),
            input_schema=_schema({}, []),
            handler=meta,
            required_scopes=("products:read",),
            plugin_id="plugin4",
            operation_id="products_get_capabilities",
            tags=("product-config", "discovery"),
        ),
        McpToolSpec(
            name="products_get_example_definition",
            title="Get Example Product Definition",
            description=(
                "Return a complete, valid 20-year level term product definition with four "
                "rating bands, a tobacco cross-reference, two riders and three compliance "
                "rules. Intended as a starting point for a new configuration."
            ),
            input_schema=_schema({}, []),
            handler=example,
            required_scopes=("products:read",),
            plugin_id="plugin4",
            operation_id="products_get_example_definition",
            tags=("product-config", "examples"),
        ),
        McpToolSpec(
            name="products_save_draft",
            title="Save Product Draft",
            description=(
                "Create or update a product draft. Only drafts are mutable; a published "
                "version can never be edited. Writes to a scratchpad, so no confirmation "
                "is required."
            ),
            input_schema=_schema(_DEFINITION, ["definition"]),
            handler=save_draft,
            required_scopes=("products:write",),
            annotations=McpToolAnnotations(read_only=False, destructive=False, idempotent=True),
            plugin_id="plugin4",
            operation_id="products_save_draft",
            tags=("product-config", "authoring"),
        ),
        McpToolSpec(
            name="products_get_draft",
            title="Get Product Draft",
            description="Fetch the current mutable definition of a product.",
            input_schema=_schema(product_id, ["productId"]),
            handler=get_draft,
            required_scopes=("products:read",),
            plugin_id="plugin4",
            operation_id="products_get_draft",
            tags=("product-config", "authoring"),
        ),
        McpToolSpec(
            name="products_validate",
            title="Validate Product Definition",
            description=(
                "Run every guardrail and compliance rule against a definition and return "
                "findings with severity and remediation. Blockers prevent publication."
            ),
            input_schema=_schema(
                {**product_id, **_DEFINITION}, ["productId"]
            ),
            handler=validate,
            required_scopes=("products:read",),
            plugin_id="plugin4",
            operation_id="products_validate",
            tags=("product-config", "governance"),
        ),
        McpToolSpec(
            name="products_simulate",
            title="Simulate Product Economics",
            description=(
                "Price a synthetic book of business against a definition and report "
                "premium, margin, loss ratio and unpriceable applicants. The benefit-cost "
                "assumption is stated in the result notes."
            ),
            input_schema=_schema(
                {
                    **product_id,
                    **_DEFINITION,
                    "profile": {"type": "object", "description": "Applicant distribution to simulate."},
                    "policies": {"type": "integer", "minimum": 1, "maximum": 20000, "default": 500},
                },
                ["productId"],
            ),
            handler=simulate,
            required_scopes=("products:read",),
            plugin_id="plugin4",
            operation_id="products_simulate",
            tags=("product-config", "economics"),
        ),
        McpToolSpec(
            name="products_publish",
            title="Publish Product Version",
            description=(
                "Publish the current draft as an immutable, content-hashed version. "
                "Destructive and irreversible: the caller must pass confirm=true. Refused "
                "while any guardrail blocker stands."
            ),
            input_schema=_schema(
                {**product_id, "actor": {"type": "string"}, "confirm": {"type": "boolean", "default": False}},
                ["productId", "confirm"],
            ),
            handler=publish,
            required_scopes=("products:publish",),
            annotations=McpToolAnnotations(read_only=False, destructive=True, idempotent=False),
            plugin_id="plugin4",
            operation_id="products_publish",
            tags=("product-config", "lifecycle"),
        ),
        McpToolSpec(
            name="products_list_versions",
            title="List Published Versions",
            description="Every published version of a product with content hashes.",
            input_schema=_schema(product_id, ["productId"]),
            handler=list_versions,
            required_scopes=("products:read",),
            plugin_id="plugin4",
            operation_id="products_list_versions",
            tags=("product-config", "lifecycle"),
        ),
        McpToolSpec(
            name="products_get_version",
            title="Get Published Version",
            description=(
                "A published version including its full definition and the guardrail state "
                "recorded at the moment of publication."
            ),
            input_schema=_schema(
                {**product_id, "version": {"type": "integer", "minimum": 1}}, ["productId", "version"]
            ),
            handler=get_version,
            required_scopes=("products:read",),
            plugin_id="plugin4",
            operation_id="products_get_version",
            tags=("product-config", "lifecycle"),
        ),
        McpToolSpec(
            name="products_diff_versions",
            title="Diff Product Versions",
            description=(
                "Structural difference between two published versions, with a materiality "
                "flag indicating whether filing obligations attach."
            ),
            input_schema=_schema(
                {
                    **product_id,
                    "fromVersion": {"type": "integer", "minimum": 1},
                    "toVersion": {"type": "integer", "minimum": 1},
                },
                ["productId", "fromVersion", "toVersion"],
            ),
            handler=diff_versions,
            required_scopes=("products:read",),
            plugin_id="plugin4",
            operation_id="products_diff_versions",
            tags=("product-config", "lifecycle"),
        ),
        McpToolSpec(
            name="products_retire_version",
            title="Retire Published Version",
            description=(
                "Withdraw a published version from sale. The version's content and hash "
                "are left intact for audit."
            ),
            input_schema=_schema(
                {**product_id, "version": {"type": "integer", "minimum": 1}}, ["productId", "version"]
            ),
            handler=retire,
            required_scopes=("products:publish",),
            annotations=McpToolAnnotations(read_only=False, destructive=True, idempotent=True),
            plugin_id="plugin4",
            operation_id="products_retire_version",
            tags=("product-config", "lifecycle"),
        ),
        McpToolSpec(
            name="products_quote",
            title="Quote Product Premium",
            description=(
                "Price a quote against the latest published version, fully itemised into "
                "base premium, each factor adjustment and each charge."
            ),
            input_schema=_schema(
                {
                    "productId": {"type": "string"},
                    "coverId": {"type": "string"},
                    "age": {"type": "integer", "minimum": 0, "maximum": 120},
                    "faceAmount": {"type": "number", "exclusiveMinimum": 0},
                    "termYears": {"type": "integer", "minimum": 1, "maximum": 80},
                    "riskFactors": {"type": "object", "additionalProperties": {"type": "number"}},
                    "state": {"type": "string", "minLength": 2, "maxLength": 2},
                    "tobacco": {"type": "boolean", "default": False},
                },
                ["productId", "coverId", "age", "faceAmount"],
            ),
            handler=quote,
            required_scopes=("products:read",),
            plugin_id="plugin4",
            operation_id="products_quote",
            tags=("product-config", "pricing"),
        ),
    ]
    for spec in specs:
        registry.register_tool(spec)

    async def schema_resource(_: TenantContext) -> dict[str, Any]:
        return {
            "eligibilityOperators": [str(o) for o in EligibilityRule.Operator],
            "eligibilityFields": sorted(ALLOWED_RULE_FIELDS),
            "complianceKinds": [
                "maxFaceAmount",
                "minFaceAmount",
                "minIssueAge",
                "maxIssueAge",
                "stateEligibility",
                "rateToValue",
                "surrenderCharge",
                "minPremium",
            ],
        }

    registry.register_resource(
        McpResourceSpec(
            uri_template="pas://products/schema",
            name="Product configuration schema",
            title="Supported operators and rule kinds",
            description="The closed set of operators and compliance kinds a definition may use.",
            mime_type="application/json",
            handler=schema_resource,
            plugin_id="plugin4",
            tags=("product-config", "schema"),
        )
    )

    async def example_resource(_: TenantContext) -> dict[str, Any]:
        return demo_definition().to_dict()

    registry.register_resource(
        McpResourceSpec(
            uri_template="pas://products/example/term20",
            name="Example term product",
            title="20-year level term definition",
            description="A complete worked product definition for use as a starting point.",
            mime_type="application/json",
            handler=example_resource,
            plugin_id="plugin4",
            tags=("product-config", "examples"),
        )
    )

    return registry


__all__ = ["PLUGIN_INFO", "build_registry"]