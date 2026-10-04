"""Plugin 2 HTTP API - IFRS 17 valuation, assumptions and disclosure."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from pas_core.app import Platform
from pas_core.errors import ErrorCode, PasError, ValidationError
from pas_core.observability import GLOBAL_METRICS
from pas_core.tenancy import TenantContext
from pas_plugins.plugin2_ifrs17.connector import ValuationService
from pas_plugins.plugin2_ifrs17.disclosure import DisclosureGenerator, EXPORTERS
from pas_plugins.plugin2_ifrs17.models import ContractGroupInput, MeasurementModel

router = APIRouter(prefix="/ifrs17", tags=["ifrs17"])

MEDIA_TYPES = {
    "json": "application/json",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
    "xbrl": "application/xbrl+xml",
}


def build_router(platform: Platform, service: ValuationService) -> APIRouter:
    """Attach the platform-bound routes."""
    from pas_core.app import context_dependency  # noqa: PLC0415

    dependency = context_dependency(platform)

    # --- discovery --------------------------------------------------------
    @router.get("/models", summary="Measurement models", operation_id="ifrs17.models")
    async def models(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """GMM, VFA and PAA with the criteria for choosing between them."""
        ctx.principal.require_scopes("ifrs17:read")
        return {
            "models": [
                {
                    "model": str(m),
                    "displayName": m.display_name,
                    "requiresCsm": m.requires_csm,
                    "requiresRiskAdjustment": m.requires_risk_adjustment,
                }
                for m in MeasurementModel
            ],
            "basis": "IFRS 17 paras 29, 53 and B3-B48",
            "warning": (
                "This engine produces IFRS 17 measurements, not the carrier's statutory "
                "reserve. NAIC SSAP 102 is reported as a mapping only."
            ),
        }

    @router.get("/criteria", summary="Grouping criteria", operation_id="ifrs17.criteria")
    async def criteria(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """The IFRS 17 paragraph 4 grouping criteria currently in force."""
        ctx.principal.require_scopes("ifrs17:read")
        return service.grouping.criteria.describe()

    # --- assumptions ------------------------------------------------------
    @router.get("/assumptions", summary="Assumption versions", operation_id="ifrs17.listAssumptions")
    async def list_assumptions(
        request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Every published assumption version with its mortality, lapse, expense and curve detail."""
        ctx.principal.require_scopes("ifrs17:read")
        versions = service.assumptions.versions(ctx.tenant_id)
        return {
            "tenantId": ctx.tenant_id,
            "count": len(versions),
            "currentVersion": versions[-1].version if versions else None,
            "versions": [v.to_dict() for v in versions],
        }

    @router.get(
        "/assumptions/{version}", summary="Assumption version", operation_id="ifrs17.getAssumptions"
    )
    async def get_assumptions(
        version: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """One assumption version in full - the audit trail behind any figure."""
        ctx.principal.require_scopes("ifrs17:read")
        return service.assumptions.get(ctx.tenant_id, version).to_dict()

    @router.post("/assumptions", summary="Publish an assumption version", operation_id="ifrs17.publishAssumptions")
    async def publish_assumptions(
        request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Publish a new assumption version. Published versions are immutable."""
        ctx.principal.require_scopes("ifrs17:write")
        body = await _json_body(request)
        from pas_plugins.plugin2_ifrs17.models import AssumptionVersion  # noqa: PLC0415

        try:
            payload = AssumptionVersion.model_validate(body)
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(
                f"assumption set is invalid: {exc}", violations=_violations(exc)
            ) from exc
        payload = payload.model_copy(update={"created_by": ctx.principal.subject})
        service.assumptions.publish(ctx.tenant_id, payload)
        return payload.to_dict()

    @router.post(
        "/assumptions/{version}/approve",
        summary="Approve an assumption version",
        operation_id="ifrs17.approveAssumptions",
    )
    async def approve_assumptions(
        version: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Approve a draft version. Approved versions are locked for the filing."""
        ctx.principal.require_scopes("ifrs17:write")
        approved = service.assumptions.approve(ctx.tenant_id, version, by=ctx.principal.subject)
        return approved.to_dict()

    @router.get("/assumptions/diff", summary="Assumption diff", operation_id="ifrs17.diffAssumptions")
    async def diff_assumptions(
        request: Request,
        from_version: str,
        to_version: str,
        ctx: TenantContext = Depends(dependency),
    ) -> dict[str, Any]:
        """Field-level diff between two assumption versions, flagged if material."""
        ctx.principal.require_scopes("ifrs17:read")
        return service.assumptions.diff(ctx.tenant_id, from_version, to_version)

    # --- measurement ------------------------------------------------------
    @router.post("/groups", summary="Group contracts", operation_id="ifrs17.group")
    async def group_contracts(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Apply IFRS 17 paragraph 4 grouping to a cohort population."""
        ctx.principal.require_scopes("ifrs17:read")
        cohorts = [ContractGroupInput.model_validate(c) for c in await _json_body(request)]
        if not cohorts:
            raise ValidationError("at least one cohort is required")
        return service.grouping.group(cohorts, ctx).to_dict()

    @router.post("/measure", summary="Measure one group", operation_id="ifrs17.measure")
    async def measure(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Measure a single group and return the full derivation."""
        ctx.principal.require_scopes("ifrs17:read")
        from pas_plugins.plugin2_ifrs17.csm import ValuationContext  # noqa: PLC0415

        body = await _json_body(request)
        cohort = ContractGroupInput.model_validate(body.get("cohort") or body)
        assumptions = service.assumptions.get(ctx.tenant_id, body.get("assumptionVersion"))
        context = ValuationContext(
            assumptions=assumptions,
            locked_in_curve=assumptions.locked_in_curve,
            updated_curve=assumptions.updated_curve,
            currency=cohort.currency,
            projection_years=service.project_years,
        )
        return service.engine.measure(cohort, context).to_dict()

    @router.post("/roll-forward", summary="Roll a CSM forward", operation_id="ifrs17.rollForward")
    async def roll_forward(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Walk a CSM balance forward one period, itemised and reconciled."""
        ctx.principal.require_scopes("ifrs17:read")
        body = await _json_body(request)
        required = ("openingCsm",)
        for field in required:
            if field not in body:
                raise ValidationError(f"'{field}' is required", field=field)
        return service.roll_forward_group(
            opening_csm=float(body["openingCsm"]),
            locked_in_rate=float(body.get("lockedInRate", 0.047)),
            csm_release=float(body.get("csmRelease", 0.0)),
            new_business_csm=float(body.get("newBusinessCsm", 0.0)),
            experience_adjustments=float(body.get("experienceAdjustments", 0.0)),
            assumption_changes=float(body.get("assumptionChanges", 0.0)),
            previous_loss_component=float(body.get("previousLossComponent", 0.0)),
        )

    @router.post("/valuation-runs", summary="Run a valuation", operation_id="ifrs17.runValuation")
    async def run_valuation(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Extract from the PAS, group, measure and disclose in one call."""
        ctx.principal.require_scopes("ifrs17:write")
        body = await _json_body(request)
        reporting_date = _date(body.get("reportingDate"), "reportingDate")
        run = await service.run(
            ctx,
            reporting_date=reporting_date,
            period=body.get("period"),
            assumption_version=body.get("assumptionVersion"),
            max_workers=int(body.get("maxWorkers", 4)),
        )
        service.audit(ctx, run)
        return run.to_dict()

    @router.get("/valuation-runs", summary="List valuation runs", operation_id="ifrs17.listRuns")
    async def list_runs(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Valuation runs for this carrier, newest last."""
        ctx.principal.require_scopes("ifrs17:read")
        runs = service.runs(ctx.tenant_id)
        return {"count": len(runs), "runs": [r.summary() for r in runs]}

    @router.get("/valuation-runs/{run_id}", summary="Valuation run", operation_id="ifrs17.getRun")
    async def get_run(
        run_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """One valuation run with its grouping, measurements and disclosure."""
        ctx.principal.require_scopes("ifrs17:read")
        return service.run_by_id(run_id, ctx).to_dict()

    @router.get(
        "/valuation-runs/{run_id}/disclosure",
        summary="Disclosure tables",
        operation_id="ifrs17.disclosure",
    )
    async def disclosure(
        run_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """The IFRS 17 disclosure tables for a run, with per-table reconciliation."""
        ctx.principal.require_scopes("ifrs17:read")
        run = service.run_by_id(run_id, ctx)
        if run.disclosure is None:
            raise PasError(
                ErrorCode.UNPROCESSABLE,
                "this run produced no measurements, so there is nothing to disclose",
                {"runId": run.run_id},
            )
        return {
            **run.disclosure,
            "reconciliation": service.disclosure_generator.reconciliation_summary(run.disclosure)
            if hasattr(service, "disclosure_generator")
            else _summarise(run.disclosure),
        }

    @router.get(
        "/valuation-runs/{run_id}/disclosure/export",
        summary="Export disclosure",
        operation_id="ifrs17.exportDisclosure",
    )
    async def export_disclosure(
        run_id: str,
        request: Request,
        format: str = "json",
        ctx: TenantContext = Depends(dependency),
    ) -> Response:
        """Render the disclosure set as JSON, Excel, PDF or inline XBRL."""
        ctx.principal.require_scopes("ifrs17:read")
        fmt = format.lower()
        if fmt not in EXPORTERS:
            raise ValidationError(
                f"unsupported export format '{format}'",
                format=format,
                supported=sorted(EXPORTERS),
            )
        run = service.run_by_id(run_id, ctx)
        payloads = service.disclosures(run, [fmt])
        return Response(
            content=payloads[fmt],
            media_type=MEDIA_TYPES[fmt],
            headers={
                "Content-Disposition": (
                    f'attachment; filename="ifrs17-{run.period}-{run_id[:8]}.{fmt}"'
                )
            },
        )

    @router.post("/demo", summary="Load a worked portfolio", operation_id="ifrs17.loadDemo")
    async def load_demo(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Measure a worked portfolio covering all three models plus a loss group."""
        ctx.principal.require_scopes("ifrs17:read")
        from pas_plugins.plugin2_ifrs17.csm import ValuationContext  # noqa: PLC0415
        from pas_plugins.plugin2_ifrs17.library import (  # noqa: PLC0415
            wavy_portfolio,
            worked_assumption_set,
        )

        if not service.assumptions.versions(ctx.tenant_id):
            service.assumptions.publish(ctx.tenant_id, worked_assumption_set())
        assumptions = service.assumptions.current(ctx.tenant_id)
        context = ValuationContext(
            assumptions=assumptions,
            locked_in_curve=assumptions.locked_in_curve,
            updated_curve=assumptions.updated_curve,
            currency=service.currency,
            projection_years=service.project_years,
        )
        cohorts = wavy_portfolio()
        grouping = service.grouping.group(cohorts, ctx)
        decisions = grouping.by_id()
        results = [
            service.engine.measure(
                cohort.model_copy(update={
                    "profitability": decisions[cohort.group_id].profitability,
                    "locked_in_rate": decisions[cohort.group_id].locked_in_rate,
                })
                if cohort.group_id in decisions else cohort,
                context,
            )
            for cohort in cohorts
        ]
        disclosure = DisclosureGenerator(currency=service.currency).generate(results, period="2026")
        return {
            "portfolio": [c.to_dict() for c in cohorts],
            "grouping": grouping.to_dict(),
            "results": [r.to_dict() for r in results],
            "disclosure": disclosure,
            "illustration": (
                "Synthetic portfolio for evaluation only. Not derived from any carrier's "
                "experience and not suitable for a filing."
            ),
        }

    @router.get("/metrics", summary="Engine metrics", operation_id="ifrs17.metrics")
    async def metrics(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Valuation counts, group counts and disclosure reconciliation status."""
        ctx.principal.require_scopes("ifrs17:read")
        runs = service.runs(ctx.tenant_id)
        return {
            "tenantId": ctx.tenant_id,
            "valuationRuns": len(runs),
            "groupsMeasured": sum(len(r.results) for r in runs),
            "unreconciledRuns": [r.run_id for r in runs if r.disclosure and not r.disclosure.get("allReconciled")],
            "runsWithoutDisclosure": [r.run_id for r in runs if r.disclosure is None],
            "assumptionVersions": len(service.assumptions.versions(ctx.tenant_id)),
            "runtime": GLOBAL_METRICS.snapshot(),
        }

    return router


def _summarise(disclosure: dict[str, Any]) -> dict[str, Any]:
    failures = [
        t["tableId"] for t in disclosure["tables"]
        if not t["reconciliation"].get("balanced", True)
    ]
    return {
        "allReconciled": not failures,
        "failedTables": failures,
        "tableCount": disclosure["tableCount"],
    }


async def _json_body(request: Request) -> Any:  # noqa: ANN401
    try:
        return await request.json()
    except Exception:  # noqa: BLE001
        return {}


def _violations(exc: Exception) -> list[dict[str, Any]]:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        return [
            {"path": ".".join(str(p) for p in e.get("loc", [])), "message": e.get("msg", "")}
            for e in errors()[:20]
        ]
    return []


def _date(value: Any, field: str) -> date:  # noqa: ANN401
    if value in (None, ""):
        raise ValidationError(f"'{field}' is required", field=field)
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValidationError(
            f"'{value}' is not an ISO-8601 date", field=field, value=str(value)
        ) from exc


def problem(status: int, code: str, detail: str, **extra: Any) -> JSONResponse:  # noqa: ANN401
    """Problem+json helper for the disclosure endpoints."""
    return JSONResponse(
        status_code=status,
        content={"code": code, "detail": detail, **extra},
        media_type="application/problem+json",
    )
