"""Plugin 4 HTTP API: product drafts, validation, quoting and publication."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from pas_core.app import Platform, context_dependency
from pas_core.errors import PasError
from pas_core.tenancy import TenantContext
from pas_plugins.plugin4_productconfig.models import (
    ProductDefinition,
    QuoteRequest,
    SimulationProfile,
)
from pas_plugins.plugin4_productconfig.rating import RatingError
from pas_plugins.plugin4_productconfig.service import (
    ProductConfigError,
    ProductConfigService,
)


def build_router(platform: Platform, default_service: ProductConfigService) -> APIRouter:
    """Attach the platform-bound routes.

    `default_service` is captured as the fallback; each request resolves the live
    service from app state first. That indirection lets a test swap in an isolated
    instance without changing production wiring, where the two are the same object.
    """
    router = APIRouter(prefix="/products", tags=["product-config"])
    dependency = context_dependency(platform)

    def svc() -> ProductConfigService:
        """Resolve the live service, preferring one installed on the platform.

        Production wires the default instance; a test can substitute an isolated
        one via ``platform.extra["product_service"]`` without touching production
        wiring. Storing it in `extra` is deliberate - Platform is a frozen-ish
        dataclass of infrastructure handles, not a place for plugin state.
        """
        installed = platform.extra.get("product_service")
        return installed or default_service

    async def _body(request: Request) -> dict[str, Any]:
        payload = await request.json()
        if not isinstance(payload, dict):
            msg = "request body must be a JSON object"
            raise TypeError(msg)
        return payload

    # -- discovery ---------------------------------------------------------

    @router.get("/meta", summary="Supported constructs", operation_id="products.meta")
    async def meta(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """What a product author can configure, and the operators rules may use."""
        from pas_plugins.plugin4_productconfig.compliance import (  # noqa: PLC0415
            ALLOWED_RULE_FIELDS,
            MIN_MARGIN_BPS_WARNING,
        )
        from pas_plugins.plugin4_productconfig.models import (  # noqa: PLC0415
            BenefitCategory,
            ChargeFrequency,
            EligibilityRule,
            ProductCategory,
        )
        from pas_plugins.plugin4_productconfig.service import (  # noqa: PLC0415
            ASSUMED_BENEFIT_COST_PER_THOUSAND,
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
            "lifecycle": ["draft", "validate", "simulate", "publish", "retire"],
        }

    # -- drafts ------------------------------------------------------------

    @router.post(
        "/drafts",
        status_code=status.HTTP_201_CREATED,
        summary="Create or update a product draft",
        operation_id="products.saveDraft",
    )
    async def save_draft(
        request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Save a draft definition. Published versions are never modified."""
        ctx.principal.require_scopes("products:write")
        try:
            definition = ProductDefinition.model_validate(await _body(request))
        except PasError:
            raise
        except Exception as exc:
            raise PasError("validation_error", str(exc), {"productId": None}) from exc
        saved = svc().save_draft(definition)
        return {"draft": saved.to_dict()}

    @router.get("/drafts/{product_id}", summary="Fetch a draft", operation_id="products.getDraft")
    async def get_draft(
        product_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """The current mutable definition of a product."""
        ctx.principal.require_scopes("products:read")
        draft = svc().get_draft(product_id)
        if draft is None:
            msg = f"no draft for product '{product_id}'"
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        return {"draft": draft.to_dict()}

    @router.post(
        "/drafts/{product_id}/validate",
        summary="Run guardrails against a draft",
        operation_id="products.validate",
    )
    async def validate_draft(
        product_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Structural, rating, compliance and economics findings, with remediation."""
        ctx.principal.require_scopes("products:read")
        payload = await _body(request)
        definition = (
            ProductDefinition.model_validate(payload["definition"])
            if payload.get("definition")
            else svc().get_draft(product_id)
        )
        if definition is None:
            msg = f"no draft for product '{product_id}'"
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        return {"report": svc().validate(definition).to_dict()}

    # -- simulation --------------------------------------------------------

    @router.post(
        "/drafts/{product_id}/simulate",
        summary="Simulate economics against a book of business",
        operation_id="products.simulate",
    )
    async def simulate(
        product_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Price a synthetic book of business against a definition.

        Reports premium, margin, loss ratio and how many applicants could not be
        priced at all. The benefit-cost assumption is stated in the notes; replace
        it with a real mortality basis for an actuarial estimate.
        """
        ctx.principal.require_scopes("products:read")
        payload = await _body(request)
        definition = (
            ProductDefinition.model_validate(payload["definition"])
            if payload.get("definition")
            else svc().get_draft(product_id)
        )
        if definition is None:
            msg = f"no draft for product '{product_id}'"
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        profile = SimulationProfile.model_validate(payload.get("profile") or {})
        result = svc().simulate(
            definition,
            profile,
            policies=int(payload.get("policies", 500)),
            seed=int(payload.get("seed", 424242)),
        )
        return {"simulation": result.to_dict()}

    # -- publication -------------------------------------------------------

    @router.post(
        "/{product_id}/publish",
        status_code=status.HTTP_201_CREATED,
        summary="Publish a draft as an immutable version",
        operation_id="products.publish",
    )
    async def publish(
        product_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Publish the draft. Refused while any guardrail blocker stands."""
        ctx.principal.require_scopes("products:publish")
        payload = await _body(request)
        try:
            published = svc().publish(product_id, ctx.principal.subject)
        except ProductConfigError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc
        return {
            "published": published.to_dict(),
            "publishedBy": ctx.principal.subject,
            "requestedBy": payload.get("actor"),
        }

    @router.get(
        "/{product_id}/versions",
        summary="Published versions of a product",
        operation_id="products.listVersions",
    )
    async def list_versions(
        product_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Every published version, oldest first, with content hashes."""
        ctx.principal.require_scopes("products:read")
        versions = svc().list_published(product_id)
        return {
            "productId": product_id,
            "count": len(versions),
            "versions": [v.to_dict() for v in versions],
        }

    @router.get(
        "/{product_id}/versions/{version}",
        summary="Fetch one published version",
        operation_id="products.getVersion",
    )
    async def get_version(
        product_id: str, version: int, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """A published version, including the guardrail state at publication."""
        ctx.principal.require_scopes("products:read")
        published = svc().get_published(product_id, version)
        if published is None:
            msg = f"product '{product_id}' has no published version {version}"
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        return {"published": published.to_dict()}

    @router.get(
        "/{product_id}/versions/{from_version}/diff/{to_version}",
        summary="Diff two published versions",
        operation_id="products.diffVersions",
    )
    async def diff_versions(
        product_id: str,
        from_version: int,
        to_version: int,
        request: Request,
        ctx: TenantContext = Depends(dependency),
    ) -> dict[str, Any]:
        """Structural difference between two versions, flagged for materiality."""
        ctx.principal.require_scopes("products:read")
        try:
            diff = svc().diff(product_id, from_version, to_version)
        except ProductConfigError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return {"diff": diff.to_dict()}

    @router.post(
        "/{product_id}/versions/{version}/retire",
        summary="Withdraw a published version",
        operation_id="products.retire",
    )
    async def retire(
        product_id: str,
        version: int,
        request: Request,
        ctx: TenantContext = Depends(dependency),
    ) -> dict[str, Any]:
        """Mark a version retired. Its content and hash are left intact."""
        ctx.principal.require_scopes("products:publish")
        try:
            retired = svc().retire(product_id, version)
        except ProductConfigError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
        return {"published": retired.to_dict()}

    # -- quoting -----------------------------------------------------------

    @router.post("/quote", summary="Price a quote", operation_id="products.quote")
    async def quote(
        request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Price against the latest published version, itemised and reproducible.

        The tenant is taken from the authenticated context, never from the body.
        Accepting a caller-supplied `tenantId` would let a caller quote against
        another carrier's product by putting their id in the request.
        """
        ctx.principal.require_scopes("products:read")
        body = await _body(request)
        use_draft = bool(body.pop("useDraft", False))
        body["tenantId"] = ctx.tenant.tenant_id
        try:
            payload = QuoteRequest.model_validate(body)
        except Exception as exc:
            raise PasError("validation_error", str(exc), {"productId": body.get("productId")}) from exc
        try:
            result = svc().quote(payload, use_published=not use_draft)
        except ProductConfigError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except RatingError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        return {"quote": result.to_dict()}

    @router.get(
        "/{product_id}/covers/{cover_id}/price",
        summary="Age/face price matrix for a cover",
        operation_id="products.priceMatrix",
    )
    async def price_matrix(
        product_id: str,
        cover_id: str,
        request: Request,
        ctx: TenantContext = Depends(dependency),
        ages: int = Query(default=7, ge=1, le=40),
        min_face: float = Query(default=100_000.0, alias="minFace", gt=0.0),
        max_face: float = Query(default=2_000_000.0, alias="maxFace", gt=0.0),
    ) -> dict[str, Any]:
        """A grid of quotes across age and face amount.

        This is the view a product manager checks before filing rates, so it
        reports the unpriceable cells explicitly instead of omitting them.
        """
        ctx.principal.require_scopes("products:read")
        published = svc().get_published(product_id)
        if published is None or published.definition is None:
            msg = f"product '{product_id}' has no published version"
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        definition = published.definition
        cover = definition.cover(cover_id)
        table = definition.table(cover.rating_table_id) if cover else None
        if cover is None or table is None:
            msg = f"product '{product_id}' has no cover '{cover_id}'"
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)

        band_low, band_high = table.covers
        high = band_high if band_high is not None else cover.max_issue_age
        age_points = [band_low + round(i * (high - band_low) / max(ages - 1, 1)) for i in range(ages)]
        face_points = [min_face, (min_face + max_face) / 2.0, max_face]

        rows: list[dict[str, Any]] = []
        unpriceable = 0
        for age in sorted(set(age_points)):
            cells: list[dict[str, Any]] = []
            for face in face_points:
                request_obj = QuoteRequest(
                    tenant_id=ctx.tenant.tenant_id,
                    product_id=product_id,
                    cover_id=cover_id,
                    age=age,
                    face_amount=face,
                    term_years=cover.term_options_years[0] if cover.term_options_years else None,
                )
                try:
                    priced = svc().quote(request_obj)
                except RatingError as exc:
                    unpriceable += 1
                    cells.append({"faceAmount": face, "error": str(exc)})
                    continue
                cells.append(
                    {
                        "faceAmount": face,
                        "annualPremium": round(priced.annual_premium, 2),
                        "monthlyPremium": round(priced.monthly_premium, 2),
                        "ratingBand": priced.rating_band,
                    }
                )
            rows.append({"age": age, "cells": cells})

        return {
            "productId": product_id,
            "coverId": cover_id,
            "version": published.version,
            "rows": rows,
            "unpriceableCells": unpriceable,
        }

    return router


__all__ = ["build_router"]