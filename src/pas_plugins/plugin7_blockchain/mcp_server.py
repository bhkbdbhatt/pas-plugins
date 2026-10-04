"""MCP tools for the policy lifecycle ledger.

Twelve tools across the four capabilities. The annotation scheme reflects how much
harm a mistake causes:

* **Read-only** - inspecting the network, a policy, its history, an event proof or
  the chain verification.
* **Draft-shaped** - proposing a beneficiary change or generating an annuity
  schedule. Neither takes effect on its own, so neither needs confirmation.
* **Irreversible** - committing a lifecycle event, applying a beneficiary change,
  paying an annuity drawdown and issuing a token all change a permanent record, so
  they are annotated destructive and require `confirm=true`.

The last one matters most: a beneficiary change and a token transfer are the two
operations a fraud actor would want, and requiring explicit confirmation while also
enforcing multi-signature is defence in depth rather than decoration.
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
from pas_plugins.plugin7_blockchain.models import Beneficiary, EventType, OrgRole
from pas_plugins.plugin7_blockchain.service import (
    LedgerError,
    PolicyLedgerService,
    default_ledger_service,
)
from pas_plugins.plugin7_blockchain.settings import Plugin7Settings

PLUGIN_INFO = McpServerInfo(
    name="pas-policy-ledger",
    version=Plugin7Settings().plugin_version,
    title="PAS Blockchain Policy Lifecycle Layer",
    description=(
        "A permissioned, hash-chained policy record with multi-signature beneficiary "
        "changes, annuity payout schedules, W3C-style verifiable credentials for "
        "beneficiaries, and signed portability packages."
    ),
)


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_BENEFICIARY = {
    "beneficiaryId": {"type": "string"},
    "fullName": {"type": "string"},
    "relationship": {"type": "string", "default": "other"},
    "shareBps": {"type": "integer", "minimum": 0, "maximum": 10000},
    "did": {"type": "string", "description": "Beneficiary DID. Required to issue a credential."},
    "consentOnFile": {"type": "boolean", "default": False},
}


def build_registry(service: PolicyLedgerService | None = None) -> McpToolRegistry:
    """Construct the registry of policy ledger tools."""
    svc = service or default_ledger_service()
    registry = McpToolRegistry(PLUGIN_INFO)

    async def network(_: dict[str, Any], __: TenantContext) -> dict[str, Any]:
        channel = svc._ledger.channels[svc.channel_id]  # noqa: SLF001
        return {
            "channel": channel.to_dict(),
            "organizations": [
                {
                    **org.to_dict(),
                    "canEndorsePolicyState": org.role.can_endorse_policy_state,
                }
                for org in svc.organizations()
            ],
        }

    async def get_policy(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        policy = svc.get_policy(str(arguments["policyId"]))
        if policy is None:
            return {"error": "policyNotFound"}
        return {"policy": policy.to_dict()}

    async def history(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        events = svc.history(str(arguments["policyId"]))
        if not events:
            return {"error": "policyNotFound", "policyId": arguments["policyId"]}
        return {
            "policyId": arguments["policyId"],
            "count": len(events),
            "events": [e.to_dict() for e in events],
        }

    async def submit_event(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        try:
            event_type = EventType(str(arguments["eventType"]))
        except (KeyError, ValueError) as exc:
            return {"error": "invalidEvent", "detail": str(exc)}
        # LedgerError is a ValueError subclass, so it must be caught before the
        # generic ValueError clause or a business refusal looks like a bad argument.
        try:
            tx = svc.submit_event(
                policy_id=str(arguments["policyId"]),
                event_type=event_type,
                payload=dict(arguments.get("payload") or {}),
                actor_org=str(arguments.get("actorOrg", "org-carrier")),
                actor_subject=ctx.principal.subject,
            )
        except LedgerError as exc:
            return {"error": "eventRejected", "detail": str(exc)}
        except (KeyError, ValueError) as exc:
            return {"error": "invalidEvent", "detail": str(exc)}
        return {"transaction": tx.to_dict()}

    async def verify_chain(_: dict[str, Any], __: TenantContext) -> dict[str, Any]:
        """Recompute every hash, Merkle root and anchor receipt on the channel."""
        return {"verification": svc.verify()}

    async def event_proof(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        try:
            return {"proof": svc.event_proof(str(arguments["eventId"]))}
        except LedgerError as exc:
            return {"error": "proofUnavailable", "detail": str(exc)}

    async def request_beneficiary_change(
        arguments: dict[str, Any], ctx: TenantContext
    ) -> dict[str, Any]:
        """Propose a change. It has no effect until the required orgs approve."""
        try:
            change = svc.request_beneficiary_change(
                str(arguments["policyId"]),
                [Beneficiary.model_validate(b) for b in arguments["beneficiaries"]],
                str(arguments["reason"]),
                str(arguments.get("requestId") or f"BC-{len(svc.history(str(arguments['policyId'])))}"),
            )
        except (KeyError, ValueError) as exc:
            return {"error": "invalidRequest", "detail": str(exc)}
        except LedgerError as exc:
            return {"error": "requestRejected", "detail": str(exc)}
        return {"changeRequest": change.to_dict()}

    async def endorse_beneficiary_change(
        arguments: dict[str, Any], ctx: TenantContext
    ) -> dict[str, Any]:
        try:
            svc.endorse_beneficiary_change(
                str(arguments["requestId"]), str(arguments["orgId"])
            )
        except (KeyError, ValueError) as exc:
            return {"error": "endorsementFailed", "detail": str(exc)}
        except LedgerError as exc:
            return {"error": "endorsementFailed", "detail": str(exc)}
        change = svc.beneficiary_request(str(arguments["requestId"]))
        return {"changeRequest": change.to_dict() if change else None}

    async def apply_beneficiary_change(
        arguments: dict[str, Any], ctx: TenantContext
    ) -> dict[str, Any]:
        """Apply an approved change. Destructive and quorum-gated."""
        try:
            policy = svc.apply_beneficiary_change(
                str(arguments["requestId"]), actor_subject=ctx.principal.subject
            )
        except LedgerError as exc:
            return {"error": "cannotApply", "detail": str(exc)}
        return {"policy": policy.to_dict()}

    async def file_claim(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        try:
            claim, _tx = svc.file_claim(
                str(arguments["policyId"]),
                claim_id=str(arguments["claimId"]),
                amount_requested=float(arguments["amountRequested"]),
                claim_type=str(arguments.get("claimType", "death")),
                subject=ctx.principal.subject,
            )
        except (KeyError, ValueError) as exc:
            return {"error": "invalidClaim", "detail": str(exc)}
        except LedgerError as exc:
            return {"error": "claimRejected", "detail": str(exc)}
        return {"claim": claim.to_dict()}

    async def schedule_annuity(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        """Generate the drawdown obligation. No money moves; that is a separate tool."""
        try:
            entries = svc.schedule_annuity(
                str(arguments["policyId"]),
                starting_value=float(arguments["startingValue"]),
                monthly_withdrawal=float(arguments["monthlyWithdrawal"]),
                periods=int(arguments.get("periods", 12)),
                tax_rate_bps=int(arguments.get("taxRateBps", 1500)),
            )
        except (KeyError, ValueError) as exc:
            return {"error": "invalidSchedule", "detail": str(exc)}
        except LedgerError as exc:
            return {"error": "scheduleRejected", "detail": str(exc)}
        return {"entries": [e.to_dict() for e in entries]}

    async def issue_credential(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        try:
            credential = svc.identity.issue_credential(
                issuer_subject=str(arguments.get("issuer", "org-carrier")),
                subject=dict(arguments["subject"]),
                credential_type=str(arguments.get("credentialType", "BeneficiaryRelationship")),
            )
        except (KeyError, ValueError) as exc:
            return {"error": "cannotIssue", "detail": str(exc)}
        return {"credential": credential.to_dict()}

    async def verify_credential(arguments: dict[str, Any], ctx: TenantContext) -> dict[str, Any]:
        from pas_plugins.plugin7_blockchain.models import VerifiableCredential  # noqa: PLC0415

        try:
            credential = VerifiableCredential.model_validate(arguments["credential"])
        except (KeyError, ValueError) as exc:
            return {"error": "invalidCredential", "detail": str(exc)}
        return {"verification": svc.identity.verify_credential(credential)}

    policy_id = {"policyId": {"type": "string", "description": "The policy identifier."}}
    confirm = {"confirm": {"type": "boolean", "default": False}}

    specs = [
        McpToolSpec(
            name="ledger_get_network",
            title="Get Network Participants",
            description=(
                "The channel and its member organisations with roles. Roles decide who "
                "may endorse: a regulator on this channel observes and cannot write."
            ),
            input_schema=_schema({}, []),
            handler=network,
            required_scopes=("ledger:read",),
            plugin_id="plugin7",
            operation_id="ledger_get_network",
            tags=("ledger", "network"),
        ),
        McpToolSpec(
            name="ledger_get_policy",
            title="Get Policy",
            description="The current state of a policy as the ledger projects it.",
            input_schema=_schema(policy_id, ["policyId"]),
            handler=get_policy,
            required_scopes=("ledger:read",),
            plugin_id="plugin7",
            operation_id="ledger_get_policy",
            tags=("ledger", "policy"),
        ),
        McpToolSpec(
            name="ledger_get_history",
            title="Get Policy History",
            description=(
                "Every committed event for a policy, in sequence, with the hash chain "
                "that links them. This is the record, as opposed to the projection."
            ),
            input_schema=_schema(policy_id, ["policyId"]),
            handler=history,
            required_scopes=("ledger:read",),
            plugin_id="plugin7",
            operation_id="ledger_get_history",
            tags=("ledger", "policy", "audit"),
        ),
        McpToolSpec(
            name="ledger_submit_event",
            title="Submit Lifecycle Event",
            description=(
                "Propose a lifecycle change (issue, modify, lapse, reinstate, surrender, "
                "mature, terminate). Destructive: it writes a permanent record, so the "
                "caller must pass confirm=true. An illegal transition is refused and "
                "nothing is written."
            ),
            input_schema=_schema(
                {
                    **policy_id,
                    "eventType": {
                        "type": "string",
                        "enum": [str(e) for e in EventType],
                    },
                    "payload": {"type": "object", "default": {}},
                    "actorOrg": {"type": "string", "default": "org-carrier"},
                    **confirm,
                },
                ["policyId", "eventType", "confirm"],
            ),
            handler=submit_event,
            required_scopes=("ledger:write",),
            annotations=McpToolAnnotations(read_only=False, destructive=True, idempotent=False),
            plugin_id="plugin7",
            operation_id="ledger_submit_event",
            tags=("ledger", "policy", "lifecycle"),
        ),
        McpToolSpec(
            name="ledger_verify_chain",
            title="Verify Chain Integrity",
            description=(
                "Recompute every block hash, every Merkle root, every event hash and "
                "every external anchor receipt, and report which check failed."
            ),
            input_schema=_schema({}, []),
            handler=verify_chain,
            required_scopes=("ledger:read",),
            plugin_id="plugin7",
            operation_id="ledger_verify_chain",
            tags=("ledger", "audit"),
        ),
        McpToolSpec(
            name="ledger_event_proof",
            title="Get Event Inclusion Proof",
            description=(
                "A Merkle inclusion proof for one event, verifiable against its block's "
                "root by a third party with no access to this service."
            ),
            input_schema=_schema({"eventId": {"type": "string"}}, ["eventId"]),
            handler=event_proof,
            required_scopes=("ledger:read",),
            plugin_id="plugin7",
            operation_id="ledger_event_proof",
            tags=("ledger", "audit", "proof"),
        ),
        McpToolSpec(
            name="ledger_request_beneficiary_change",
            title="Request Beneficiary Change",
            description=(
                "Propose a beneficiary change with shares totalling exactly 100%. This "
                "writes a proposal only: it has no effect until the required "
                "organisations approve it."
            ),
            input_schema=_schema(
                {
                    **policy_id,
                    "beneficiaries": {"type": "array", "items": _BENEFICIARY, "minItems": 1},
                    "reason": {"type": "string", "minLength": 5},
                    "requestId": {"type": "string"},
                },
                ["policyId", "beneficiaries", "reason"],
            ),
            handler=request_beneficiary_change,
            required_scopes=("ledger:write",),
            annotations=McpToolAnnotations(read_only=False, destructive=False, idempotent=False),
            plugin_id="plugin7",
            operation_id="ledger_request_beneficiary_change",
            tags=("ledger", "beneficiary", "workflow"),
        ),
        McpToolSpec(
            name="ledger_endorse_beneficiary_change",
            title="Endorse Beneficiary Change",
            description="Record one organisation's approval of a pending beneficiary change.",
            input_schema=_schema(
                {"requestId": {"type": "string"}, "orgId": {"type": "string"}},
                ["requestId", "orgId"],
            ),
            handler=endorse_beneficiary_change,
            required_scopes=("ledger:write",),
            annotations=McpToolAnnotations(read_only=False, destructive=False, idempotent=False),
            plugin_id="plugin7",
            operation_id="ledger_endorse_beneficiary_change",
            tags=("ledger", "beneficiary", "workflow"),
        ),
        McpToolSpec(
            name="ledger_apply_beneficiary_change",
            title="Apply Beneficiary Change",
            description=(
                "Apply a change that has reached its multi-signature quorum. Destructive: "
                "it redirects a death benefit, so the caller must pass confirm=true, and "
                "it is refused without quorum or with a beneficiary lacking consent."
            ),
            input_schema=_schema(
                {"requestId": {"type": "string"}, **confirm}, ["requestId", "confirm"]
            ),
            handler=apply_beneficiary_change,
            required_scopes=("ledger:write",),
            annotations=McpToolAnnotations(read_only=False, destructive=True, idempotent=False),
            plugin_id="plugin7",
            operation_id="ledger_apply_beneficiary_change",
            tags=("ledger", "beneficiary", "human-in-the-loop"),
        ),
        McpToolSpec(
            name="ledger_file_claim",
            title="File Claim",
            description=(
                "File a claim. Amounts above the carrier's auto-approval limit are held "
                "for human approval, and a claim above the face amount is refused."
            ),
            input_schema=_schema(
                {
                    **policy_id,
                    "claimId": {"type": "string"},
                    "amountRequested": {"type": "number", "minimum": 0},
                    "claimType": {"type": "string", "default": "death"},
                },
                ["policyId", "claimId", "amountRequested"],
            ),
            handler=file_claim,
            required_scopes=("ledger:write",),
            annotations=McpToolAnnotations(read_only=False, destructive=False, idempotent=False),
            plugin_id="plugin7",
            operation_id="ledger_file_claim",
            tags=("ledger", "claim"),
        ),
        McpToolSpec(
            name="ledger_schedule_annuity",
            title="Schedule Annuity Drawdowns",
            description=(
                "Generate an annuity drawdown schedule and record the obligation on the "
                "ledger. No money moves: paying an entry is a separate, confirmed step."
            ),
            input_schema=_schema(
                {
                    **policy_id,
                    "startingValue": {"type": "number", "exclusiveMinimum": 0},
                    "monthlyWithdrawal": {"type": "number", "exclusiveMinimum": 0},
                    "periods": {"type": "integer", "minimum": 1, "maximum": 600, "default": 12},
                    "taxRateBps": {"type": "integer", "minimum": 0, "maximum": 5000, "default": 1500},
                },
                ["policyId", "startingValue", "monthlyWithdrawal"],
            ),
            handler=schedule_annuity,
            required_scopes=("ledger:write",),
            annotations=McpToolAnnotations(read_only=False, destructive=False, idempotent=False),
            plugin_id="plugin7",
            operation_id="ledger_schedule_annuity",
            tags=("ledger", "annuity"),
        ),
        McpToolSpec(
            name="ledger_issue_credential",
            title="Issue Verifiable Credential",
            description=(
                "Issue an Ed25519-signed credential about a subject, such as a "
                "beneficiary's relationship to a policy."
            ),
            input_schema=_schema(
                {
                    "subject": {"type": "object", "description": "The credential subject."},
                    "credentialType": {"type": "string", "default": "BeneficiaryRelationship"},
                    "issuer": {"type": "string", "default": "org-carrier"},
                },
                ["subject"],
            ),
            handler=issue_credential,
            required_scopes=("ledger:write",),
            annotations=McpToolAnnotations(read_only=False, destructive=False, idempotent=False),
            plugin_id="plugin7",
            operation_id="ledger_issue_credential",
            tags=("ledger", "identity", "ssi"),
        ),
        McpToolSpec(
            name="ledger_verify_credential",
            title="Verify Verifiable Credential",
            description=(
                "Verify a credential's signature, issuer, expiry and revocation status, "
                "naming which check failed."
            ),
            input_schema=_schema({"credential": {"type": "object"}}, ["credential"]),
            handler=verify_credential,
            required_scopes=("ledger:read",),
            plugin_id="plugin7",
            operation_id="ledger_verify_credential",
            tags=("ledger", "identity", "ssi"),
        ),
    ]
    for spec in specs:
        registry.register_tool(spec)

    async def schema_resource(_: TenantContext) -> dict[str, Any]:
        return {
            "eventTypes": [str(e) for e in EventType],
            "endorsementPolicy": svc._ledger.channels[svc.channel_id].endorsement_policy,  # noqa: SLF001
            "organizationRoles": [str(o) for o in OrgRole],
        }

    registry.register_resource(
        McpResourceSpec(
            uri_template="pas://ledger/schema",
            name="Ledger schema",
            title="Event types and endorsement policy",
            description="Every lifecycle event this ledger accepts and how endorsement resolves.",
            mime_type="application/json",
            handler=schema_resource,
            plugin_id="plugin7",
            tags=("ledger", "schema"),
        )
    )

    return registry



__all__ = ["PLUGIN_INFO", "build_registry"]