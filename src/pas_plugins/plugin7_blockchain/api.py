"""Plugin 7 HTTP API: the query layer over the policy ledger.

Route prefixes follow the deliverable in the brief: `/blockchain/v1/...`.

The API is a *query and submit* layer. It cannot mutate the ledger directly - every
write goes through `submit_event`, which runs chaincode and requires endorsements.
There is no endpoint that edits an event, because there is no such operation.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from pas_core.app import Platform, context_dependency
from pas_core.errors import PasError
from pas_core.tenancy import TenantContext
from pas_plugins.plugin7_blockchain.chaincode import ChaincodeError
from pas_plugins.plugin7_blockchain.models import (
    Beneficiary,
    EventType,
    PortabilityPackage,
)
from pas_plugins.plugin7_blockchain.service import LedgerError, PolicyLedgerService


def build_router(platform: Platform, default_service: PolicyLedgerService) -> APIRouter:
    """Attach the platform-bound routes."""
    router = APIRouter(prefix="/blockchain/v1", tags=["policy-ledger"])
    dependency = context_dependency(platform)

    def svc() -> PolicyLedgerService:
        installed = platform.extra.get("ledger_service")
        return installed or default_service

    async def _body(request: Request) -> dict[str, Any]:
        payload = await request.json()
        if not isinstance(payload, dict):
            msg = "request body must be a JSON object"
            raise TypeError(msg)
        return payload

    def _fail(exc: Exception) -> HTTPException:
        detail = str(exc)
        if detail.startswith("unknown"):
            return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)

    # -- network -----------------------------------------------------------

    @router.get("/network", summary="Network participants and channel", operation_id="ledger.network")
    async def network(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """The organisations on the channel and their roles.

        Roles matter because they determine who may endorse: a regulator on this
        channel observes and cannot write.
        """
        ctx.principal.require_scopes("ledger:read")
        service = svc()
        channel = service._ledger.channels[service.channel_id]  # noqa: SLF001
        return {
            "channel": channel.to_dict(),
            "organizations": [
                {
                    **org.to_dict(),
                    "canEndorsePolicyState": org.role.can_endorse_policy_state,
                    "canAttestIdentity": org.role.can_attest_identity,
                }
                for org in service.organizations()
            ],
        }

    @router.get("/blocks", summary="Committed blocks", operation_id="ledger.blocks")
    async def blocks(
        request: Request,
        ctx: TenantContext = Depends(dependency),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        """Block headers with their hash links, Merkle roots and anchor receipts."""
        ctx.principal.require_scopes("ledger:read")
        chain = svc().blocks()
        return {
            "count": len(chain),
            "blocks": [b.to_dict() for b in chain[-limit:]],
        }

    @router.get("/transactions", summary="Chaincode transactions", operation_id="ledger.transactions")
    async def transactions(
        request: Request,
        ctx: TenantContext = Depends(dependency),
        status_filter: str | None = Query(default=None, alias="status"),
    ) -> dict[str, Any]:
        """Transactions with their endorsements and commit status."""
        ctx.principal.require_scopes("ledger:read")
        found = svc().transactions()
        if status_filter:
            found = [t for t in found if str(t.status) == status_filter]
        return {
            "count": len(found),
            "endorsementSummary": [
                {"txId": t.tx_id, "endorsingOrgs": t.endorsing_orgs, "requiredOrgs": t.required_orgs}
                for t in found
            ],
            "transactions": [t.to_dict() for t in found],
        }

    @router.get("/verify", summary="Audit the chain", operation_id="ledger.verify")
    async def verify(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Recompute every hash, Merkle root and anchor receipt.

        This is the endpoint a regulator or an auditor calls, and it is expected to
        say "not trustworthy" when something has been altered.
        """
        ctx.principal.require_scopes("ledger:read")
        return {"verification": svc().verify()}

    # -- policies ----------------------------------------------------------

    @router.post(
        "/policies",
        status_code=status.HTTP_201_CREATED,
        summary="Create a policy and commit its issue event",
        operation_id="ledger.createPolicy",
    )
    async def create_policy(
        request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Issue a policy. The `issue` event is endorsed and committed immediately."""
        ctx.principal.require_scopes("ledger:write")
        body = await _body(request)
        try:
            beneficiaries = [Beneficiary.model_validate(b) for b in body.get("beneficiaries", [])]
            policy = svc().create_policy(
                tenant_id=ctx.tenant.tenant_id,
                policy_number=str(body["policyNumber"]),
                holder_subject=str(body["holderSubject"]),
                product_code=str(body["productCode"]),
                face_amount=float(body["faceAmount"]),
                annual_premium=float(body.get("annualPremium", 0.0)),
                beneficiaries=beneficiaries,
                actor_subject=ctx.principal.subject,
            )
        except (KeyError, ValueError) as exc:
            if isinstance(exc, ChaincodeError):
                raise _fail(exc) from exc
            raise PasError("validation_error", str(exc), {}) from exc
        except LedgerError as exc:
            raise _fail(exc) from exc
        return {"policy": policy.to_dict(), "history": [e.to_dict() for e in svc().history(policy.policy_id)]}

    @router.get("/policies", summary="List policies", operation_id="ledger.listPolicies")
    async def list_policies(
        request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Policies visible to the caller's tenant."""
        ctx.principal.require_scopes("ledger:read")
        found = svc().policies(ctx.tenant.tenant_id)
        return {"count": len(found), "policies": [p.to_dict() for p in found]}

    @router.get("/policies/{policy_id}", summary="Policy state", operation_id="ledger.getPolicy")
    async def get_policy(
        policy_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """The current projection of a policy."""
        ctx.principal.require_scopes("ledger:read")
        policy = svc().get_policy(policy_id)
        if policy is None:
            msg = f"unknown policy '{policy_id}'"
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        return {"policy": policy.to_dict()}

    @router.get(
        "/policies/{policy_id}/history",
        summary="Full policy history",
        operation_id="ledger.policyHistory",
    )
    async def policy_history(
        policy_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Every committed event for a policy, hash-chained and in sequence."""
        ctx.principal.require_scopes("ledger:read")
        events = svc().history(policy_id)
        if not events:
            msg = f"policy '{policy_id}' has no events"
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        return {"policyId": policy_id, "count": len(events), "events": [e.to_dict() for e in events]}

    @router.post(
        "/policies/{policy_id}/events",
        status_code=status.HTTP_201_CREATED,
        summary="Submit a lifecycle event",
        operation_id="ledger.submitEvent",
    )
    async def submit_event(
        policy_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Propose a lifecycle change; committed only once endorsed.

        An illegal transition is refused with 422 and nothing is written.
        """
        ctx.principal.require_scopes("ledger:write")
        body = await _body(request)
        try:
            event_type = EventType(body["eventType"])
        except (KeyError, ValueError) as exc:
            raise PasError("validation_error", f"unknown eventType: {body.get('eventType')}", {}) from exc
        try:
            tx = svc().submit_event(
                policy_id=policy_id,
                event_type=event_type,
                payload=body.get("payload") or {},
                actor_org=str(body.get("actorOrg", "org-carrier")),
                actor_subject=ctx.principal.subject,
            )
        except LedgerError as exc:
            raise _fail(exc) from exc
        return {"transaction": tx.to_dict()}

    @router.get(
        "/events/{event_id}/proof",
        summary="Merkle inclusion proof for an event",
        operation_id="ledger.eventProof",
    )
    async def event_proof(
        event_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Prove an event is in a block, verifiable against the block's Merkle root."""
        ctx.principal.require_scopes("ledger:read")
        try:
            return {"proof": svc().event_proof(event_id)}
        except LedgerError as exc:
            raise _fail(exc) from exc

    # -- beneficiaries -----------------------------------------------------

    @router.post(
        "/policies/{policy_id}/beneficiary-changes",
        status_code=status.HTTP_201_CREATED,
        summary="Propose a beneficiary change",
        operation_id="ledger.requestBeneficiaryChange",
    )
    async def request_beneficiary_change(
        policy_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Record a proposed change. It takes effect only after multi-signature approval."""
        ctx.principal.require_scopes("ledger:write")
        body = await _body(request)
        try:
            beneficiaries = [Beneficiary.model_validate(b) for b in body["beneficiaries"]]
            change = svc().request_beneficiary_change(
                policy_id,
                beneficiaries,
                str(body["reason"]),
                str(body.get("requestId") or f"BC-{len(svc().history(policy_id))}"),
            )
        except (KeyError, ValueError) as exc:
            raise PasError("validation_error", str(exc), {}) from exc
        except LedgerError as exc:
            raise _fail(exc) from exc
        return {"changeRequest": change.to_dict()}

    @router.post(
        "/beneficiary-changes/{request_id}/endorse",
        summary="Approve a beneficiary change",
        operation_id="ledger.endorseBeneficiaryChange",
    )
    async def endorse_beneficiary_change(
        request_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Add one organisation's approval. Quorum is required before it can apply."""
        ctx.principal.require_scopes("ledger:write")
        body = await _body(request)
        try:
            svc().endorse_beneficiary_change(request_id, str(body["orgId"]))
        except (KeyError, ValueError) as exc:
            raise PasError("validation_error", str(exc), {}) from exc
        except LedgerError as exc:
            raise _fail(exc) from exc
        change = svc().beneficiary_request(request_id)
        assert change is not None
        return {"changeRequest": change.to_dict()}

    @router.post(
        "/beneficiary-changes/{request_id}/apply",
        summary="Apply an approved beneficiary change",
        operation_id="ledger.applyBeneficiaryChange",
    )
    async def apply_beneficiary_change(
        request_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Apply a change that has reached quorum, and record it permanently."""
        ctx.principal.require_scopes("ledger:write")
        try:
            policy = svc().apply_beneficiary_change(request_id, actor_subject=ctx.principal.subject)
        except LedgerError as exc:
            raise _fail(exc) from exc
        return {"policy": policy.to_dict()}

    # -- claims ------------------------------------------------------------

    @router.post(
        "/policies/{policy_id}/claims",
        status_code=status.HTTP_201_CREATED,
        summary="File a claim",
        operation_id="ledger.fileClaim",
    )
    async def file_claim(
        policy_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """File a claim. Large claims are held for human approval automatically."""
        ctx.principal.require_scopes("ledger:write")
        body = await _body(request)
        try:
            claim, tx = svc().file_claim(
                policy_id,
                claim_id=str(body["claimId"]),
                amount_requested=float(body["amountRequested"]),
                claim_type=str(body.get("claimType", "death")),
                subject=ctx.principal.subject,
            )
        except (KeyError, ValueError) as exc:
            raise PasError("validation_error", str(exc), {}) from exc
        except LedgerError as exc:
            raise _fail(exc) from exc
        return {"claim": claim.to_dict(), "transaction": tx.to_dict()}

    @router.get("/claims/{claim_id}", summary="Claim state", operation_id="ledger.getClaim")
    async def get_claim(
        claim_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """A claim and whether it needed manual approval."""
        ctx.principal.require_scopes("ledger:read")
        claim = svc().get_claim(claim_id)
        if claim is None:
            msg = f"unknown claim '{claim_id}'"
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        return {"claim": claim.to_dict()}

    @router.post("/claims/{claim_id}/approve", summary="Approve and pay a claim", operation_id="ledger.approveClaim")
    async def approve_claim(
        claim_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Approve a claim. Approving more than was requested is refused."""
        ctx.principal.require_scopes("ledger:write")
        body = await _body(request)
        try:
            claim = svc().approve_claim(
                claim_id, float(body["amountApproved"]), reason=str(body.get("reason", ""))
            )
        except (KeyError, ValueError) as exc:
            raise PasError("validation_error", str(exc), {}) from exc
        except LedgerError as exc:
            raise _fail(exc) from exc
        return {"claim": claim.to_dict()}

    @router.post("/claims/{claim_id}/decline", summary="Decline a claim", operation_id="ledger.declineClaim")
    async def decline_claim(
        claim_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Decline a claim, with the reason recorded on the ledger."""
        ctx.principal.require_scopes("ledger:write")
        body = await _body(request)
        try:
            claim = svc().decline_claim(claim_id, str(body["reason"]))
        except (KeyError, ValueError) as exc:
            raise PasError("validation_error", str(exc), {}) from exc
        except LedgerError as exc:
            raise _fail(exc) from exc
        return {"claim": claim.to_dict()}

    # -- annuities ---------------------------------------------------------

    @router.post(
        "/policies/{policy_id}/annuity-schedule",
        status_code=status.HTTP_201_CREATED,
        summary="Generate an annuity drawdown schedule",
        operation_id="ledger.scheduleAnnuity",
    )
    async def schedule_annuity(
        policy_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Record the payout obligation before any money moves."""
        ctx.principal.require_scopes("ledger:write")
        body = await _body(request)
        try:
            entries = svc().schedule_annuity(
                policy_id,
                starting_value=float(body["startingValue"]),
                monthly_withdrawal=float(body["monthlyWithdrawal"]),
                periods=int(body.get("periods", 12)),
                tax_rate_bps=int(body.get("taxRateBps", 1500)),
            )
        except (KeyError, ValueError) as exc:
            raise PasError("validation_error", str(exc), {}) from exc
        except LedgerError as exc:
            raise _fail(exc) from exc
        return {"entries": [e.to_dict() for e in entries]}

    @router.get(
        "/policies/{policy_id}/annuity-position",
        summary="Reconstruct an annuity position",
        operation_id="ledger.annuityPosition",
    )
    async def annuity_position(
        policy_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Paid and remaining withdrawals, derived from the ledger rather than a table."""
        ctx.principal.require_scopes("ledger:read")
        return {"position": svc().annuity_position(policy_id)}

    @router.post(
        "/annuity-entries/{entry_id}/pay",
        summary="Record an annuity payout",
        operation_id="ledger.payAnnuityEntry",
    )
    async def pay_annuity_entry(
        entry_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Mark a scheduled drawdown paid. Paying twice is refused."""
        ctx.principal.require_scopes("ledger:write")
        body = await _body(request)
        try:
            entry = svc().pay_annuity_entry(str(body["policyId"]), entry_id)
        except (KeyError, ValueError) as exc:
            raise PasError("validation_error", str(exc), {}) from exc
        except LedgerError as exc:
            raise _fail(exc) from exc
        return {"entry": entry.to_dict()}

    # -- portability and identity -----------------------------------------

    @router.post(
        "/policies/{policy_id}/export",
        summary="Export a self-verifying portability package",
        operation_id="ledger.exportPackage",
    )
    async def export_package(
        policy_id: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """A signed export another carrier can import and verify independently."""
        ctx.principal.require_scopes("ledger:read")
        try:
            package = svc().export_package(policy_id, exported_by=ctx.principal.subject)
        except LedgerError as exc:
            raise _fail(exc) from exc
        return {"package": package.to_dict()}

    @router.post("/packages/verify", summary="Verify a portability package", operation_id="ledger.verifyPackage")
    async def verify_package(
        request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Verify an export without trusting the ledger that produced it."""
        ctx.principal.require_scopes("ledger:read")
        body = await _body(request)
        try:
            package = PortabilityPackage.model_validate(body["package"])
        except (KeyError, ValueError) as exc:
            raise PasError("validation_error", str(exc), {}) from exc
        return {"verification": svc().verify_package(package)}

    @router.get(
        "/identity/{did}",
        summary="Resolve a DID",
        operation_id="ledger.resolveDid",
    )
    async def resolve_did(
        did: str, request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """A DID document with its verification methods."""
        ctx.principal.require_scopes("ledger:read")
        document = svc().identity.resolve(did)
        if document is None:
            msg = f"unresolvable DID '{did}'"
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)
        return {"didDocument": document.to_dict()}

    @router.post(
        "/identity/credentials",
        status_code=status.HTTP_201_CREATED,
        summary="Issue a verifiable credential",
        operation_id="ledger.issueCredential",
    )
    async def issue_credential(
        request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Issue an Ed25519-signed credential about a subject."""
        ctx.principal.require_scopes("ledger:write")
        body = await _body(request)
        try:
            credential = svc().identity.issue_credential(
                issuer_subject=str(body.get("issuer", "org-carrier")),
                subject=dict(body["subject"]),
                credential_type=str(body.get("credentialType", "BeneficiaryRelationship")),
            )
        except (KeyError, ValueError) as exc:
            raise PasError("validation_error", str(exc), {}) from exc
        return {"credential": credential.to_dict()}

    @router.post(
        "/identity/credentials/verify",
        summary="Verify a verifiable credential",
        operation_id="ledger.verifyCredential",
    )
    async def verify_credential(
        request: Request, ctx: TenantContext = Depends(dependency)
    ) -> dict[str, Any]:
        """Check signature, issuer, expiry and revocation, naming whichever failed."""
        ctx.principal.require_scopes("ledger:read")
        body = await _body(request)
        from pas_plugins.plugin7_blockchain.models import VerifiableCredential  # noqa: PLC0415

        try:
            credential = VerifiableCredential.model_validate(body["credential"])
        except (KeyError, ValueError) as exc:
            raise PasError("validation_error", str(exc), {}) from exc
        return {"verification": svc().identity.verify_credential(credential)}

    @router.get("/health/detailed", summary="Ledger inventory", operation_id="ledger.inventory")
    async def inventory(request: Request, ctx: TenantContext = Depends(dependency)) -> dict[str, Any]:
        """Counts and the current chain-trust position."""
        ctx.principal.require_scopes("ledger:read")
        return {"ledger": svc().health_summary()}

    return router


__all__ = ["build_router"]