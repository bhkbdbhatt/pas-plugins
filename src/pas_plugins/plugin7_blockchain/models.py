"""Policy lifecycle domain models.

Three groups: the network (orgs, channels, blocks, transactions), the policy
domain (policies, events, beneficiaries, claims, annuities), and identity (DIDs,
verifiable credentials).

The vocabulary is Hyperledger Fabric's where that is the precise term -
`endorsement`, `channel`, `block`, `transaction`, `chaincode` - because a
deployment target of Fabric 2.x means the model should speak Fabric's language
rather than a generic one a Fabric operator would have to translate from.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pas_core.pas.vendors import to_camel

# Mirrors ledger.GENESIS_HASH. Declared here rather than imported because ledger
# imports this module, and a back-reference would be a circular import for the
# sake of one constant.
GENESIS_HASH = "0" * 64


class Model(BaseModel):
    """Base model: snake_case in Python, camelCase on the wire, strict on input."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        alias_generator=to_camel,
        populate_by_name=True,
    )


class OrgRole(StrEnum):
    """Why an organisation is on the network.

    The endorsement rules follow from these roles, not from a flat membership list:
    the carrier writes policies, the regulator observes, a beneficiary attests to
    their own identity, and a service provider performs work on instruction.
    """

    CARRIER = "carrier"
    REGULATOR = "regulator"
    BENEFICIARY = "beneficiary"
    SERVICE_PROVIDER = "serviceProvider"

    @property
    def can_endorse_policy_state(self) -> bool:
        return self is OrgRole.CARRIER

    @property
    def can_attest_identity(self) -> bool:
        return self in {OrgRole.BENEFICIARY, OrgRole.CARRIER, OrgRole.REGULATOR}


class TransactionStatus(StrEnum):
    VALID = "valid"
    """Endorsed by every required organisation; committed to the ledger."""
    PENDING_ENDORSEMENT = "pendingEndorsement"
    """Not all required endorsements have arrived; not yet in a block."""
    REJECTED = "rejected"
    INVALID = "invalid"

    @property
    def is_committed(self) -> bool:
        return self is TransactionStatus.VALID


class PolicyStatus(StrEnum):
    """The lifecycle states a policy can legally be in.

    `IN_FORCE` and `LAPSED` are the only states from which money can be paid;
    `TERMINATED` and `EXHAUSTED` are terminal.
    """

    PENDING = "pending"
    IN_FORCE = "inForce"
    LAPSED = "lapsed"
    REINSTATED = "reinstated"
    SURRENDERED = "surrendered"
    MATURED = "matured"
    TERMINATED = "terminated"
    EXPIRED = "expired"
    DECLINED = "declined"

    @property
    def is_terminal(self) -> bool:
        return self in {PolicyStatus.TERMINATED, PolicyStatus.DECLINED, PolicyStatus.EXPIRED}

    @property
    def is_cover_in_force(self) -> bool:
        """Whether a death benefit is payable in this state.

        `LAPSED` is deliberately excluded. A lapsed policy keeps its history and can
        often be reinstated, but it pays nothing while lapsed - treating it as cover
        in force would let a claim be approved against a policy that has no cover.
        """
        return self in {
            PolicyStatus.IN_FORCE,
            PolicyStatus.REINSTATED,
            PolicyStatus.SURRENDERED,
        }


class EventType(StrEnum):
    """The lifecycle events chaincode accepts."""

    ISSUE = "issue"
    MODIFY = "modify"
    BENEFICIARY_CHANGE_REQUESTED = "beneficiaryChangeRequested"
    BENEFICIARY_CHANGE_APPROVED = "beneficiaryChangeApproved"
    LAPSE = "lapse"
    REINSTATE = "reinstate"
    SURRENDER = "surrender"
    MATURE = "mature"
    TERMINATE = "terminate"
    CLAIM_FILED = "claimFiled"
    CLAIM_APPROVED = "claimApproved"
    CLAIM_PAID = "claimPaid"
    CLAIM_DECLINED = "claimDeclined"
    ANNUITY_PAYOUT = "annuityPayout"
    ANNUITY_SCHEDULED = "annuityScheduled"
    TOKEN_ISSUED = "tokenIssued"
    TOKEN_TRANSFERRED = "tokenTransferred"
    PORTABILITY_EXPORT = "portabilityExport"
    PORTABILITY_IMPORT = "portabilityImport"


# Legal state transitions. An event that is not permitted from the current state
# is rejected by chaincode rather than silently recorded - a ledger that accepts
# impossible transitions is an append-only log of nonsense.
_LEGAL_TRANSITIONS: dict[EventType, set[PolicyStatus]] = {
    EventType.ISSUE: {PolicyStatus.PENDING},
    EventType.MODIFY: {
        PolicyStatus.IN_FORCE,
        PolicyStatus.LAPSED,
        PolicyStatus.REINSTATED,
        PolicyStatus.SURRENDERED,
    },
    EventType.BENEFICIARY_CHANGE_REQUESTED: {
        PolicyStatus.IN_FORCE,
        PolicyStatus.REINSTATED,
        PolicyStatus.LAPSED,
    },
    EventType.BENEFICIARY_CHANGE_APPROVED: {
        PolicyStatus.IN_FORCE,
        PolicyStatus.REINSTATED,
        PolicyStatus.LAPSED,
    },
    EventType.LAPSE: {PolicyStatus.IN_FORCE, PolicyStatus.REINSTATED},
    EventType.REINSTATE: {PolicyStatus.LAPSED},
    EventType.SURRENDER: {PolicyStatus.IN_FORCE, PolicyStatus.REINSTATED, PolicyStatus.LAPSED},
    EventType.MATURE: {PolicyStatus.IN_FORCE, PolicyStatus.REINSTATED},
    EventType.TERMINATE: {PolicyStatus.IN_FORCE, PolicyStatus.LAPSED, PolicyStatus.REINSTATED},
    EventType.CLAIM_FILED: {
        PolicyStatus.IN_FORCE,
        PolicyStatus.REINSTATED,
        PolicyStatus.LAPSED,
        PolicyStatus.SURRENDERED,
    },
    EventType.CLAIM_APPROVED: {
        PolicyStatus.IN_FORCE,
        PolicyStatus.REINSTATED,
        PolicyStatus.LAPSED,
        PolicyStatus.SURRENDERED,
    },
    EventType.CLAIM_PAID: {PolicyStatus.IN_FORCE, PolicyStatus.REINSTATED, PolicyStatus.LAPSED},
    EventType.CLAIM_DECLINED: {
        PolicyStatus.IN_FORCE,
        PolicyStatus.REINSTATED,
        PolicyStatus.LAPSED,
        PolicyStatus.SURRENDERED,
    },
    EventType.ANNUITY_PAYOUT: {PolicyStatus.IN_FORCE, PolicyStatus.REINSTATED},
    EventType.ANNUITY_SCHEDULED: {PolicyStatus.IN_FORCE, PolicyStatus.REINSTATED},
    EventType.TOKEN_ISSUED: {
        PolicyStatus.IN_FORCE,
        PolicyStatus.REINSTATED,
        PolicyStatus.SURRENDERED,
    },
    EventType.TOKEN_TRANSFERRED: {
        PolicyStatus.IN_FORCE,
        PolicyStatus.REINSTATED,
        PolicyStatus.SURRENDERED,
    },
    EventType.PORTABILITY_EXPORT: {PolicyStatus.IN_FORCE, PolicyStatus.REINSTATED, PolicyStatus.LAPSED},
    EventType.PORTABILITY_IMPORT: {PolicyStatus.PENDING},
}


class ClaimStatus(StrEnum):
    FILED = "filed"
    UNDER_REVIEW = "underReview"
    APPROVED = "approved"
    PAID = "paid"
    DECLINED = "declined"

    @property
    def is_closed(self) -> bool:
        return self in {ClaimStatus.PAID, ClaimStatus.DECLINED}


class TransferStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"


class VerifiableCredentialStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"
    SUSPENDED = "suspended"


class ProofType(StrEnum):
    """Signature schemes the identity layer supports."""

    ED25519 = "Ed25519Signature2020"


class Organization(Model):
    """A member of the permissioned network."""

    org_id: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=2, max_length=160)
    role: OrgRole
    peer_endpoints: list[str] = Field(default_factory=list)
    msp_id: str = Field(default="", max_length=80, description="Fabric MSP identifier.")
    active: bool = True
    joined_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "orgId": self.org_id,
            "name": self.name,
            "role": str(self.role),
            "peerEndpoints": self.peer_endpoints,
            "mspId": self.msp_id,
            "active": self.active,
            "joinedAt": self.joined_at.isoformat(),
        }


class Channel(Model):
    """A private set of state shared only between its member organisations."""

    channel_id: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=2, max_length=80)
    member_orgs: list[str] = Field(default_factory=list)
    endorsement_policy: str = Field(
        default="MAJORITY",
        max_length=40,
        description="ALL, MAJORITY, ANY, or an explicit org list.",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "channelId": self.channel_id,
            "name": self.name,
            "memberOrgs": self.member_orgs,
            "endorsementPolicy": self.endorsement_policy,
            "createdAt": self.created_at.isoformat(),
        }


class Endorsement(Model):
    """One organisation's signature on a transaction."""

    org_id: str
    peer_id: str = Field(default="", max_length=120)
    signature: str = Field(default="", max_length=256)
    endorsed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    signed_payload_hash: str = Field(default="", max_length=64)

    def to_dict(self) -> dict[str, Any]:
        return {
            "orgId": self.org_id,
            "peerId": self.peer_id,
            "signature": self.signature,
            "endorsedAt": self.endorsed_at.isoformat(),
            "signedPayloadHash": self.signed_payload_hash,
        }


class Transaction(Model):
    """A proposed change, and whether enough organisations have endorsed it."""

    tx_id: str = Field(min_length=4, max_length=80)
    channel_id: str
    chaincode: str = Field(default="policy-lifecycle", max_length=80)
    function: str = Field(min_length=2, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)
    payload_hash: str = Field(min_length=64, max_length=64)
    endorsements: list[Endorsement] = Field(default_factory=list)
    required_orgs: list[str] = Field(default_factory=list)
    status: TransactionStatus = TransactionStatus.PENDING_ENDORSEMENT
    block_number: int | None = Field(default=None, ge=0)
    submitted_by: str = Field(default="", max_length=128)
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    result: dict[str, Any] = Field(default_factory=dict)
    rejection_reason: str | None = Field(default=None, max_length=300)

    @property
    def endorsing_orgs(self) -> list[str]:
        """Distinct organisations that have endorsed."""
        return sorted({e.org_id for e in self.endorsements})

    @property
    def is_endorsed(self) -> bool:
        return set(self.required_orgs).issubset(set(self.endorsing_orgs))

    def to_dict(self) -> dict[str, Any]:
        return {
            "txId": self.tx_id,
            "channelId": self.channel_id,
            "chaincode": self.chaincode,
            "function": self.function,
            "arguments": self.arguments,
            "payloadHash": self.payload_hash,
            "endorsements": [e.to_dict() for e in self.endorsements],
            "endorsingOrgs": self.endorsing_orgs,
            "requiredOrgs": self.required_orgs,
            "status": str(self.status),
            "blockNumber": self.block_number,
            "submittedBy": self.submitted_by,
            "submittedAt": self.submitted_at.isoformat(),
            "result": self.result,
            "rejectionReason": self.rejection_reason,
            "isEndorsed": self.is_endorsed,
        }


class Block(Model):
    """A sealed batch of transactions, committed to its predecessor's hash."""

    block_number: int = Field(ge=0)
    channel_id: str
    previous_hash: str = Field(default="0" * 64, min_length=64, max_length=64)
    merkle_root: str = Field(min_length=64, max_length=64)
    data_hash: str = Field(min_length=64, max_length=64)
    block_hash: str = Field(min_length=64, max_length=64)
    tx_ids: list[str] = Field(default_factory=list)
    tx_count: int = Field(default=0, ge=0)
    sealed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    anchor_receipt: str | None = Field(default=None, max_length=200)

    def to_dict(self) -> dict[str, Any]:
        return {
            "blockNumber": self.block_number,
            "channelId": self.channel_id,
            "previousHash": self.previous_hash,
            "merkleRoot": self.merkle_root,
            "dataHash": self.data_hash,
            "blockHash": self.block_hash,
            "txIds": self.tx_ids,
            "txCount": self.tx_count,
            "sealedAt": self.sealed_at.isoformat(),
            "anchorReceipt": self.anchor_receipt,
        }


class PolicyEvent(Model):
    """A committed lifecycle event. Immutable once written."""

    event_id: str = Field(min_length=4, max_length=80)
    policy_id: str = Field(min_length=2, max_length=64)
    sequence: int = Field(ge=0, description="Position in this policy's history.")
    event_type: EventType
    status_before: PolicyStatus
    status_after: PolicyStatus
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_hash: str = Field(min_length=64, max_length=64)
    previous_event_hash: str = Field(default=GENESIS_HASH, min_length=64, max_length=64)
    event_hash: str = Field(
        default="",
        max_length=64,
        description="Computed over the event content after construction; never supplied by a caller.",
    )
    tx_id: str = Field(min_length=4, max_length=80)
    block_number: int | None = Field(default=None, ge=0)
    actor_org: str = Field(default="", max_length=64)
    actor_subject: str = Field(default="", max_length=160)
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "eventId": self.event_id,
            "policyId": self.policy_id,
            "sequence": self.sequence,
            "eventType": str(self.event_type),
            "statusBefore": str(self.status_before),
            "statusAfter": str(self.status_after),
            "payload": self.payload,
            "payloadHash": self.payload_hash,
            "previousEventHash": self.previous_event_hash,
            "eventHash": self.event_hash,
            "txId": self.tx_id,
            "blockNumber": self.block_number,
            "actorOrg": self.actor_org,
            "actorSubject": self.actor_subject,
            "recordedAt": self.recorded_at.isoformat(),
        }


class Beneficiary(Model):
    """A named beneficiary, with their share and consent status."""

    beneficiary_id: str = Field(min_length=2, max_length=64)
    full_name: str = Field(min_length=2, max_length=160)
    relationship: str = Field(default="other", max_length=40)
    share_bps: int = Field(ge=0, le=10_000)
    did: str | None = Field(default=None, max_length=200, description="Beneficiary DID.")
    verified_identity: bool = False
    consent_on_file: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "beneficiaryId": self.beneficiary_id,
            "fullName": self.full_name,
            "relationship": self.relationship,
            "shareBps": self.share_bps,
            "did": self.did,
            "verifiedIdentity": self.verified_identity,
            "consentOnFile": self.consent_on_file,
        }


class BeneficiaryChangeRequest(Model):
    """A proposed beneficiary change awaiting multi-signature approval."""

    request_id: str = Field(min_length=2, max_length=64)
    policy_id: str
    proposed_beneficiaries: list[Beneficiary] = Field(min_length=1)
    reason: str = Field(min_length=5, max_length=400)
    endorsements: list[Endorsement] = Field(default_factory=list)
    required_signatories: int = Field(ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    applied: bool = False

    @field_validator("proposed_beneficiaries")
    @classmethod
    def _shares_must_total_10000(cls, value: list[Beneficiary]) -> list[Beneficiary]:
        total = sum(b.share_bps for b in value)
        if total != 10_000:
            msg = f"beneficiary shares total {total}bp; they must total exactly 10000bp"
            raise ValueError(msg)
        return value

    @property
    def endorsing_orgs(self) -> list[str]:
        return sorted({e.org_id for e in self.endorsements})

    def unsigned_payload(self) -> dict[str, Any]:
        """The content endorsements sign.

        Excludes the endorsements themselves, so a signature covers what was
        proposed rather than who has already agreed - which is what makes the
        quorum count meaningful.
        """
        return {
            "requestId": self.request_id,
            "policyId": self.policy_id,
            "reason": self.reason,
            "requiredSignatories": self.required_signatories,
            "proposedBeneficiaries": [b.to_dict() for b in self.proposed_beneficiaries],
        }

    @property
    def has_quorum(self) -> bool:
        """Whether enough distinct signatories have approved."""
        return len(self.endorsing_orgs) >= self.required_signatories

    def to_dict(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "policyId": self.policy_id,
            "proposedBeneficiaries": [b.to_dict() for b in self.proposed_beneficiaries],
            "reason": self.reason,
            "endorsements": [e.to_dict() for e in self.endorsements],
            "endorsingOrgs": self.endorsing_orgs,
            "requiredSignatories": self.required_signatories,
            "hasQuorum": self.has_quorum,
            "createdAt": self.created_at.isoformat(),
            "applied": self.applied,
        }


class Claim(Model):
    """A claim against a policy, and where it is in the process."""

    claim_id: str = Field(min_length=2, max_length=64)
    policy_id: str
    claim_type: str = Field(default="death", max_length=40)
    amount_requested: float = Field(ge=0.0)
    amount_approved: float = Field(default=0.0, ge=0.0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    status: ClaimStatus = ClaimStatus.FILED
    filed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    requires_manual_approval: bool = False
    decision_reason: str | None = Field(default=None, max_length=300)

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _approval_within_request(self) -> Claim:
        if self.amount_approved > self.amount_requested:
            msg = (
                f"approved amount {self.amount_approved} exceeds the requested "
                f"{self.amount_requested}"
            )
            raise ValueError(msg)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "claimId": self.claim_id,
            "policyId": self.policy_id,
            "claimType": self.claim_type,
            "amountRequested": round(self.amount_requested, 2),
            "amountApproved": round(self.amount_approved, 2),
            "currency": self.currency,
            "status": str(self.status),
            "filedAt": self.filed_at.isoformat(),
            "requiresManualApproval": self.requires_manual_approval,
            "decisionReason": self.decision_reason,
        }


class AnnuityScheduleEntry(Model):
    """One scheduled annuity drawdown."""

    entry_id: str = Field(min_length=2, max_length=64)
    policy_id: str
    scheduled_for: date
    gross_withdrawal: float = Field(ge=0.0)
    tax_withholding: float = Field(default=0.0, ge=0.0)
    net_paid: float = Field(ge=0.0)
    status: TransferStatus = TransferStatus.PENDING

    @model_validator(mode="after")
    def _tax_within_gross(self) -> AnnuityScheduleEntry:
        if self.tax_withholding > self.gross_withdrawal:
            msg = "tax withholding cannot exceed the gross withdrawal"
            raise ValueError(msg)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "entryId": self.entry_id,
            "policyId": self.policy_id,
            "scheduledFor": self.scheduled_for.isoformat(),
            "grossWithdrawal": round(self.gross_withdrawal, 2),
            "taxWithholding": round(self.tax_withholding, 2),
            "netPaid": round(self.net_paid, 2),
            "status": str(self.status),
        }


class PolicyToken(Model):
    """A transferable interest in a policy's surrender value.

    Deliberately narrow. A token here is a *recorded, transferable economic
    interest*, not a security, and the transfer path requires KYC on the
    transferee. Anything broader would need real securities law, which this
    plugin does not have.
    """

    token_id: str = Field(min_length=2, max_length=64)
    policy_id: str
    token_class: str = Field(default="surrenderValue", max_length=40)
    units: int = Field(ge=0)
    unit_value: float = Field(ge=0.0)
    owner_subject: str = Field(min_length=2, max_length=160)
    owner_did: str | None = Field(default=None, max_length=200)
    status: TransferStatus = TransferStatus.COMPLETED
    minted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def total_value(self) -> float:
        return round(self.units * self.unit_value, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tokenId": self.token_id,
            "policyId": self.policy_id,
            "tokenClass": self.token_class,
            "units": self.units,
            "unitValue": self.unit_value,
            "totalValue": self.total_value,
            "ownerSubject": self.owner_subject,
            "ownerDid": self.owner_did,
            "status": str(self.status),
            "mintedAt": self.minted_at.isoformat(),
        }


class DidDocument(Model):
    """A W3C DID Document.

    `verificationMethod` holds Ed25519 public keys, which is what makes a
    verifiable credential checkable by anyone holding this document and no more.
    """

    model_config = ConfigDict(
        extra="allow",
        str_strip_whitespace=True,
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: str = Field(description="The DID, e.g. did:key:z6Mk...")
    controller: str = ""
    verification_method: list[dict[str, Any]] = Field(default_factory=list)
    authentication: list[str] = Field(default_factory=list)
    assertion_method: list[str] = Field(default_factory=list)
    service: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def key_ids(self) -> list[str]:
        return [vm["id"] for vm in self.verification_method if "id" in vm]

    def public_key_for(self, key_id: str) -> str | None:
        for method in self.verification_method:
            if method.get("id") == key_id:
                return method.get("publicKeyMultibase")
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "@context": ["https://www.w3.org/ns/did/v1"],
            "id": self.id,
            "controller": self.controller or self.id,
            "verificationMethod": self.verification_method,
            "authentication": self.authentication or self.key_ids,
            "assertionMethod": self.assertion_method or self.key_ids,
            "service": self.service,
            "createdAt": self.created_at.isoformat(),
        }


class VerifiableCredential(Model):
    """A signed assertion about a subject, verifiable without the issuer."""

    model_config = ConfigDict(
        extra="allow",
        str_strip_whitespace=True,
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: str = Field(description="Credential URI, e.g. urn:uuid:...")
    type: list[str] = Field(default_factory=lambda: ["VerifiableCredential"])
    issuer: str = Field(description="Issuer DID.")
    issuance_date: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expiration_date: datetime | None = None
    credential_subject: dict[str, Any] = Field(default_factory=dict)
    proof: dict[str, Any] = Field(default_factory=dict)
    status: VerifiableCredentialStatus = VerifiableCredentialStatus.ACTIVE

    @property
    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "issuer": self.issuer,
            "issuanceDate": self.issuance_date.isoformat(),
            "expirationDate": self.expiration_date.isoformat() if self.expiration_date else None,
            "credentialSubject": self.credential_subject,
        }

    @property
    def is_expired(self) -> bool:
        if self.expiration_date is None:
            return False
        return datetime.now(UTC) >= self.expiration_date

    @property
    def is_usable(self) -> bool:
        return self.status is VerifiableCredentialStatus.ACTIVE and not self.is_expired

    def to_dict(self) -> dict[str, Any]:
        return {
            "@context": ["https://www.w3.org/2018/credentials/v1"],
            "id": self.id,
            "type": self.type,
            "issuer": self.issuer,
            "issuanceDate": self.issuance_date.isoformat(),
            "expirationDate": self.expiration_date.isoformat() if self.expiration_date else None,
            "credentialSubject": self.credential_subject,
            "proof": self.proof,
            "status": str(self.status),
            "isUsable": self.is_usable,
        }


class Policy(Model):
    """The current state of a policy, as the ledger sees it.

    This is a projection. The events are the record; this is the convenience view
    the query layer serves, and it is always derivable from the events.
    """

    policy_id: str = Field(min_length=2, max_length=64)
    tenant_id: str = Field(min_length=2, max_length=64)
    policy_number: str = Field(min_length=2, max_length=64)
    holder_subject: str = Field(min_length=2, max_length=160)
    holder_did: str | None = Field(default=None, max_length=200)
    product_code: str = Field(min_length=1, max_length=60)
    status: PolicyStatus = PolicyStatus.PENDING
    face_amount: float = Field(ge=0.0)
    annual_premium: float = Field(default=0.0, ge=0.0)
    cash_value: float = Field(default=0.0, ge=0.0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    beneficiaries: list[Beneficiary] = Field(default_factory=list)
    event_count: int = Field(default=0, ge=0)
    version: int = Field(default=0, ge=0, description="Number of committed events.")
    issued_on: date | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @property
    def total_beneficiary_bps(self) -> int:
        return sum(b.share_bps for b in self.beneficiaries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "policyId": self.policy_id,
            "tenantId": self.tenant_id,
            "policyNumber": self.policy_number,
            "holderSubject": self.holder_subject,
            "holderDid": self.holder_did,
            "productCode": self.product_code,
            "status": str(self.status),
            "faceAmount": self.face_amount,
            "annualPremium": round(self.annual_premium, 2),
            "cashValue": round(self.cash_value, 2),
            "currency": self.currency,
            "beneficiaries": [b.to_dict() for b in self.beneficiaries],
            "eventCount": self.event_count,
            "version": self.version,
            "issuedOn": self.issued_on.isoformat() if self.issued_on else None,
            "createdAt": self.created_at.isoformat(),
            "updatedAt": self.updated_at.isoformat(),
        }


class ChainVerification(Model):
    """The result of auditing the whole chain."""

    channel_id: str
    blocks_checked: int = 0
    events_checked: int = 0
    chain_intact: bool = True
    merkle_roots_valid: bool = True
    event_hashes_valid: bool = True
    anchors_valid: bool = True
    problems: list[str] = Field(default_factory=list)
    verified_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_trustworthy(self) -> bool:
        return (
            self.chain_intact
            and self.merkle_roots_valid
            and self.event_hashes_valid
            and self.anchors_valid
            and not self.problems
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "channelId": self.channel_id,
            "blocksChecked": self.blocks_checked,
            "eventsChecked": self.events_checked,
            "chainIntact": self.chain_intact,
            "merkleRootsValid": self.merkle_roots_valid,
            "eventHashesValid": self.event_hashes_valid,
            "anchorsValid": self.anchors_valid,
            "problems": self.problems,
            "trustworthy": self.is_trustworthy,
            "verifiedAt": self.verified_at.isoformat(),
        }


class PortabilityPackage(Model):
    """A signed export of one policy's complete history.

    The receiving carrier can verify it without trusting the sender's ledger, which
    is the entire point of an export that carries its own proof.
    """

    package_id: str = Field(min_length=2, max_length=64)
    policy_id: str
    source_tenant: str
    source_system: str = Field(default="pas-plugin7", max_length=80)
    events: list[PolicyEvent] = Field(default_factory=list)
    blocks: list[Block] = Field(default_factory=list)
    export_hash: str = Field(min_length=64, max_length=64)
    merkle_root: str = Field(min_length=64, max_length=64)
    signature: str = Field(default="", max_length=256)
    signing_org: str = Field(default="", max_length=64)
    exported_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    exported_by: str = Field(default="", max_length=128)

    def to_dict(self) -> dict[str, Any]:
        return {
            "packageId": self.package_id,
            "policyId": self.policy_id,
            "sourceTenant": self.source_tenant,
            "sourceSystem": self.source_system,
            "exportHash": self.export_hash,
            "merkleRoot": self.merkle_root,
            "signature": self.signature,
            "signingOrg": self.signing_org,
            "exportedAt": self.exported_at.isoformat(),
            "exportedBy": self.exported_by,
            "blocks": [b.to_dict() for b in self.blocks],
            "events": [e.to_dict() for e in self.events],
        }


__all__ = [
    "AnnuityScheduleEntry",
    "Beneficiary",
    "BeneficiaryChangeRequest",
    "Block",
    "ChainVerification",
    "Channel",
    "Claim",
    "ClaimStatus",
    "DidDocument",
    "Endorsement",
    "EventType",
    "Model",
    "OrgRole",
    "Organization",
    "Policy",
    "PolicyEvent",
    "PolicyStatus",
    "PolicyToken",
    "PortabilityPackage",
    "ProofType",
    "Transaction",
    "TransactionStatus",
    "TransferStatus",
    "VerifiableCredential",
    "VerifiableCredentialStatus",
]