"""Plugin 5 HTTP API: catalog, quotes, onboarding, payments and commissions."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from pas_core.app import Platform, context_dependency
from pas_core.errors import PasError
from pas_core.tenancy import TenantContext
from pas_plugins.plugin5_embedded.engine import DistributionEngine, DistributionError
from pas_plugins.plugin5_embedded.models import (
    ApplicantProfile,
    Lead,
    Party,
    PaymentMethod,
)
from pas_plugins.plugin5_embedded.settings import Plugin5Settings


def build_router(platform: Platform, default_engine: DistributionEngine) -> APIRouter:
    """Attach the platform-bound routes."""
    router = APIRouter(prefix="/distribution", tags=["embedded-distribution"])
    dependency = context_dependency(platform)

    def eng() -> DistributionEngine:
        installed = platform.extra.get("distribution_engine")
        return installed or default_engine

    async def _body(request: Request) -> dict[str, Any]:
        payload = await request.json()
        if not isinstance(payload, dict):
            msg = "request body must be a JSON object"
            raise TypeError(msg)
        return payload

    def _fail(exc: DistributionError) -> HTTPException:
        """Map an engine refusal onto the right HTTP status.

        A refusal because the partner is not licensed is a permission problem, not
        a bad request, and returning 400 for it would tell a partner to fix their
        code when in fact they need a licence.
        """
        detail = str(exc)
        if any(word in detail for word in ("not licensed", "may not sell", "tier", "quota", "terminated")):
            return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
        if detail.startswith("unknown"):
            return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)

    # -- catalog -----------------------------------------------------------

    @router.get("/catalog", summary="Products this partner may sell", operation_id="distribution.catalog")
    async def catalog(
        request: Request,
        ctx: TenantContext = Depends(dependency),
        partner_id: str = Query(min_length=2, alias="partnerId"),
        state: str | None = Query(default=None),
        category: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """Products licensed to a partner, filtered by entitlement and jurisdiction."""
        ctx.principal.require_scopes("distribution:read")
        try:
            products = eng().list_catalog(partner_id, state=state, category=category)
        except DistributionError as exc:
            raise _fail(exc) from exc
        return {
            "partnerId": partner_id,
            "state": state,
            "count": len(products),
            "products": [p.to_dict() for p in products],
        }

    @router.get("/partners/{partner_id}", summary="Fetch a partner", operation_id="distribution.getPartner")
    async def get_partner(
        partner_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """A partner's licence, tier, entitlements and quota."""
        ctx.principal.require_scopes("distribution:read")
        partner = eng().get_partner(partner_id)
        if partner is None:
            msg = f"unknown partner '{partner_id}'"
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        return {"partner": partner.to_dict()}

    # -- leads and quotes --------------------------------------------------

    @router.post(
        "/leads",
        status_code=status.HTTP_201_CREATED,
        summary="Capture a prospect",
        operation_id="distribution.createLead",
    )
    async def create_lead(
        request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Capture a lead. The lead records who the consumer belongs to."""
        ctx.principal.require_scopes("distribution:write")
        body = await _body(request)
        try:
            applicant = ApplicantProfile.model_validate(body.get("applicant") or {})
            lead = Lead(
                lead_id=str(body.get("leadId") or f"LEAD-{abs(hash(applicant.first_name)) % 10**8}"),
                partner_id=str(body["partnerId"]),
                applicant=applicant,
                state=applicant.state_of_residence,
                product_id=body.get("productId"),
            )
            created = eng().create_lead(lead)
        except (KeyError, DistributionError) as exc:
            raise _fail(exc if isinstance(exc, DistributionError) else DistributionError(str(exc))) from exc
        return {"lead": created.to_dict()}

    @router.post("/quotes", summary="Quote a product for a partner", operation_id="distribution.quote")
    async def quote(
        request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Price for a consumer, including what the partner earns."""
        ctx.principal.require_scopes("distribution:read")
        body = await _body(request)
        try:
            applicant = ApplicantProfile.model_validate(body.get("applicant") or {})
            result = eng().quote(
                tenant_id=ctx.tenant.tenant_id,
                partner_id=str(body["partnerId"]),
                product_id=str(body["productId"]),
                applicant=applicant,
                face_amount=float(body["faceAmount"]),
                term_years=body.get("termYears"),
                lead_id=body.get("leadId"),
            )
        except DistributionError as exc:
            raise _fail(exc) from exc
        except (KeyError, ValueError) as exc:
            raise PasError("validation_error", str(exc), {"partnerId": body.get("partnerId")}) from exc
        return {"quote": result.to_dict()}

    @router.get("/quotes/{quote_id}", summary="Fetch a quote", operation_id="distribution.getQuote")
    async def get_quote(
        quote_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """A quote with its suitability assessment and disclosures."""
        ctx.principal.require_scopes("distribution:read")
        found = eng().get_quote(quote_id)
        if found is None:
            msg = f"unknown quote '{quote_id}'"
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        return {"quote": found.to_dict()}

    # -- onboarding --------------------------------------------------------

    @router.post(
        "/quotes/{quote_id}/kyc",
        summary="Screen a quote for identity, sanctions and PEP",
        operation_id="distribution.screen",
    )
    async def screen(
        quote_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Run KYC. A confirmed sanctions match is never a clear."""
        ctx.principal.require_scopes("distribution:kyc")
        body = await _body(request)
        try:
            result = eng().screen(
                quote_id,
                str(body.get("documentType", "unknown")),
                str(body.get("documentReference", "unknown")),
            )
        except DistributionError as exc:
            raise _fail(exc) from exc
        return {"quoteId": quote_id, "kyc": result.to_dict()}

    @router.post(
        "/quotes/{quote_id}/bind",
        status_code=status.HTTP_201_CREATED,
        summary="Bind a policy from a quote",
        operation_id="distribution.bind",
    )
    async def bind(
        quote_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Bind a policy. Refused without a bindable quote, KYC and suitability."""
        ctx.principal.require_scopes("distribution:write")
        try:
            policy = eng().bind(quote_id)
        except DistributionError as exc:
            raise _fail(exc) from exc
        return {"policy": policy.to_dict()}

    @router.get("/policies/{policy_id}", summary="Fetch a policy", operation_id="distribution.getPolicy")
    async def get_policy(
        policy_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """A bound policy, including free-look and commission-payability state."""
        ctx.principal.require_scopes("distribution:read")
        policy = eng().get_policy(policy_id)
        if policy is None:
            msg = f"unknown policy '{policy_id}'"
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        return {"policy": policy.to_dict()}

    # -- money -------------------------------------------------------------

    @router.post(
        "/quotes/{quote_id}/payment-intent",
        status_code=status.HTTP_201_CREATED,
        summary="Request the first premium",
        operation_id="distribution.createIntent",
    )
    async def create_intent(
        quote_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Ask for the first premium. Payment follows a bound policy, never precedes it."""
        ctx.principal.require_scopes("distribution:write")
        body = await _body(request)
        method = PaymentMethod(body.get("method", "ach"))
        try:
            intent = eng().create_payment_intent(quote_id, method)
        except DistributionError as exc:
            raise _fail(exc) from exc
        return {"intent": intent.to_dict()}

    @router.post(
        "/intents/{intent_id}/capture",
        status_code=status.HTTP_201_CREATED,
        summary="Record that money arrived",
        operation_id="distribution.capture",
    )
    async def capture(
        intent_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Capture a payment intent. This records an arrival; it is not a ledger."""
        ctx.principal.require_scopes("distribution:write")
        body = await _body(request)
        try:
            payment = eng().capture_payment(
                intent_id,
                success=bool(body.get("success", True)),
                amount=body.get("amount"),
            )
        except DistributionError as exc:
            raise _fail(exc) from exc
        return {"payment": payment.to_dict()}

    @router.get("/reconciliation", summary="Quote-to-cash reconciliation", operation_id="distribution.reconcile")
    async def reconcile(
        request: Request,
        ctx: TenantContext = Depends(dependency),
        partner_id: str = Query(min_length=2, alias="partnerId"),
    ) -> dict[str, Any]:
        """Compare quoted, bound and collected so both sides see the same figures."""
        ctx.principal.require_scopes("distribution:read")
        rows = eng().reconcile(partner_id)
        outstanding = round(sum(r.outstanding for r in rows if r.outstanding > 0), 2)
        return {
            "partnerId": partner_id,
            "rowCount": len(rows),
            "totalOutstanding": outstanding,
            "rows": [r.to_dict() for r in rows],
        }

    # -- commission --------------------------------------------------------

    @router.get(
        "/commissions/statement",
        summary="Partner commission statement",
        operation_id="distribution.commissionStatement",
    )
    async def commission_statement(
        request: Request,
        ctx: TenantContext = Depends(dependency),
        partner_id: str = Query(min_length=2, alias="partnerId"),
        period_start: date = Query(alias="periodStart"),
        period_end: date = Query(alias="periodEnd"),
    ) -> dict[str, Any]:
        """What a partner earned in a period.

        Commission on a policy inside free look is accrued, not payable: presenting
        it as payable invites a partner to book revenue a cancellation will remove.
        """
        ctx.principal.require_scopes("distribution:read")
        try:
            statement = eng().commission_statement(
                ctx.tenant.tenant_id, partner_id, period_start, period_end
            )
        except DistributionError as exc:
            raise _fail(exc) from exc
        return {"statement": statement.to_dict()}

    # -- events and configuration -----------------------------------------

    @router.get("/events", summary="Partner webhook events", operation_id="distribution.events")
    async def events(
        request: Request,
        ctx: TenantContext = Depends(dependency),
        partner_id: str | None = Query(default=None, alias="partnerId"),
    ) -> dict[str, Any]:
        """The events a partner would be notified about, for replay or reconciliation."""
        ctx.principal.require_scopes("distribution:read")
        found = eng().events(partner_id)
        return {
            "partnerId": partner_id,
            "count": len(found),
            "deliveredCount": sum(1 for e in found if e.delivered),
            "events": [e.to_dict() for e in found],
        }

    @router.get("/meta", summary="Distribution configuration", operation_id="distribution.meta")
    async def meta(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """The rules this deployment enforces, so a partner can predict them."""
        ctx.principal.require_scopes("distribution:read")
        settings: Plugin5Settings = eng().settings
        from pas_plugins.plugin5_embedded.engine import (  # noqa: PLC0415
            INSTANT_DECISION_FACE_LIMIT,
            OVER_INSURED_BLOCK_RATIO,
            OVER_INSURED_REVIEW_RATIO,
        )

        return {
            "quoteTtlMinutes": settings.quote_ttl_minutes,
            "freeLookDays": settings.free_look_days,
            "requireSuitabilityCheck": settings.require_suitability_check,
            "requireKycBeforeBind": settings.require_kyc_before_bind,
            "commissionHaircutBps": settings.commission_haircut_bps,
            "suitabilityThresholds": {
                "reviewRatio": OVER_INSURED_REVIEW_RATIO,
                "blockRatio": OVER_INSURED_BLOCK_RATIO,
                "instantDecisionFaceLimit": INSTANT_DECISION_FACE_LIMIT,
            },
            "paymentMethods": [str(m) for m in PaymentMethod],
            "gates": ["suitability", "kyc", "bind", "pay", "commission"],
        }

    @router.post(
        "/parties",
        status_code=status.HTTP_201_CREATED,
        summary="Record a contracting party",
        operation_id="distribution.createParty",
    )
    async def create_party(
        request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Record the legal entity behind a partner arrangement."""
        ctx.principal.require_scopes("distribution:write")
        party = Party.model_validate(await _body(request))
        return {"party": party.to_dict()}

    return router


__all__ = ["build_router"]