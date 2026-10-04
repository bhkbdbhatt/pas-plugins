"""MCP tools and resources for the unified data foundation.

The specification's requirement is specific: "AI agents can query customer 360,
policy history, claims and billing in one call". That is `customer_360_get` here,
and it is deliberately one tool rather than four, because an agent that has to
join four calls will sometimes join three of them and present a confident answer
built on three quarters of the picture.

Every read tool requires a ``purpose``, which is a real access control rather than
a form field: purpose limitation is what HIPAA and SOC 2 expect, and an agent that
cannot state a purpose is not asking for data for a reason anyone can audit.
"""

from __future__ import annotations

from typing import Any

from pas_core.errors import ErrorCode, PasError
from pas_core.mcp.registry import McpToolRegistry, standard_catalogue_tool
from pas_core.mcp.spec import (
    McpPromptSpec,
    McpResourceSpec,
    McpServerInfo,
    McpToolAnnotations,
    McpToolSpec,
)
from pas_core.pii import PiiPolicy
from pas_core.tenancy import TenantContext
from pas_plugins.plugin6_datamesh.models import AccessPurpose, EntityType

MCP_SERVER_NAME = "pas-unified-data"
MCP_INSTRUCTIONS = """\
You are connected to a carrier's unified data foundation - a single, reconciled
view of customer, policy, claims and billing data assembled from the carrier's
Policy Administration System, claims system, billing platform and CRM.

How to work:
  1. Start with `customer_360_get`. It returns the customer, their policies, their
     claims and their premium history in one call. Do not assemble that picture
     from separate calls: you will get it wrong.
  2. Every read requires a `purpose`. Use one from the allowed list and one that
     reflects what you are actually doing. Do not invent a purpose.
  3. Sensitive fields (name, date of birth, address, contact, financial amounts)
     are MASKED unless the caller holds the `pii:unmask` scope. The response tells
     you which fields were masked in `maskedFields`. Never infer a masked value
     from context, and never repeat one to a user as if it were the real value.
  4. `data_quality_report` tells you how much to trust a dataset. If the score is
     below 0.90, say so rather than presenting the data as fact.
  5. `data_lineage_impact` answers "if this source is wrong, what else is wrong?"
     Use it before telling a user that a number is correct.

Amounts are decimal numbers in the stated currency. Dates are ISO-8601.
"""


def build_registry(service: Any) -> McpToolRegistry:  # noqa: ANN401
    """Build the data mesh MCP registry bound to a :class:`DataMeshService`."""
    registry = McpToolRegistry(
        McpServerInfo(
            name=MCP_SERVER_NAME,
            version="1.0.0",
            title="PAS Unified Data Foundation",
            description=(
                "Reconciled customer, policy, claims and billing data with quality "
                "scoring, lineage and purpose-limited access."
            ),
            instructions=MCP_INSTRUCTIONS,
        )
    )
    _register_queries(registry, service)
    _register_operations(registry, service)
    _register_resources(registry, service)
    _register_prompts(registry)
    standard_catalogue_tool(registry)
    return registry


_PURPOSE_SCHEMA = {
    "type": "string",
    "enum": [str(p) for p in AccessPurpose],
    "description": (
        "Why this data is being read. Required by policy: a read with no stated "
        "purpose is refused. Choose the purpose that matches what you are doing."
    ),
}


def _register_queries(registry: McpToolRegistry, service: Any) -> None:  # noqa: ANN401
    """Read-only tools. All of them require a purpose; none of them mutate."""

    async def customer_360(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        """Customer, policies, claims and premiums in a single call."""
        view = service.customer_360(
            ctx,
            str(arguments["customerId"]),
            purpose=_purpose(arguments),
            unmask=bool(arguments.get("unmask", False)),
        )
        return {
            **view.to_summary(),
            "customer": view.customer.model_dump(mode="json"),
            "policies": [p.model_dump(mode="json") for p in view.policies],
            "claims": [c.model_dump(mode="json") for c in view.claims],
            "premiums": [x.model_dump(mode="json") for x in view.premiums],
        }

    registry.register_tool(
        McpToolSpec(
            name="customer_360_get",
            title="Get the full customer picture",
            description=(
                "Return the customer together with every policy, claim and premium record "
                "linked to them, in one call. Includes data quality and the list of fields "
                "that were masked for this caller. Use this instead of chaining several "
                "lookups - the links between these records are not something to infer."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "customerId": {
                        "type": "string",
                        "description": "Surrogate key or natural key of the customer.",
                    },
                    "purpose": _PURPOSE_SCHEMA,
                    "unmask": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Return unmasked values. Requires the pii:unmask scope; "
                            "otherwise the request is served masked."
                        ),
                    },
                },
                "required": ["customerId", "purpose"],
                "additionalProperties": False,
            },
            handler=customer_360,
            annotations=McpToolAnnotations(read_only=True, idempotent=True),
            plugin_id=MCP_SERVER_NAME,
            tags=("query", "customer"),
            examples=({"customerId": "CUST000001", "purpose": "servicing"},),
            error_codes=(ErrorCode.NOT_FOUND.value, ErrorCode.PERMISSION_DENIED.value),
        )
    )

    async def entity_search(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        """Search golden records and return summaries."""
        entity_type = arguments.get("entityType")
        return service.search(
            ctx,
            entity_type=EntityType(entity_type) if entity_type else None,
            query=arguments.get("query"),
            limit=int(arguments.get("limit", 25)),
            offset=int(arguments.get("offset", 0)),
            purpose=_purpose(arguments),
            unmask=bool(arguments.get("unmask", False)),
        )

    registry.register_tool(
        McpToolSpec(
            name="data_search",
            title="Search the unified data layer",
            description=(
                "Search reconciled golden records by entity type and free text, returning "
                "summaries rather than full records. Use this to find a person or a policy "
                "when you do not have their key."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entityType": {
                        "type": "string",
                        "enum": [str(e) for e in EntityType],
                        "description": "Restrict to one entity type.",
                    },
                    "query": {"type": "string", "description": "Free-text search term."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25},
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    "purpose": _PURPOSE_SCHEMA,
                    "unmask": {"type": "boolean", "default": False},
                },
                "required": ["purpose"],
                "additionalProperties": False,
            },
            handler=entity_search,
            annotations=McpToolAnnotations(read_only=True, idempotent=True),
            plugin_id=MCP_SERVER_NAME,
            tags=("query", "search"),
            examples=({"entityType": "policy", "query": "POL", "purpose": "support"},),
            error_codes=(ErrorCode.VALIDATION_FAILED.value,),
        )
    )

    async def quality_report(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        """How much to trust the data before answering from it."""
        report = service.quality_report(ctx, dataset=str(arguments.get("dataset", "unified-gold")))
        payload = report.to_dict()
        payload["interpretation"] = (
            "Score above 0.90 is safe to answer from. Below 0.75, tell the user the data "
            "is unreliable and name the failing rules rather than presenting it as fact."
        )
        return payload

    registry.register_tool(
        McpToolSpec(
            name="data_quality_report",
            title="How much to trust this data",
            description=(
                "Return the quality score, grade and failing rules for a dataset, broken "
                "down by dimension (completeness, accuracy, consistency, timeliness, "
                "uniqueness, validity). Call this before answering quantitative questions "
                "so you can qualify your answer honestly."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "dataset": {"type": "string", "default": "unified-gold"}
                },
                "additionalProperties": False,
            },
            handler=quality_report,
            annotations=McpToolAnnotations(read_only=True, idempotent=True),
            plugin_id=MCP_SERVER_NAME,
            tags=("query", "quality"),
            examples=({},),
            error_codes=(),
        )
    )

    async def lineage_impact(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        """Which downstream datasets break if this one is wrong."""
        return service.lineage.impact(ctx, str(arguments["dataset"]))

    registry.register_tool(
        McpToolSpec(
            name="data_lineage_impact",
            title="What breaks if this source is wrong",
            description=(
                "Walk the lineage graph forward from a source dataset and return every "
                "downstream dataset that depends on it. Use it before telling a user that "
                "a figure is correct, and when investigating a discrepancy."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "dataset": {
                        "type": "string",
                        "description": "Dataset name, e.g. raw.pas-policies.",
                    }
                },
                "required": ["dataset"],
                "additionalProperties": False,
            },
            handler=lineage_impact,
            annotations=McpToolAnnotations(read_only=True, idempotent=True),
            plugin_id=MCP_SERVER_NAME,
            tags=("query", "lineage"),
            examples=({"dataset": "raw.pas-policies"},),
            error_codes=(),
        )
    )

    async def entity_read(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        """Read one golden record by key."""
        entity, masked = service.read_entity(
            ctx,
            str(arguments["surrogateKey"]),
            purpose=_purpose(arguments),
            unmask=bool(arguments.get("unmask", False)),
        )
        return {
            **entity.model_dump(mode="json"),
            "maskedFields": masked,
            "qualityScore": entity.quality_score,
            "sources": sorted({str(entry.get("sourceSystem")) for entry in entity.lineage}),
        }

    registry.register_tool(
        McpToolSpec(
            name="entity_get",
            title="Read one unified record",
            description=(
                "Read a single golden record by surrogate or natural key, with its lineage "
                "sources and the list of masked fields. Prefer `customer_360_get` when you "
                "need a complete picture of a person."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "surrogateKey": {"type": "string", "description": "Surrogate or natural key."},
                    "purpose": _PURPOSE_SCHEMA,
                    "unmask": {"type": "boolean", "default": False},
                },
                "required": ["surrogateKey", "purpose"],
                "additionalProperties": False,
            },
            handler=entity_read,
            annotations=McpToolAnnotations(read_only=True, idempotent=True),
            plugin_id=MCP_SERVER_NAME,
            tags=("query",),
            examples=({"surrogateKey": "CUST000001", "purpose": "reporting"},),
            error_codes=(ErrorCode.NOT_FOUND.value,),
        )
    )


def _register_operations(registry: McpToolRegistry, service: Any) -> None:  # noqa: ANN401
    """Tools that write: ingestion, entity resolution and feature materialisation."""

    async def ingest_source(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        """Pull a batch source into the unified layer."""
        result = await service.ingest(str(arguments["sourceId"]), ctx, limit=arguments.get("limit"))
        return result.to_dict()

    registry.register_tool(
        McpToolSpec(
            name="data_ingest_source",
            title="Ingest a batch source",
            description=(
                "Pull every record from a registered batch source into the unified layer. "
                "Records land in bronze, are conformed to the canonical model into silver, "
                "and are not visible to queries until entity resolution promotes them to "
                "gold. Use this to refresh a source on demand."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "sourceId": {"type": "string", "description": "Registered source id."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10000},
                },
                "required": ["sourceId"],
                "additionalProperties": False,
            },
            handler=ingest_source,
            required_scopes=("data:write",),
            annotations=McpToolAnnotations(read_only=False, destructive=False, idempotent=True),
            plugin_id=MCP_SERVER_NAME,
            tags=("ingestion", "write"),
            examples=({"sourceId": "pas-policies"},),
            error_codes=(ErrorCode.NOT_FOUND.value, ErrorCode.VALIDATION_FAILED.value),
            rate_limit_policy="data-export",
        )
    )

    async def resolve_entities(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        """Resolve silver records into deduplicated golden records."""
        return service.resolve(ctx, quality_floor=arguments.get("qualityFloor"))

    registry.register_tool(
        McpToolSpec(
            name="mdm_resolve",
            title="Resolve and deduplicate records",
            description=(
                "Run entity resolution and survivorship over the silver layer, producing one "
                "golden record per real-world customer. Returns the quality score and whether "
                "the result is above the promotion floor. Safe and idempotent: re-running it "
                "re-resolves from scratch rather than double-merging."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "qualityFloor": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "description": "Minimum score for a record to be published as gold.",
                    }
                },
                "additionalProperties": False,
            },
            handler=resolve_entities,
            required_scopes=("data:write",),
            annotations=McpToolAnnotations(read_only=False, destructive=False, idempotent=True),
            plugin_id=MCP_SERVER_NAME,
            tags=("mdm", "write"),
            examples=({},),
            error_codes=(ErrorCode.BUSINESS_RULE_VIOLATION.value,),
            rate_limit_policy="data-export",
        )
    )

    async def materialize_features(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        """Write feature values for an entity, ready for online serving."""
        entity = service.entity_key_for(
            ctx, EntityType(arguments.get("entityType", "submission")), str(arguments["entityKey"])
        )
        return service.materialize_features(ctx, entity, dict(arguments.get("values") or {}))

    registry.register_tool(
        McpToolSpec(
            name="feature_materialize",
            title="Materialise features for an entity",
            description=(
                "Write a set of feature values for one entity into the online feature store, "
                "recording the source, version and expiry of each. Unknown feature names are "
                "reported in `missingFeatures` rather than silently stored."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entityType": {"type": "string", "enum": [str(e) for e in EntityType], "default": "submission"},
                    "entityKey": {"type": "string"},
                    "values": {
                        "type": "object",
                        "description": "Feature name to value. Names must be registered features.",
                        "additionalProperties": True,
                    },
                },
                "required": ["entityKey", "values"],
                "additionalProperties": False,
            },
            handler=materialize_features,
            required_scopes=("data:write",),
            annotations=McpToolAnnotations(read_only=False, destructive=False, idempotent=False),
            plugin_id=MCP_SERVER_NAME,
            tags=("feature-store", "write"),
            examples=({"entityKey": "SUB001", "values": {"prior_claims_count": 2}},),
            error_codes=(ErrorCode.NOT_FOUND.value,),
        )
    )

    async def feature_vector(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        """Fetch a feature vector for online inference."""
        entity = service.entity_key_for(
            ctx, EntityType(arguments.get("entityType", "submission")), str(arguments["entityKey"])
        )
        return service.feature_vector(ctx, entity, arguments.get("features"))

    registry.register_tool(
        McpToolSpec(
            name="feature_get_vector",
            title="Fetch a feature vector",
            description=(
                "Retrieve the current feature vector for an entity, with versions, "
                "completeness and the names of missing or stale features. Expired values are "
                "excluded rather than served, so a model never scores against stale data."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entityType": {"type": "string", "enum": [str(e) for e in EntityType], "default": "submission"},
                    "entityKey": {"type": "string"},
                    "features": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific features to fetch. Omit for the model default set.",
                    },
                },
                "required": ["entityKey"],
                "additionalProperties": False,
            },
            handler=feature_vector,
            annotations=McpToolAnnotations(read_only=True, idempotent=True),
            plugin_id=MCP_SERVER_NAME,
            tags=("feature-store", "query"),
            examples=({"entityKey": "SUB001"},),
            error_codes=(),
        )
    )

    async def compute_drift(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        """Measure distribution shift for a feature against its baseline."""
        report = service.features.compute_drift(
            str(arguments["featureName"]),
            [float(v) for v in arguments.get("currentValues", [])],
            ctx,
        )
        return report.to_dict()

    registry.register_tool(
        McpToolSpec(
            name="feature_drift_check",
            title="Check a feature for drift",
            description=(
                "Compute the Population Stability Index between a feature's training "
                "baseline and the current population, and report whether it warrants "
                "retraining. PSI below 0.10 is stable, 0.10-0.25 moderate, above 0.25 "
                "significant. Call this when model accuracy looks like it has slipped."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "featureName": {"type": "string"},
                    "currentValues": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "Current population values.",
                    },
                },
                "required": ["featureName", "currentValues"],
                "additionalProperties": False,
            },
            handler=compute_drift,
            annotations=McpToolAnnotations(read_only=True, idempotent=True),
            plugin_id=MCP_SERVER_NAME,
            tags=("feature-store", "monitoring"),
            examples=({"featureName": "prior_claims_count", "currentValues": [0, 1, 2]},),
            error_codes=(),
            rate_limit_policy="data-export",
        )
    )


def _register_resources(registry: McpToolRegistry, service: Any) -> None:  # noqa: ANN401
    """Resources an agent can pull as context without a tool call."""

    async def catalogue(_params: dict[str, str], ctx: TenantContext) -> dict[str, Any]:
        return service.catalog(ctx)

    registry.register_resource(
        McpResourceSpec(
            uri_template="pas://data/catalog",
            name="data_catalog",
            title="Unified data catalogue",
            description=(
                "Every entity, source, feature and quality report for the carrier's unified "
                "data layer. The best starting point for an agent that needs to know what "
                "data exists."
            ),
            handler=catalogue,
            plugin_id=MCP_SERVER_NAME,
            tags=("discovery",),
        )
    )
    registry.register_resource(
        McpResourceSpec(
            uri_template="pas://data/sources",
            name="data_sources",
            title="Registered ingestion sources",
            description="What is ingested, from where, how often, and on which entity type.",
            handler=lambda p, ctx: {"sources": service.ingestion.stats(ctx)["sources"]},
            plugin_id=MCP_SERVER_NAME,
            tags=("discovery", "ingestion"),
        )
    )
    registry.register_resource(
        McpResourceSpec(
            uri_template="pas://data/lineage",
            name="data_lineage_graph",
            title="Lineage graph",
            description="Source-to-gold dataset lineage as a node/edge graph.",
            handler=lambda p, ctx: service.lineage.graph(ctx),
            plugin_id=MCP_SERVER_NAME,
            tags=("lineage",),
        )
    )
    registry.register_resource(
        McpResourceSpec(
            uri_template="pas://data/features",
            name="feature_definitions",
            title="Feature definitions",
            description="Every declared feature with its owner, TTL and materialisation mode.",
            handler=lambda p, ctx: {
                "features": [f.to_dict() for f in service.features.definitions(ctx=ctx)]
            },
            plugin_id=MCP_SERVER_NAME,
            tags=("feature-store",),
        )
    )
    registry.register_resource(
        McpResourceSpec(
            uri_template="pas://data/pii-policy",
            name="pii_policy",
            title="PII masking and purpose policy",
            description=(
                "The masking strategy per data class, the purposes allowed, and the scope "
                "required for unmasked values."
            ),
            handler=lambda p, ctx: {
                "policy": {
                    "mode": service.pii_policy.mode,
                    "unmaskScope": service.pii_policy.unmask_scope,
                    "allowedPurposes": sorted(service.pii_policy.allowed_purposes) or [
                        str(x) for x in AccessPurpose
                    ],
                    "deniedPurposes": sorted(service.pii_policy.denied_purposes),
                    "requirePurpose": service.pii_policy.require_purpose,
                }
            },
            plugin_id=MCP_SERVER_NAME,
            tags=("governance", "compliance"),
        )
    )


def _register_prompts(registry: McpToolRegistry) -> None:
    registry.register_prompt(
        McpPromptSpec(
            name="answer_customer_question",
            title="Answer a question about a customer",
            description=(
                "A playbook for answering a service agent's question about a customer with "
                "the unified data layer, including the quality caveat the agent must give."
            ),
            template=(
                "A service agent asks: {question}\n"
                "Customer reference: {customerId}\n"
                "Agent's stated purpose for the request: {purpose}\n\n"
                "Follow these steps:\n"
                "1. Call `data_quality_report` first. If the grade is C or below, tell the "
                "agent the data is unreliable before answering anything else.\n"
                "2. Call `customer_360_get` with the stated purpose. Do not assemble the "
                "picture from separate calls.\n"
                "3. If the question depends on a figure, check `maskedFields`. If the "
                "relevant field is masked, say that you can see the field but not its value "
                "- do not guess, and do not reason about how large a masked value might be.\n"
                "4. Answer the agent's question directly, then state the data quality grade "
                "and the date the records were last refreshed.\n"
                "5. If the answer depends on which source system holds a fact, use "
                "`data_lineage_impact` and name the source."
            ),
            arguments=(
                {"name": "question", "description": "The agent's question", "required": True},
                {"name": "customerId", "description": "Customer reference", "required": True},
                {"name": "purpose", "description": "Stated purpose", "required": True},
            ),
            plugin_id=MCP_SERVER_NAME,
        )
    )
    registry.register_prompt(
        McpPromptSpec(
            name="investigate_data_discrepancy",
            title="Investigate a data discrepancy",
            description=(
                "A disciplined path for working out why two systems disagree about the same "
                "customer or policy fact."
            ),
            template=(
                "Discrepancy: {discrepancy}\n"
                "Entity reference: {entityId}\n"
                "Systems believed to disagree: {systems}\n\n"
                "1. Call `entity_get` to see the surviving values and which sources "
                "contributed them.\n"
                "2. Call `data_quality_report` and note whether the consistency dimension is "
                "below 1.0 - that is the signal that sources disagree.\n"
                "3. Call `data_lineage_impact` on the suspect source to establish the blast "
                "radius before escalating.\n"
                "4. Report: which source won by survivorship rule, which lost, the rule "
                "that decided it, and the datasets that inherit the problem. Do not change "
                "the data yourself - survivorship changes require the data steward."
            ),
            arguments=(
                {"name": "discrepancy", "description": "What disagrees", "required": True},
                {"name": "entityId", "description": "Entity reference", "required": True},
                {"name": "systems", "description": "Systems believed to disagree", "required": False},
            ),
            plugin_id=MCP_SERVER_NAME,
        )
    )


def _purpose(arguments: dict[str, Any]) -> str | None:
    """Extract and validate the declared access purpose."""
    purpose = arguments.get("purpose")
    if not purpose:
        raise PasError(
            ErrorCode.VALIDATION_FAILED,
            "This read requires a `purpose` from the allowed list",
            {"allowedPurposes": [str(p) for p in AccessPurpose]},
        )
    return str(purpose)


def build_default_registry() -> McpToolRegistry:
    """Registry bound to the process-wide service singleton."""
    from pas_plugins.plugin6_datamesh.service import get_service  # noqa: PLC0415

    return build_registry(get_service())


def default_policy() -> PiiPolicy:
    """The platform default masking policy."""
    return PiiPolicy()
