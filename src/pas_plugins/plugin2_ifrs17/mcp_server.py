"""MCP tools and resources for the IFRS 17 engine.

An agent is given the calculation, not just the answer. That matters because an
actuarial number is only useful if the recipient can see how it was produced: the
grouping rationale, the measurement model, the risk-adjustment method, the CSM
roll-forward and whether the disclosure reconciles.

Every tool that returns a figure also returns the inputs it was derived from and
whether the disclosure tables tie. A tool that returned only a reserve would be
useless - and dangerous - in a filing conversation.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pas_core.errors import ErrorCode, NotFoundError, PasError
from pas_core.mcp.registry import McpToolRegistry, standard_catalogue_tool
from pas_core.mcp.spec import (
    McpPromptSpec,
    McpResourceSpec,
    McpServerInfo,
    McpToolAnnotations,
    McpToolSpec,
)
from pas_core.tenancy import TenantContext
from pas_plugins.plugin2_ifrs17.models import (
    MeasurementModel,
    MeasurementResult,
    net_balance_sheet,
)

MCP_SERVER_NAME = "pas-ifrs17"
MCP_INSTRUCTIONS = """\
You are connected to a carrier's IFRS 17 and regulatory automation engine.

What this server does and does not do:
  * It MEASURES. It groups contracts under IFRS 17 paragraph 4, projects
    fulfilment cash flows, computes the risk adjustment and rolls the
    contractual service margin forward.
  * It DISCLOSES. It produces the liability roll-forwards, the CSM roll-forward,
    the risk adjustment roll-forward and the NAIC SSAP 102 reserve-class mapping.
  * It does NOT produce the carrier's statutory reserve, and it is not actuarial
    advice. SSAP 102 is reported as a mapping alongside the IFRS 17 figure, never
    as a substitute for it.

How to work:
  1. Start with `ifrs17_list_measurement_models` and `ifrs17_get_assumption_versions`.
     Never quote a number without naming the assumption version it came from.
  2. Call `ifrs17_measure_group` per group, or `ifrs17_run_valuation` for the whole
     portfolio. Prefer the whole-portfolio tool unless the user asked about one group.
  3. Check `allReconciled` on every disclosure. If it is false, say so and name the
     failing tables - a disclosure that does not reconcile must not be filed.
  4. A decline is never available here: every decline the engine could issue is
     already expressed as a loss component. Say "loss component", not "decline".
  5. When a group's CSM is zero and it has a loss component, that is IFRS 17
     paragraph 103 working correctly, not a failure. Explain it as such.

All amounts are in the stated currency. Dates are ISO-8601.
"""


def build_registry(service: Any) -> McpToolRegistry:  # noqa: ANN401
    """Build the IFRS 17 MCP registry bound to a :class:`ValuationService`."""
    registry = McpToolRegistry(
        McpServerInfo(
            name=MCP_SERVER_NAME,
            version="1.0.0",
            title="PAS IFRS 17 Regulatory Automation",
            description=(
                "IFRS 17 grouping, measurement, CSM roll-forward and disclosure for "
                "Life and Annuity business, mapped to NAIC SSAP 102."
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


def _register_queries(registry: McpToolRegistry, service: Any) -> None:  # noqa: ANN401
    async def list_models(_: dict[str, Any], __: TenantContext) -> dict[str, Any]:
        """Describe the three IFRS 17 measurement models and when each applies."""
        return {
            "models": [
                {
                    "model": str(m),
                    "displayName": m.display_name,
                    "requiresCsm": m.requires_csm,
                    "requiresRiskAdjustment": m.requires_risk_adjustment,
                    "appliesWhen": _model_guidance(m),
                }
                for m in MeasurementModel
            ],
            "decisionRules": [
                "Contracts that do not contain direct participating features and are not "
                "short-duration are measured under the GMM.",
                "Contracts with direct participating features are measured under the VFA, "
                "with fulfilment cash flows varying with the carrier's share of the "
                "underlying items (para B48).",
                "Short-duration contracts may use the PAA, which recognises no CSM and "
                "no risk adjustment (para 53).",
            ],
        }

    registry.register_tool(
        McpToolSpec(
            name="ifrs17_list_measurement_models",
            title="List the IFRS 17 measurement models",
            description=(
                "Describe the General Measurement Model, the Variable Fee Approach and "
                "the Premium Allocation Approach, and state when each applies. Call this "
                "before quoting a measurement so you do not mislabel the basis."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=list_models,
            annotations=McpToolAnnotations(read_only=True, idempotent=True),
            plugin_id=MCP_SERVER_NAME,
            tags=("discovery",),
            examples=({},),
            error_codes=(),
        )
    )

    async def list_assumptions(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        """List the tenant's versioned actuarial assumptions."""
        versions = service.assumptions.versions(ctx.tenant_id)
        return {
            "tenantId": ctx.tenant_id,
            "count": len(versions),
            "currentVersion": service.assumptions.current(ctx.tenant_id).version
            if versions else None,
            "versions": [v.to_dict() for v in versions],
        }

    registry.register_tool(
        McpToolSpec(
            name="ifrs17_get_assumption_versions",
            title="List actuarial assumption versions",
            description=(
                "List every published assumption version with its mortality, lapse, "
                "expense and discount-curve detail, and which one is current. Always name "
                "the version behind a figure you quote."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=list_assumptions,
            annotations=McpToolAnnotations(read_only=True, idempotent=True),
            plugin_id=MCP_SERVER_NAME,
            tags=("assumptions", "discovery"),
            examples=({},),
            error_codes=(ErrorCode.NOT_FOUND.value,),
        )
    )

    async def group_contracts(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        """Group contract cohorts under IFRS 17 paragraph 4."""
        cohorts = _cohorts_from_arguments(arguments)
        outcome = service.grouping.group(cohorts, ctx)
        return outcome.to_dict()

    registry.register_tool(
        McpToolSpec(
            name="ifrs17_group_contracts",
            title="Group contracts under IFRS 17 paragraph 4",
            description=(
                "Assign contract cohorts to groups of insurance contracts: by line of "
                "business, then by profitability assessed at initial recognition, then by "
                "issue-date cohort. Returns the rationale for each group, because the "
                "grouping is the judgement an auditor examines first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cohorts": {
                        "type": "array",
                        "description": "Contract cohorts to group.",
                        "items": {"type": "object", "additionalProperties": True},
                        "minItems": 1,
                    },
                    "cohortYearsPerGroup": {"type": "integer", "minimum": 1, "maximum": 20},
                    "minimumProfitabilityMargin": {"type": "number"},
                },
                "required": ["cohorts"],
                "additionalProperties": False,
            },
            handler=group_contracts,
            annotations=McpToolAnnotations(read_only=True, idempotent=True),
            plugin_id=MCP_SERVER_NAME,
            tags=("grouping",),
            examples=({"cohorts": _example_cohort()},),
            error_codes=(ErrorCode.VALIDATION_FAILED.value,),
            rate_limit_policy="ifrs17-valuation",
        )
    )

    async def measure_group(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        """Measure one group of insurance contracts."""
        contract = _cohort_from_arguments(arguments)
        context = _context_for(service, ctx, arguments.get("assumptionVersion"))
        result = service.engine.measure(contract, context)
        return {**result.to_dict(), "balanceSheet": net_balance_sheet(result)}

    registry.register_tool(
        McpToolSpec(
            name="ifrs17_measure_group",
            title="Measure one group of insurance contracts",
            description=(
                "Measure a single group at a reporting date under the selected measurement "
                "model, returning the fulfilment cash flows, the risk adjustment, the CSM or "
                "loss component, the profit-or-loss lines and the full CSM roll-forward. "
                "Prefer `ifrs17_run_valuation` for a whole portfolio."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_COHORT_PROPERTIES,
                    "assumptionVersion": {"type": "string"},
                },
                "required": [
                    "productLine", "cohortYear", "contractCount", "issuedPremium",
                    "expectedPremiums", "expectedClaims", "expectedExpenses",
                    "reportingDate",
                ],
                "additionalProperties": False,
            },
            handler=measure_group,
            annotations=McpToolAnnotations(read_only=True, idempotent=True),
            plugin_id=MCP_SERVER_NAME,
            tags=("measurement",),
            examples=({**_example_cohort(), "assumptionVersion": "v1"},),
            error_codes=(ErrorCode.VALIDATION_FAILED.value, ErrorCode.COMPUTATION_FAILED.value),
            rate_limit_policy="ifrs17-valuation",
        )
    )

    async def roll_forward_csm(arguments: dict[str, Any], __: TenantContext) -> dict[str, Any]:
        """Walk a CSM balance forward one period."""
        return service.roll_forward_group(
            opening_csm=float(arguments["openingCsm"]),
            locked_in_rate=float(arguments.get("lockedInRate", 0.047)),
            csm_release=float(arguments.get("csmRelease", 0.0)),
            new_business_csm=float(arguments.get("newBusinessCsm", 0.0)),
            experience_adjustments=float(arguments.get("experienceAdjustments", 0.0)),
            assumption_changes=float(arguments.get("assumptionChanges", 0.0)),
            previous_loss_component=float(arguments.get("previousLossComponent", 0.0)),
        )

    registry.register_tool(
        McpToolSpec(
            name="ifrs17_roll_forward_csm",
            title="Roll a CSM balance forward",
            description=(
                "Walk a contractual service margin forward one reporting period and return "
                "every movement: interest accretion at the locked-in rate, releases as "
                "services were provided, experience and assumption changes, and the "
                "loss-component interaction. Use it to explain a movement in a filing "
                "without re-running a valuation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "openingCsm": {"type": "number", "description": "Opening CSM balance."},
                    "lockedInRate": {"type": "number", "default": 0.047},
                    "csmRelease": {"type": "number", "default": 0.0},
                    "newBusinessCsm": {"type": "number", "default": 0.0},
                    "experienceAdjustments": {"type": "number", "default": 0.0},
                    "assumptionChanges": {"type": "number", "default": 0.0},
                    "previousLossComponent": {"type": "number", "default": 0.0},
                },
                "required": ["openingCsm"],
                "additionalProperties": False,
            },
            handler=roll_forward_csm,
            annotations=McpToolAnnotations(read_only=True, idempotent=True),
            plugin_id=MCP_SERVER_NAME,
            tags=("csm",),
            examples=({"openingCsm": 8_000_000, "lockedInRate": 0.047, "csmRelease": 900_000},),
            error_codes=(ErrorCode.COMPUTATION_FAILED.value,),
        )
    )


def _register_operations(registry: McpToolRegistry, service: Any) -> None:  # noqa: ANN401
    async def run_valuation(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        """Run a full valuation: extract from the PAS, group, measure, disclose."""
        reporting_date = _parse_date(arguments.get("reportingDate")) or date.today()
        run = await service.run(
            ctx,
            reporting_date=reporting_date,
            period=arguments.get("period"),
            assumption_version=arguments.get("assumptionVersion"),
            max_workers=int(arguments.get("maxWorkers", 4)),
        )
        return run.summary()

    registry.register_tool(
        McpToolSpec(
            name="ifrs17_run_valuation",
            title="Run a full IFRS 17 valuation",
            description=(
                "Extract the portfolio from the carrier's PAS, group it under paragraph 4, "
                "measure every group and generate the disclosure tables. Returns the totals, "
                "the grouping counts, whether the disclosure reconciles, and every warning. "
                "This is the tool to use for a portfolio-level question."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "reportingDate": {
                        "type": "string",
                        "format": "date",
                        "description": "Valuation date. Defaults to today.",
                    },
                    "period": {
                        "type": "string",
                        "description": "Reporting period, e.g. '2026'. Defaults to the reporting year.",
                    },
                    "assumptionVersion": {"type": "string"},
                    "maxWorkers": {"type": "integer", "minimum": 1, "maximum": 32, "default": 4},
                },
                "additionalProperties": False,
            },
            handler=run_valuation,
            required_scopes=("ifrs17:write",),
            annotations=McpToolAnnotations(read_only=False, destructive=False, idempotent=True),
            plugin_id=MCP_SERVER_NAME,
            tags=("valuation", "write"),
            examples=({"reportingDate": "2026-12-31", "period": "2026"},),
            error_codes=(ErrorCode.VALIDATION_FAILED.value, ErrorCode.PAS_UNAVAILABLE.value),
            rate_limit_policy="ifrs17-valuation",
        )
    )

    async def get_disclosure(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        """Return the disclosure tables for a valuation run."""
        run = service.run_by_id(str(arguments["runId"]), ctx)
        if run.disclosure is None:
            raise PasError(
                ErrorCode.UNPROCESSABLE,
                "This run produced no measurements, so there is nothing to disclose",
                {"runId": run.run_id},
            )
        return {
            "runId": run.run_id,
            "period": run.period,
            "reportingDate": run.reporting_date.isoformat(),
            "allReconciled": run.disclosure["allReconciled"],
            "reconciliation": _reconciliation(run.disclosure),
            "tables": run.disclosure["tables"],
            "interpretation": (
                "Present these tables with their notes and references. If anyReconciled is "
                "false, state that the figures are not filing-ready and name the tables "
                "that failed - do not present an unreconciled disclosure as final."
            ),
        }

    registry.register_tool(
        McpToolSpec(
            name="ifrs17_get_disclosure",
            title="Read the IFRS 17 disclosure tables",
            description=(
                "Return the IFRS 17 liability-for-remaining-coverage roll-forward, the CSM "
                "roll-forward, the risk adjustment roll-forward, the disaggregated insurance "
                "result, the measurement-model split and the NAIC SSAP 102 reserve-class "
                "mapping, each with its standard reference, its notes and its reconciliation "
                "status."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "runId": {"type": "string", "description": "Valuation run id."},
                },
                "required": ["runId"],
                "additionalProperties": False,
            },
            handler=get_disclosure,
            annotations=McpToolAnnotations(read_only=True, idempotent=True),
            plugin_id=MCP_SERVER_NAME,
            tags=("disclosure",),
            examples=({"runId": "a1b2c3"},),
            error_codes=(ErrorCode.NOT_FOUND.value, ErrorCode.UNPROCESSABLE.value),
        )
    )

    async def sensitivity(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        """Re-measure one group under shocked assumptions.

        The tornado chart in the actuarial UI is built from this.
        """
        contract = _cohort_from_arguments(arguments)
        base_context = _context_for(service, ctx, arguments.get("assumptionVersion"))
        base = service.engine.measure(contract, base_context)

        results: dict[str, Any] = {"groupId": base.group_id, "base": base.contractual_service_margin}
        for label, shift in (("discountRate", 0.01), ("mortality", 0.10), ("expenses", 0.20)):
            shocked = contract.model_copy(update={
                "locked_in_rate": max(0.0, contract.locked_in_rate + shift)
                if label == "discountRate"
                else contract.locked_in_rate,
            })
            context = base_context
            if label == "mortality":
                assumptions = base_context.assumptions
                context = ValuationContextFactory.with_mortality_shift(
                    base_context, assumptions, shift
                )
            elif label == "expenses":
                context = ValuationContextFactory.with_expense_shift(base_context, shift)
            outcome = service.engine.measure(shocked, context)
            results[label] = {
                "contractualServiceMargin": outcome.contractual_service_margin,
                "delta": round(outcome.contractual_service_margin - base.contractual_service_margin, 2),
                "riskAdjustment": outcome.risk_adjustment,
            }
        ranked = sorted(
            (k for k in ("discountRate", "mortality", "expenses")),
            key=lambda k: abs(results[k]["delta"]),
            reverse=True,
        )
        results["mostSensitiveTo"] = ranked[0] if ranked else None
        return results

    registry.register_tool(
        McpToolSpec(
            name="ifrs17_sensitivity",
            title="Sensitivity of a group to its assumptions",
            description=(
                "Re-measure a group under shocked discount rate, mortality and expense "
                "assumptions and report the CSM impact of each, ranked. Use it to explain "
                "which assumption a reserve is most exposed to."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_COHORT_PROPERTIES,
                    "assumptionVersion": {"type": "string"},
                },
                "required": [
                    "productLine", "cohortYear", "contractCount", "issuedPremium",
                    "expectedPremiums", "expectedClaims", "expectedExpenses",
                    "reportingDate",
                ],
                "additionalProperties": False,
            },
            handler=sensitivity,
            annotations=McpToolAnnotations(read_only=True, idempotent=True),
            plugin_id=MCP_SERVER_NAME,
            tags=("measurement", "sensitivity"),
            examples=({**_example_cohort()},),
            error_codes=(ErrorCode.COMPUTATION_FAILED.value,),
            rate_limit_policy="ifrs17-valuation",
        )
    )


class ValuationContextFactory:
    """Builds a perturbed :class:`ValuationContext` for sensitivity analysis."""

    @staticmethod
    def with_mortality_shift(context: Any, assumptions: Any, shift: float) -> Any:  # noqa: ANN401
        from pas_plugins.plugin2_ifrs17.csm import ValuationContext  # noqa: PLC0415
        from pas_plugins.plugin2_ifrs17.models import AssumptionVersion  # noqa: PLC0415

        shocked = AssumptionVersion.model_validate(assumptions.to_dict())
        shocked.mortality.qx_by_age = {
            age: min(1.0, qx * (1 + shift)) for age, qx in assumptions.mortality.qx_by_age.items()
        }
        shocked.label = f"{assumptions.label} (mortality +{shift:.0%})"
        return ValuationContext(
            assumptions=shocked,
            locked_in_curve=context.locked_in_curve,
            updated_curve=context.updated_curve,
            currency=context.currency,
            projection_years=context.projection_years,
            tolerance=context.tolerance,
        )

    @staticmethod
    def with_expense_shift(context: Any, shift: float) -> Any:  # noqa: ANN401
        from pas_plugins.plugin2_ifrs17.csm import ValuationContext  # noqa: PLC0415
        from pas_plugins.plugin2_ifrs17.models import AssumptionVersion  # noqa: PLC0415

        shocked = AssumptionVersion.model_validate(context.assumptions.to_dict())
        shocked.expenses.maintenance_expense_pct_of_premium = (
            assumptions_shift(shocked.expenses.maintenance_expense_pct_of_premium, shift)
        )
        shocked.label = f"{context.assumptions.label} (expenses +{shift:.0%})"
        return ValuationContext(
            assumptions=shocked,
            locked_in_curve=context.locked_in_curve,
            updated_curve=context.updated_curve,
            currency=context.currency,
            projection_years=context.projection_years,
            tolerance=context.tolerance,
        )


def assumptions_shift(base: float, shift: float) -> float:
    return min(1.0, max(0.0, base * (1 + shift)))


def _register_resources(registry: McpToolRegistry, service: Any) -> None:  # noqa: ANN401
    async def catalogue(_: dict[str, str], ctx: TenantContext) -> dict[str, Any]:
        return {
            "measurementModels": [str(m) for m in MeasurementModel],
            "assumptionVersions": [
                v.to_dict() for v in service.assumptions.versions(ctx.tenant_id)
            ],
            "recentRuns": [r.summary() for r in service.runs(ctx.tenant_id)[-5:]],
            "groupingCriteria": service.grouping.criteria.describe(),
        }

    registry.register_resource(
        McpResourceSpec(
            uri_template="pas://ifrs17/catalog",
            name="ifrs17_catalog",
            title="IFRS 17 engine catalogue",
            description=(
                "Measurement models, published assumption versions, the grouping criteria in "
                "force and the recent valuation runs for this carrier."
            ),
            handler=catalogue,
            plugin_id=MCP_SERVER_NAME,
            tags=("discovery",),
        )
    )
    registry.register_resource(
        McpResourceSpec(
            uri_template="pas://ifrs17/assumptions/{version}",
            name="ifrs17_assumptions",
            title="An actuarial assumption version",
            description=(
                "One published assumption version with its mortality, lapse, expense and "
                "discount-curve detail. Audit trail: cite this when quoting a figure."
            ),
            handler=lambda params, ctx: service.assumptions.get(
                ctx.tenant_id, params.get("version")
            ).to_dict(),
            plugin_id=MCP_SERVER_NAME,
            tags=("assumptions",),
        )
    )
    registry.register_resource(
        McpResourceSpec(
            uri_template="pas://ifrs17/disclosure/{runId}",
            name="ifrs17_disclosure",
            title="A valuation run's disclosure tables",
            description="The full IFRS 17 disclosure set for one valuation run.",
            handler=lambda params, ctx: (
                service.run_by_id(params["runId"], ctx).disclosure
                or {"error": "run has no measurements"}
            ),
            plugin_id=MCP_SERVER_NAME,
            tags=("disclosure",),
        )
    )


def _register_prompts(registry: McpToolRegistry) -> None:
    registry.register_prompt(
        McpPromptSpec(
            name="explain_a_reserve_movement",
            title="Explain a reserve movement between two reporting dates",
            description=(
                "A disciplined playbook for explaining to a finance committee why a "
                "contractual service margin moved between reporting dates."
            ),
            template=(
                "The CSM for group {groupId} moved between {fromDate} and {toDate}.\n"
                "Opening CSM was {openingCsm}.\n\n"
                "1. Call `ifrs17_measure_group` at the reporting date to get the current "
                "position and its assumption version.\n"
                "2. Call `ifrs17_roll_forward_csm` with the opening CSM and the movements to "
                "rebuild the roll-forward and confirm it reconciles.\n"
                "3. Call `ifrs17_sensitivity` to identify which assumption is driving the "
                "position.\n"
                "4. Explain the movement in this order: new business, interest accretion at "
                "the locked-in rate, releases as services were provided, then experience and "
                "assumption changes. Name any loss component movement separately.\n"
                "5. State the assumption version behind every figure and flag any disclosure "
                "table that does not reconcile."
            ),
            arguments=(
                {"name": "groupId", "description": "Contract group", "required": True},
                {"name": "fromDate", "description": "Opening reporting date", "required": True},
                {"name": "toDate", "description": "Closing reporting date", "required": True},
                {"name": "openingCsm", "description": "Opening CSM", "required": True},
            ),
            plugin_id=MCP_SERVER_NAME,
        )
    )
    registry.register_prompt(
        McpPromptSpec(
            name="prepare_a_filing_review",
            title="Prepare an IFRS 17 valuation for review",
            description=(
                "The sequence an actuarial reviewer should work through before a filing is "
                "released."
            ),
            template=(
                "Prepare the {period} valuation for actuarial review.\n\n"
                "1. Call `ifrs17_run_valuation` for {period}. Do not substitute any figure "
                "from an earlier run.\n"
                "2. Call `ifrs17_get_disclosure` for the run. If `allReconciled` is false, "
                "stop and report which tables failed and their differences.\n"
                "3. Call `ifrs17_get_assumption_versions` and confirm which version was used "
                "and that it is approved rather than draft.\n"
                "4. Identify every group with a loss component and confirm each has an "
                "explainable cause. A loss component without a stated cause is a review item, "
                "not a footnote.\n"
                "5. Confirm the SSAP 102 mapping is reported as a mapping, not as the "
                "statutory reserve.\n"
                "6. Produce a summary that states: total CSM, total risk adjustment, total loss "
                "component, insurance revenue, the assumption version, and whether the "
                "disclosure reconciles."
            ),
            arguments=({"name": "period", "description": "Reporting period", "required": True},),
            plugin_id=MCP_SERVER_NAME,
        )
    )


# ---------------------------------------------------------------------------
# Argument helpers
# ---------------------------------------------------------------------------
_COHORT_PROPERTIES: dict[str, Any] = {
    "groupId": {"type": "string", "description": "Group identifier."},
    "productLine": {"type": "string", "description": "Line of business, e.g. 'term'."},
    "cohortYear": {"type": "integer", "description": "Issue-date cohort year."},
    "contractCount": {"type": "integer", "minimum": 0},
    "issuedPremium": {"type": "number", "minimum": 0},
    "expectedPremiums": {"type": "number", "minimum": 0},
    "expectedClaims": {"type": "number", "minimum": 0},
    "expectedExpenses": {"type": "number", "minimum": 0},
    "reportingDate": {"type": "string", "format": "date"},
    "measurementModel": {
        "type": "string",
        "enum": [str(m) for m in MeasurementModel],
        "default": "gmm",
    },
    "averageAgeAtIssue": {"type": "integer", "minimum": 0, "maximum": 120, "default": 38},
    "averageTermYears": {"type": "integer", "minimum": 1, "maximum": 80, "default": 20},
    "averageAnnualisedPremium": {"type": "number", "minimum": 0, "default": 0},
    "lockedInRate": {"type": "number", "default": 0.047},
    "currency": {"type": "string", "default": "USD"},
}


def _example_cohort() -> dict[str, Any]:
    return {
        "groupId": "GIC-TERM-2024",
        "productLine": "term",
        "cohortYear": 2024,
        "contractCount": 1250,
        "issuedPremium": 3_600_000,
        "expectedPremiums": 4_450_000,
        "expectedClaims": 2_150_000,
        "expectedExpenses": 510_000,
        "reportingDate": "2026-12-31",
        "measurementModel": "gmm",
        "averageAgeAtIssue": 38,
        "averageTermYears": 20,
        "averageAnnualisedPremium": 2880,
        "lockedInRate": 0.047,
    }


def _cohort_from_arguments(arguments: dict[str, Any]) -> Any:  # noqa: ANN401
    from pas_plugins.plugin2_ifrs17.models import ContractGroupInput  # noqa: PLC0415

    payload = {
        k: v for k, v in arguments.items()
        if k in ContractGroupInput.model_fields and v is not None
    }
    reporting_date = _parse_date(payload.get("reporting_date") or payload.get("reportingDate"))
    if reporting_date is None:
        raise PasError(ErrorCode.VALIDATION_FAILED, "reportingDate is required")
    payload["reporting_date"] = reporting_date
    payload.setdefault("period", str(reporting_date.year))
    payload.setdefault("group_id", payload.get("groupId", "GIC-UNSPECIFIED"))
    payload.setdefault("product_line", payload.get("productLine", "life"))
    payload.setdefault("measurement_model", payload.get("measurementModel", "gmm"))
    return ContractGroupInput.model_validate(payload)


def _cohorts_from_arguments(arguments: dict[str, Any]) -> list[Any]:  # noqa: ANN401
    raw_cohorts = arguments.get("cohorts") or []
    cohorts = [
        _cohort_from_arguments({**raw, **{k: v for k, v in arguments.items() if k != "cohorts"}})
        for raw in raw_cohorts
    ]
    if not cohorts:
        raise PasError(ErrorCode.VALIDATION_FAILED, "at least one cohort is required")
    return cohorts


def _context_for(service: Any, ctx: TenantContext, version: str | None) -> Any:  # noqa: ANN401
    from pas_plugins.plugin2_ifrs17.csm import ValuationContext  # noqa: PLC0415

    assumptions = service.assumptions.get(ctx.tenant_id, version)
    return ValuationContext(
        assumptions=assumptions,
        locked_in_curve=assumptions.locked_in_curve,
        updated_curve=assumptions.updated_curve,
        currency=service.currency,
        projection_years=service.project_years,
    )


def _parse_date(value: Any) -> date | None:  # noqa: ANN401
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise PasError(
            ErrorCode.VALIDATION_FAILED,
            f"'{value}' is not an ISO-8601 date",
            {"value": str(value)},
        ) from None


def _reconciliation(disclosure: dict[str, Any]) -> dict[str, Any]:
    return {
        table["tableId"]: {
            "balanced": table["reconciliation"].get("balanced", True),
            **{k: v for k, v in table["reconciliation"].items() if k != "balanced"},
        }
        for table in disclosure["tables"]
    }


def _model_guidance(model: MeasurementModel) -> str:
    return {
        MeasurementModel.GMM: (
            "Contracts with no direct participating features that are not short-duration. "
            "This is the default and covers most life business."
        ),
        MeasurementModel.VFA: (
            "Contracts with direct participating features, where fulfilment cash flows vary "
            "with the entity's share of the underlying items (para B48)."
        ),
        MeasurementModel.PAA: (
            "Short-duration contracts only (para 53). Recognises no CSM and no risk "
            "adjustment; the liability for remaining coverage is the unearned premium plus "
            "insurance finance income on it."
        ),
    }[model]


def run_or_404(service: Any, run_id: str, ctx: TenantContext) -> Any:  # noqa: ANN401
    try:
        return service.run_by_id(run_id, ctx)
    except NotFoundError:
        raise


def build_default_registry() -> McpToolRegistry:
    """Registry bound to the process-wide valuation service."""
    from pas_plugins.plugin2_ifrs17.connector import default_valuation_service  # noqa: PLC0415

    return build_registry(default_valuation_service())


def describe_results(results: list[MeasurementResult]) -> dict[str, Any]:
    """Compact summary of a set of measurements, used by tests and the UI."""
    return {
        "groups": len(results),
        "csm": round(sum(r.contractual_service_margin for r in results), 2),
        "riskAdjustment": round(sum(r.risk_adjustment for r in results), 2),
        "lossComponent": round(sum(r.loss_component for r in results), 2),
    }
