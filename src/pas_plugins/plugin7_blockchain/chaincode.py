"""Chaincode: the policy lifecycle state machine and its business rules.

In Fabric terms this is what would compile to chaincode. It is deliberately a pure
function of (current state, arguments) with no I/O, because that is the property
that makes chaincode testable and that makes every peer reach the same answer.

Three rules do most of the work:

1. **Illegal transitions are rejected, not recorded.** A ledger that accepts
   `reinstate` on a terminated policy is an append-only log of nonsense, and worse,
   it looks authoritative.
2. **Every state change is an event**, never an update. The `Policy` projection in
   the service is derived; the events are the record.
3. **Beneficiary changes need a quorum.** No single party - including the carrier -
   can redirect a death benefit alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from pas_plugins.plugin7_blockchain.ledger import Ledger, sha256_hex
from pas_plugins.plugin7_blockchain.models import (
    AnnuityScheduleEntry,
    Beneficiary,
    BeneficiaryChangeRequest,
    Claim,
    EventType,
    Policy,
    PolicyEvent,
    PolicyStatus,
    PolicyToken,
    TransferStatus,
)
from pas_plugins.plugin7_blockchain.settings import Plugin7Settings

# Which event types move the policy into a new status.
_STATUS_AFTER: dict[EventType, PolicyStatus | None] = {
    EventType.ISSUE: PolicyStatus.IN_FORCE,
    EventType.MODIFY: None,
    EventType.BENEFICIARY_CHANGE_REQUESTED: None,
    EventType.BENEFICIARY_CHANGE_APPROVED: None,
    EventType.LAPSE: PolicyStatus.LAPSED,
    EventType.REINSTATE: PolicyStatus.REINSTATED,
    EventType.SURRENDER: PolicyStatus.SURRENDERED,
    EventType.MATURE: PolicyStatus.MATURED,
    EventType.TERMINATE: PolicyStatus.TERMINATED,
    EventType.CLAIM_FILED: None,
    EventType.CLAIM_APPROVED: None,
    EventType.CLAIM_PAID: None,
    EventType.CLAIM_DECLINED: None,
    EventType.ANNUITY_PAYOUT: None,
    EventType.ANNUITY_SCHEDULED: None,
    EventType.TOKEN_ISSUED: None,
    EventType.TOKEN_TRANSFERRED: None,
    EventType.PORTABILITY_EXPORT: None,
    EventType.PORTABILITY_IMPORT: None,
}

_LEGAL_FROM: dict[EventType, set[PolicyStatus]] = {
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
    EventType.CLAIM_PAID: {
        PolicyStatus.IN_FORCE,
        PolicyStatus.REINSTATED,
        PolicyStatus.LAPSED,
        PolicyStatus.SURRENDERED,
    },
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
    EventType.PORTABILITY_EXPORT: {
        PolicyStatus.IN_FORCE,
        PolicyStatus.REINSTATED,
        PolicyStatus.LAPSED,
    },
    EventType.PORTABILITY_IMPORT: {PolicyStatus.PENDING},
}

# Ordinary annuity withdrawal rates, used to build a default schedule.
DEFAULT_ANNUITY_GROSS_RATE_BPS = 500
DEFAULT_ANNUITY_TAX_BPS = 1500


class ChaincodeError(ValueError):
    """A business-rule refusal. Chaincode raises it; the ledger never records it."""


@dataclass(slots=True)
class Chaincode:
    """Pure lifecycle rules over a policy projection."""

    settings: Plugin7Settings = field(default_factory=Plugin7Settings)

    # -- validation --------------------------------------------------------

    def validate_transition(self, current: PolicyStatus, event: EventType) -> None:
        """Raise if an event is not legal from the current status."""
        allowed = _LEGAL_FROM[event]
        if current not in allowed:
            msg = (
                f"'{event}' is not permitted from status '{current}'; "
                f"permitted from {sorted(str(s) for s in allowed)}"
            )
            raise ChaincodeError(msg)

    def next_status(self, current: PolicyStatus, event: EventType) -> PolicyStatus:
        """The status a policy holds once an event is applied."""
        self.validate_transition(current, event)
        return _STATUS_AFTER[event] or current

    def status_for(self, policy: Policy, event: EventType) -> PolicyStatus:
        return self.next_status(policy.status, event)

    # -- claims ------------------------------------------------------------

    def assess_claim(self, policy: Policy, claim: Claim) -> Claim:
        """Decide whether a claim needs human approval, and cap what is payable.

        The cap matters: a claim for more than the face amount is either a mistake
        or an attempt, and paying either without a human is the wrong default.
        """
        if claim.amount_requested > policy.face_amount:
            msg = (
                f"claim of {claim.amount_requested:,.0f} exceeds the face amount of "
                f"{policy.face_amount:,.0f}"
            )
            raise ChaincodeError(msg)
        if not policy.status.is_cover_in_force:
            msg = f"policy is '{policy.status}'; cover is not payable in that state"
            raise ChaincodeError(msg)

        claim.requires_manual_approval = (
            claim.amount_requested >= self.settings.require_claim_approval_above
        )
        if not claim.requires_manual_approval:
            # Within the auto-approval limit the full requested amount is payable.
            claim.amount_approved = claim.amount_requested
        return claim

    # -- beneficiaries -----------------------------------------------------

    def build_beneficiary_request(
        self,
        policy_id: str,
        beneficiaries: list[Beneficiary],
        reason: str,
        request_id: str,
    ) -> BeneficiaryChangeRequest:
        """Create a change request. Pydantic has already checked the shares total."""
        return BeneficiaryChangeRequest(
            request_id=request_id,
            policy_id=policy_id,
            proposed_beneficiaries=beneficiaries,
            reason=reason,
            required_signatories=self.settings.beneficiary_signatories_required,
        )

    def apply_beneficiary_request(
        self, policy: Policy, request: BeneficiaryChangeRequest
    ) -> Policy:
        """Apply a change that has reached quorum. Raises if it has not."""
        if request.policy_id != policy.policy_id:
            msg = f"request {request.request_id} is for policy {request.policy_id}, not {policy.policy_id}"
            raise ChaincodeError(msg)
        if request.applied:
            msg = f"request {request.request_id} has already been applied"
            raise ChaincodeError(msg)
        if not request.has_quorum:
            msg = (
                f"request {request.request_id} has {len(request.endorsing_orgs)} of "
                f"{request.required_signatories} required endorsements"
            )
            raise ChaincodeError(msg)
        unconsented = [b.full_name for b in request.proposed_beneficiaries if not b.consent_on_file]
        if unconsented:
            msg = (
                f"beneficiaries without consent on file cannot be applied: {unconsented}. "
                "Naming somebody without their consent is not permitted."
            )
            raise ChaincodeError(msg)
        request.applied = True
        policy.beneficiaries = list(request.proposed_beneficiaries)
        return policy

    # -- annuities ---------------------------------------------------------

    def build_annuity_schedule(
        self,
        policy_id: str,
        *,
        starting_value: float,
        monthly_withdrawal: float,
        periods: int = 60,
        tax_rate_bps: int = DEFAULT_ANNUITY_TAX_BPS,
        first_payment: date | None = None,
    ) -> list[AnnuityScheduleEntry]:
        """Build a drawdown schedule whose every line will become an event.

        The schedule is generated up front and *then* paid, so the obligation is
        visible in the record before any money moves. That ordering is the point:
        a payment that appears without a prior schedule cannot be audited.
        """
        if starting_value <= 0:
            msg = "annuity value must be positive to build a schedule"
            raise ChaincodeError(msg)
        if monthly_withdrawal <= 0:
            msg = "annuity withdrawal must be positive"
            raise ChaincodeError(msg)
        if periods <= 0:
            msg = "annuity schedule must have at least one period"
            raise ChaincodeError(msg)

        start = first_payment or (date.today() + timedelta(days=30))
        entries: list[AnnuityScheduleEntry] = []
        for index in range(periods):
            due = start + timedelta(days=30 * index)
            gross = round(monthly_withdrawal, 2)
            tax = round(gross * tax_rate_bps / 10_000.0, 2)
            entries.append(
                AnnuityScheduleEntry(
                    entry_id=f"ANN-{policy_id}-{index + 1:04d}",
                    policy_id=policy_id,
                    scheduled_for=due,
                    gross_withdrawal=gross,
                    tax_withholding=tax,
                    net_paid=round(gross - tax, 2),
                    status=TransferStatus.PENDING,
                )
            )
        return entries

    def total_scheduled(self, entries: list[AnnuityScheduleEntry]) -> float:
        return round(sum(e.net_paid for e in entries), 2)

    # -- tokens ------------------------------------------------------------

    def mint_token(
        self,
        policy: Policy,
        units: int,
        unit_value: float,
        owner_subject: str,
        owner_did: str | None,
        token_id: str,
    ) -> PolicyToken:
        """Mint a transferable interest in a policy's surrender value."""
        if policy.status not in {
            PolicyStatus.IN_FORCE,
            PolicyStatus.REINSTATED,
            PolicyStatus.SURRENDERED,
        }:
            msg = f"a policy in status '{policy.status}' cannot be tokenised"
            raise ChaincodeError(msg)
        if units <= 0:
            msg = "token units must be positive"
            raise ChaincodeError(msg)
        if unit_value <= 0:
            msg = "token unit value must be positive"
            raise ChaincodeError(msg)
        if units > self.settings.token_max_supply:
            msg = f"units {units} exceed the maximum supply of {self.settings.token_max_supply}"
            raise ChaincodeError(msg)
        if owner_did is None:
            msg = "a token holder must have a DID; unidentified holders cannot be KYC'd"
            raise ChaincodeError(msg)
        return PolicyToken(
            token_id=token_id,
            policy_id=policy.policy_id,
            units=units,
            unit_value=unit_value,
            owner_subject=owner_subject,
            owner_did=owner_did,
        )

    def transfer_token(
        self, token: PolicyToken, units: int, transferee_subject: str, transferee_did: str | None
    ) -> tuple[PolicyToken, float]:
        """Transfer units, returning the new token and the fee taken.

        The transferee must have a DID. That is the whole reason this plugin has
        an identity layer: a transferee who cannot be identified cannot be
        screened, and an unscreenable holder is not something to create.
        """
        if units <= 0:
            msg = "transfer units must be positive"
            raise ChaincodeError(msg)
        if units > token.units:
            msg = f"cannot transfer {units} units; the token holds {token.units}"
            raise ChaincodeError(msg)
        if transferee_did is None:
            msg = "a transferee must have a DID; unidentified transferees cannot be screened"
            raise ChaincodeError(msg)

        remaining = token.units - units
        fee = round(units * token.unit_value * self.settings.token_transfer_fee_bps / 10_000.0, 2)
        token.owner_subject = transferee_subject
        token.owner_did = transferee_did
        token.units = remaining
        return token, fee


def build_event(
    *,
    policy: Policy,
    event_type: EventType,
    payload: dict[str, Any],
    actor_org: str,
    actor_subject: str,
    tx_id: str,
    previous_event_hash: str,
    event_id: str,
) -> PolicyEvent:
    """Construct a hash-chained event. Used by the service when committing."""
    status_after = _STATUS_AFTER[event_type] or policy.status
    event = PolicyEvent(
        event_id=event_id,
        policy_id=policy.policy_id,
        sequence=policy.version,
        event_type=event_type,
        status_before=policy.status,
        status_after=status_after,
        payload=payload,
        payload_hash=sha256_hex(payload),
        previous_event_hash=previous_event_hash,
        tx_id=tx_id,
        actor_org=actor_org,
        actor_subject=actor_subject,
    )
    event.event_hash = Ledger.event_hash_for(event)
    return event


def chaincode_hash(channel_id: str, function: str, arguments: dict[str, Any]) -> str:
    """Deterministic hash of a chaincode invocation.

    Fabric identifies a transaction by its payload, and so does this: the same
    invocation always produces the same payload hash, which is what makes
    endorsements portable and the chain reproducible.
    """
    return sha256_hex({"channel": channel_id, "function": function, "args": arguments})


__all__ = [
    "Chaincode",
    "ChaincodeError",
    "DEFAULT_ANNUITY_GROSS_RATE_BPS",
    "DEFAULT_ANNUITY_TAX_BPS",
    "build_event",
    "chaincode_hash",
]