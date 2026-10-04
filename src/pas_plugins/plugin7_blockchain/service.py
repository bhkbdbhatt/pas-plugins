"""The policy ledger service: the surface a carrier or a PAS connector integrates.

Every write follows the same path, and the path *is* the permissioning:

    propose -> chaincode validates -> required orgs endorse -> block seals -> event

Nothing is written to the ledger without chaincode agreeing it is legal and
without the endorsement policy being satisfied. That is what separates this from a
database with an audit column.

The `submit_lifecycle_event` path is also what Plugin 1 calls: PAS state changes
arrive as chaincode invocations, get endorsed, and become immutable events.
"""

from __future__ import annotations

import secrets
from datetime import UTC, date, datetime
from typing import Any

from pas_plugins.plugin7_blockchain.chaincode import (
    Chaincode,
    ChaincodeError,
    build_event,
    chaincode_hash,
)
from pas_plugins.plugin7_blockchain.identity import IdentityService
from pas_plugins.plugin7_blockchain.ledger import (
    GENESIS_HASH,
    Ledger,
    merkle_proof,
    sha256_hex,
    verify_merkle_proof,
)
from pas_plugins.plugin7_blockchain.models import (
    AnnuityScheduleEntry,
    Beneficiary,
    BeneficiaryChangeRequest,
    Block,
    Channel,
    Claim,
    ClaimStatus,
    Endorsement,
    EventType,
    OrgRole,
    Organization,
    Policy,
    PolicyEvent,
    PolicyStatus,
    PolicyToken,
    PortabilityPackage,
    Transaction,
    TransactionStatus,
    TransferStatus,
)
from pas_plugins.plugin7_blockchain.settings import Plugin7Settings


class LedgerError(ValueError):
    """A ledger-level refusal."""


class PolicyLedgerService:
    """Policy lifecycle over a permissioned, hash-chained ledger."""

    def __init__(
        self,
        settings: Plugin7Settings | None = None,
        *,
        ledger: Ledger | None = None,
        chaincode: Chaincode | None = None,
        identity: IdentityService | None = None,
    ) -> None:
        self._settings = settings or Plugin7Settings()
        self._ledger = ledger or Ledger(settings=self._settings)
        self._chaincode = chaincode or Chaincode(self._settings)
        self._identity = identity or IdentityService(self._ledger.keystore)
        self._policies: dict[str, Policy] = {}
        self._claims: dict[str, Claim] = {}
        self._schedules: dict[str, list[AnnuityScheduleEntry]] = {}
        self._tokens: dict[str, PolicyToken] = {}
        self._beneficiary_requests: dict[str, BeneficiaryChangeRequest] = {}
        self._channel_id: str | None = None

    # -- network -----------------------------------------------------------

    def bootstrap_network(self, channel_id: str = "policy-lifecycle-channel") -> Channel:
        """Create the standard four-role network this plugin describes.

        The regulator is a channel member so it can observe, but its role does not
        permit it to endorse policy state. That asymmetry is deliberate: an
        observer that could also write is not an observer.
        """
        organisations = [
            Organization(
                org_id="org-carrier",
                name="Harbour Mutual Life",
                role=OrgRole.CARRIER,
                msp_id="HarbourMSP",
                peer_endpoints=["peer0.harbour.example:7051"],
            ),
            Organization(
                org_id="org-regulator",
                name="State Department of Insurance",
                role=OrgRole.REGULATOR,
                msp_id="RegulatorMSP",
                peer_endpoints=["peer0.regulator.example:7051"],
            ),
            Organization(
                org_id="org-beneficiary",
                name="Beneficiary Network",
                role=OrgRole.BENEFICIARY,
                msp_id="BeneficiaryMSP",
            ),
            Organization(
                org_id="org-service-provider",
                name="Northwind Policy Servicing",
                role=OrgRole.SERVICE_PROVIDER,
                msp_id="ServicingMSP",
            ),
        ]
        for org in organisations:
            self._ledger.add_organization(org)
            self._ledger.keystore.generate(org.org_id)

        channel = Channel(
            channel_id=channel_id,
            name=self._settings.channel_name,
            member_orgs=["org-carrier", "org-regulator", "org-service-provider"],
            endorsement_policy="ALL",
        )
        self._ledger.create_channel(channel)
        self._channel_id = channel.channel_id
        return channel

    @property
    def channel_id(self) -> str:
        if self._channel_id is None:
            self.bootstrap_network()
        assert self._channel_id is not None
        return self._channel_id

    def organizations(self) -> list[Organization]:
        return list(self._ledger.organizations.values())

    def blocks(self) -> list[Block]:
        return [b for b in self._ledger.blocks if b.channel_id == self.channel_id]

    def transactions(self) -> list[Transaction]:
        return [t for t in self._ledger.transactions.values() if t.channel_id == self.channel_id]

    # -- the write path ----------------------------------------------------

    def submit_event(
        self,
        *,
        policy_id: str,
        event_type: EventType,
        payload: dict[str, Any],
        actor_org: str,
        actor_subject: str,
        required_orgs: list[str] | None = None,
        endorse_now: bool = True,
    ) -> Transaction:
        """Propose, validate, endorse and commit one lifecycle event.

        The transaction is only committed once its endorsement policy is
        satisfied. A caller can propose without endorsing (`endorse_now=False`) to
        exercise the two-phase path.
        """
        policy = self._policies.get(policy_id)
        if policy is None:
            msg = f"unknown policy '{policy_id}'"
            raise LedgerError(msg)

        try:
            status_after = self._chaincode.status_for(policy, event_type)
        except ChaincodeError as exc:
            msg = str(exc)
            raise LedgerError(msg) from exc

        channel_id = self.channel_id
        required = required_orgs or self._eligible_endorsers(channel_id)
        tx = Transaction(
            tx_id=f"tx-{sha256_hex([policy_id, str(event_type), policy.version, secrets.token_hex(4)])[:40]}",
            channel_id=channel_id,
            function=f"{event_type}",
            arguments={"policyId": policy_id, "payload": payload},
            payload_hash=chaincode_hash(channel_id, str(event_type), payload),
            required_orgs=required,
            submitted_by=actor_subject,
        )
        self._ledger.transactions[tx.tx_id] = tx

        if endorse_now:
            for org_id in required:
                try:
                    self._ledger.endorse(tx, org_id)
                except (PermissionError, KeyError):
                    # An org whose role cannot endorse state is simply not asked.
                    continue

        if not tx.is_endorsed:
            tx.status = TransactionStatus.PENDING_ENDORSEMENT
            tx.rejection_reason = self._ledger.policy_for(channel_id).explain(
                set(tx.endorsing_orgs)
            )
            return tx

        self._commit(tx, policy, event_type, payload, status_after, actor_org, actor_subject)
        return tx

    def _eligible_endorsers(self, channel_id: str) -> list[str]:
        """The organisations whose endorsement policy actually requires a signature.

        A channel's raw policy can include organisations whose role forbids them to
        endorse - a regulator observes but does not write. Resolving the policy over
        those members would make every transaction unsatisfiable, which is how a
        ledger ends up with transactions that never commit for a reason nobody
        diagnosed.
        """
        policy_orgs = self._ledger.policy_for(channel_id).required_orgs()
        eligible = [
            org_id
            for org_id in policy_orgs
            if self._ledger.org_role(org_id).can_endorse_policy_state
        ]
        return eligible or policy_orgs

    def _commit(
        self,
        tx: Transaction,
        policy: Policy,
        event_type: EventType,
        payload: dict[str, Any],
        status_after: PolicyStatus,
        actor_org: str,
        actor_subject: str,
    ) -> PolicyEvent:
        """Seal a block if needed and append the hash-chained event."""
        previous = self._ledger.events_for(policy.policy_id)
        previous_hash = previous[-1].event_hash if previous else GENESIS_HASH

        # The transaction must be marked committed *before* sealing, because
        # sealing only picks up transactions the ledger considers already valid.
        tx.status = TransactionStatus.VALID
        block = self._ledger.seal_block(self.channel_id)
        event = build_event(
            policy=policy,
            event_type=event_type,
            payload=payload,
            actor_org=actor_org,
            actor_subject=actor_subject,
            tx_id=tx.tx_id,
            previous_event_hash=previous_hash,
            event_id=f"EV-{policy.policy_id}-{policy.version:04d}-{tx.tx_id[-8:]}",
        )
        event.block_number = block.block_number if block else None
        self._ledger.events.append(event)

        tx.block_number = event.block_number
        tx.result = {"eventId": event.event_id, "statusAfter": str(status_after)}
        self._apply(policy, event_type, payload, status_after)
        return event

    def _apply(
        self,
        policy: Policy,
        event_type: EventType,
        payload: dict[str, Any],
        status_after: PolicyStatus,
    ) -> None:
        """Update the derived projection. The event remains the record."""
        policy.status = status_after
        policy.version += 1
        policy.event_count += 1
        policy.updated_at = datetime.now(UTC)
        if event_type is EventType.ISSUE:
            policy.issued_on = date.today()
        if "beneficiaries" in payload and event_type is EventType.BENEFICIARY_CHANGE_APPROVED:
            policy.beneficiaries = [Beneficiary.model_validate(b) for b in payload["beneficiaries"]]
        if "cashValue" in payload:
            policy.cash_value = float(payload["cashValue"])
        if "faceAmount" in payload and event_type is EventType.MODIFY:
            policy.face_amount = float(payload["faceAmount"])

    # -- policies ----------------------------------------------------------

    def create_policy(
        self,
        *,
        tenant_id: str,
        policy_number: str,
        holder_subject: str,
        product_code: str,
        face_amount: float,
        annual_premium: float = 0.0,
        beneficiaries: list[Beneficiary] | None = None,
        actor_org: str = "org-carrier",
        actor_subject: str = "carrier.underwriter",
    ) -> Policy:
        """Create a policy and commit its `issue` event."""
        policy_id = f"pol-{sha256_hex([tenant_id, policy_number])[:16]}"
        if policy_id in self._policies:
            msg = f"policy '{policy_number}' already exists for this tenant"
            raise LedgerError(msg)

        policy = Policy(
            policy_id=policy_id,
            tenant_id=tenant_id,
            policy_number=policy_number,
            holder_subject=holder_subject,
            product_code=product_code,
            face_amount=face_amount,
            annual_premium=annual_premium,
            beneficiaries=beneficiaries or [],
            status=PolicyStatus.PENDING,
        )
        self._policies[policy_id] = policy

        self.submit_event(
            policy_id=policy_id,
            event_type=EventType.ISSUE,
            payload={
                "policyNumber": policy_number,
                "productCode": product_code,
                "faceAmount": face_amount,
                "annualPremium": annual_premium,
                "beneficiaries": [b.to_dict() for b in policy.beneficiaries],
            },
            actor_org=actor_org,
            actor_subject=actor_subject,
        )
        return policy

    def get_policy(self, policy_id: str) -> Policy | None:
        return self._policies.get(policy_id)

    def find_policy_by_number(self, policy_number: str) -> Policy | None:
        return next(
            (p for p in self._policies.values() if p.policy_number == policy_number), None
        )

    def policies(self, tenant_id: str | None = None) -> list[Policy]:
        return [
            p for p in self._policies.values() if tenant_id is None or p.tenant_id == tenant_id
        ]

    def history(self, policy_id: str) -> list[PolicyEvent]:
        """`GET /blockchain/v1/policies/{id}/history` - the policy's full record."""
        return self._ledger.events_for(policy_id)

    def transition(
        self,
        policy_id: str,
        event_type: EventType,
        payload: dict[str, Any] | None = None,
        *,
        actor_org: str = "org-carrier",
        actor_subject: str = "carrier.underwriter",
    ) -> Transaction:
        """Convenience wrapper for single-endorser lifecycle transitions."""
        return self.submit_event(
            policy_id=policy_id,
            event_type=event_type,
            payload=payload or {},
            actor_org=actor_org,
            actor_subject=actor_subject,
        )

    # -- beneficiaries -----------------------------------------------------

    def request_beneficiary_change(
        self, policy_id: str, beneficiaries: list[Beneficiary], reason: str, request_id: str
    ) -> BeneficiaryChangeRequest:
        """Record a proposed beneficiary change awaiting multi-signature approval."""
        policy = self._policies.get(policy_id)
        if policy is None:
            msg = f"unknown policy '{policy_id}'"
            raise LedgerError(msg)
        try:
            request = self._chaincode.build_beneficiary_request(
                policy_id, beneficiaries, reason, request_id
            )
        except ChaincodeError as exc:
            msg = str(exc)
            raise LedgerError(msg) from exc
        self._beneficiary_requests[request.request_id] = request

        self.submit_event(
            policy_id=policy_id,
            event_type=EventType.BENEFICIARY_CHANGE_REQUESTED,
            payload={
                "requestId": request.request_id,
                "reason": reason,
                "beneficiaries": [b.to_dict() for b in beneficiaries],
            },
            actor_org="org-carrier",
            actor_subject="carrier.beneficiary_admin",
        )
        return request

    def endorse_beneficiary_change(
        self, request_id: str, org_id: str, *, subject: str = ""
    ) -> Endorsement:
        """Add one organisation's approval to a pending change."""
        request = self._beneficiary_requests.get(request_id)
        if request is None:
            msg = f"unknown beneficiary change request '{request_id}'"
            raise LedgerError(msg)
        if request.applied:
            msg = f"request '{request_id}' has already been applied"
            raise LedgerError(msg)

        payload_hash = sha256_hex(request.unsigned_payload())
        signature = self._ledger.keystore.sign(org_id, bytes.fromhex(payload_hash))
        endorsement = Endorsement(
            org_id=org_id,
            signature=signature,
            signed_payload_hash=payload_hash,
        )
        request.endorsements.append(endorsement)
        return endorsement

    def apply_beneficiary_change(
        self, request_id: str, *, actor_subject: str = "carrier.beneficiary_admin"
    ) -> Policy:
        """Apply a change that reached quorum, and record it forever."""
        request = self._beneficiary_requests.get(request_id)
        if request is None:
            msg = f"unknown beneficiary change request '{request_id}'"
            raise LedgerError(msg)
        policy = self._policies.get(request.policy_id)
        if policy is None:
            msg = f"unknown policy '{request.policy_id}'"
            raise LedgerError(msg)
        try:
            self._chaincode.apply_beneficiary_request(policy, request)
        except ChaincodeError as exc:
            msg = str(exc)
            raise LedgerError(msg) from exc

        self.submit_event(
            policy_id=policy.policy_id,
            event_type=EventType.BENEFICIARY_CHANGE_APPROVED,
            payload={
                "requestId": request.request_id,
                "reason": request.reason,
                "beneficiaries": [b.to_dict() for b in request.proposed_beneficiaries],
                "endorsedBy": request.endorsing_orgs,
            },
            actor_org="org-carrier",
            actor_subject=actor_subject,
        )
        return policy

    def beneficiary_request(self, request_id: str) -> BeneficiaryChangeRequest | None:
        return self._beneficiary_requests.get(request_id)

    # -- claims ------------------------------------------------------------

    def file_claim(
        self,
        policy_id: str,
        *,
        claim_id: str,
        amount_requested: float,
        claim_type: str = "death",
        subject: str = "carrier.claims",
    ) -> tuple[Claim, Transaction]:
        """File a claim and record the decision about whether a human must approve."""
        policy = self._policies.get(policy_id)
        if policy is None:
            msg = f"unknown policy '{policy_id}'"
            raise LedgerError(msg)
        claim = Claim(
            claim_id=claim_id,
            policy_id=policy_id,
            claim_type=claim_type,
            amount_requested=amount_requested,
        )
        try:
            self._chaincode.assess_claim(policy, claim)
        except ChaincodeError as exc:
            msg = str(exc)
            raise LedgerError(msg) from exc

        self._claims[claim_id] = claim
        claim.status = ClaimStatus.UNDER_REVIEW if claim.requires_manual_approval else ClaimStatus.APPROVED
        tx = self.submit_event(
            policy_id=policy_id,
            event_type=EventType.CLAIM_FILED,
            payload={
                "claimId": claim_id,
                "amountRequested": amount_requested,
                "claimType": claim_type,
                "requiresManualApproval": claim.requires_manual_approval,
            },
            actor_org="org-carrier",
            actor_subject=subject,
        )
        return claim, tx

    def approve_claim(
        self, claim_id: str, amount_approved: float, *, reason: str = ""
    ) -> Claim:
        """Approve and pay a claim. An approval over the requested amount is refused."""
        claim = self._claims.get(claim_id)
        if claim is None:
            msg = f"unknown claim '{claim_id}'"
            raise LedgerError(msg)
        if claim.status.is_closed:
            msg = f"claim '{claim_id}' is already {claim.status}"
            raise LedgerError(msg)
        if amount_approved > claim.amount_requested:
            msg = (
                f"approved amount {amount_approved:,.0f} exceeds the requested "
                f"{claim.amount_requested:,.0f}"
            )
            raise LedgerError(msg)

        claim.amount_approved = amount_approved
        claim.decision_reason = reason
        claim.status = ClaimStatus.PAID
        self.submit_event(
            policy_id=claim.policy_id,
            event_type=EventType.CLAIM_PAID,
            payload={
                "claimId": claim_id,
                "amountApproved": amount_approved,
                "amountRequested": claim.amount_requested,
                "reason": reason,
            },
            actor_org="org-carrier",
            actor_subject="carrier.claims_manager",
        )
        return claim

    def decline_claim(self, claim_id: str, reason: str) -> Claim:
        claim = self._claims.get(claim_id)
        if claim is None:
            msg = f"unknown claim '{claim_id}'"
            raise LedgerError(msg)
        if claim.status.is_closed:
            msg = f"claim '{claim_id}' is already {claim.status}"
            raise LedgerError(msg)
        claim.status = ClaimStatus.DECLINED
        claim.decision_reason = reason
        self.submit_event(
            policy_id=claim.policy_id,
            event_type=EventType.CLAIM_DECLINED,
            payload={"claimId": claim_id, "reason": reason},
            actor_org="org-carrier",
            actor_subject="carrier.claims_manager",
        )
        return claim

    def get_claim(self, claim_id: str) -> Claim | None:
        return self._claims.get(claim_id)

    # -- annuities ---------------------------------------------------------

    def schedule_annuity(
        self,
        policy_id: str,
        *,
        starting_value: float,
        monthly_withdrawal: float,
        periods: int = 12,
        tax_rate_bps: int = 1500,
    ) -> list[AnnuityScheduleEntry]:
        """Generate a drawdown schedule and record the obligation before any payment."""
        policy = self._policies.get(policy_id)
        if policy is None:
            msg = f"unknown policy '{policy_id}'"
            raise LedgerError(msg)
        try:
            entries = self._chaincode.build_annuity_schedule(
                policy_id,
                starting_value=starting_value,
                monthly_withdrawal=monthly_withdrawal,
                periods=periods,
                tax_rate_bps=tax_rate_bps,
            )
        except ChaincodeError as exc:
            msg = str(exc)
            raise LedgerError(msg) from exc

        self._schedules[policy_id] = entries
        self.submit_event(
            policy_id=policy_id,
            event_type=EventType.ANNUITY_SCHEDULED,
            payload={
                "startingValue": starting_value,
                "monthlyWithdrawal": monthly_withdrawal,
                "periods": periods,
                "totalScheduled": self._chaincode.total_scheduled(entries),
                "firstPayment": entries[0].scheduled_for.isoformat(),
            },
            actor_org="org-carrier",
            actor_subject="carrier.annuity_admin",
        )
        return entries

    def pay_annuity_entry(self, policy_id: str, entry_id: str) -> AnnuityScheduleEntry:
        """Mark one scheduled drawdown as paid, recording the payment as an event."""
        entries = self._schedules.get(policy_id)
        if not entries:
            msg = f"policy '{policy_id}' has no annuity schedule"
            raise LedgerError(msg)
        entry = next((e for e in entries if e.entry_id == entry_id), None)
        if entry is None:
            msg = f"unknown schedule entry '{entry_id}'"
            raise LedgerError(msg)
        if entry.status is TransferStatus.COMPLETED:
            msg = f"schedule entry '{entry_id}' has already been paid"
            raise LedgerError(msg)

        entry.status = TransferStatus.COMPLETED
        self.submit_event(
            policy_id=policy_id,
            event_type=EventType.ANNUITY_PAYOUT,
            payload={
                "entryId": entry_id,
                "grossWithdrawal": entry.gross_withdrawal,
                "taxWithholding": entry.tax_withholding,
                "netPaid": entry.net_paid,
            },
            actor_org="org-carrier",
            actor_subject="carrier.annuity_admin",
        )
        return entry

    def annuity_schedule(self, policy_id: str) -> list[AnnuityScheduleEntry]:
        return list(self._schedules.get(policy_id, []))

    def annuity_position(self, policy_id: str) -> dict[str, Any]:
        """Reconstruct an annuity's position purely from the ledger.

        This is the capability a fragmented estate lacks: every withdrawal, every
        payment, derived from events rather than from a table someone can edit.
        """
        entries = self._schedules.get(policy_id, [])
        paid = [e for e in entries if e.status is TransferStatus.COMPLETED]
        return {
            "policyId": policy_id,
            "scheduledEntries": len(entries),
            "paidEntries": len(paid),
            "pendingEntries": len(entries) - len(paid),
            "grossPaid": round(sum(e.gross_withdrawal for e in paid), 2),
            "taxWithheld": round(sum(e.tax_withholding for e in paid), 2),
            "netPaid": round(sum(e.net_paid for e in paid), 2),
            "remainingScheduled": round(
                sum(e.net_paid for e in entries if e.status is not TransferStatus.COMPLETED), 2
            ),
        }

    # -- tokens ------------------------------------------------------------

    def mint_token(
        self, policy_id: str, units: int, unit_value: float, owner_did: str, token_id: str
    ) -> PolicyToken:
        policy = self._policies.get(policy_id)
        if policy is None:
            msg = f"unknown policy '{policy_id}'"
            raise LedgerError(msg)
        try:
            token = self._chaincode.mint_token(
                policy, units, unit_value, policy.holder_subject, owner_did, token_id
            )
        except ChaincodeError as exc:
            msg = str(exc)
            raise LedgerError(msg) from exc
        self._tokens[token_id] = token
        self.submit_event(
            policy_id=policy_id,
            event_type=EventType.TOKEN_ISSUED,
            payload={"tokenId": token_id, "units": units, "unitValue": unit_value, "ownerDid": owner_did},
            actor_org="org-carrier",
            actor_subject="carrier.tokenisation",
        )
        return token

    def transfer_token(
        self, token_id: str, units: int, transferee_did: str, transferee_subject: str
    ) -> dict[str, Any]:
        token = self._tokens.get(token_id)
        if token is None:
            msg = f"unknown token '{token_id}'"
            raise LedgerError(msg)
        try:
            updated, fee = self._chaincode.transfer_token(
                token, units, transferee_subject, transferee_did
            )
        except ChaincodeError as exc:
            msg = str(exc)
            raise LedgerError(msg) from exc

        self.submit_event(
            policy_id=token.policy_id,
            event_type=EventType.TOKEN_TRANSFERRED,
            payload={
                "tokenId": token_id,
                "units": units,
                "fee": fee,
                "fromDid": token.owner_did,
                "toDid": transferee_did,
            },
            actor_org="org-carrier",
            actor_subject="carrier.tokenisation",
        )
        return {"token": updated.to_dict(), "fee": fee, "remainingUnits": updated.units}

    def get_token(self, token_id: str) -> PolicyToken | None:
        return self._tokens.get(token_id)

    # -- verification ------------------------------------------------------

    def verify(self) -> dict[str, Any]:
        """Audit the chain: block links, Merkle roots, event hashes and anchors."""
        return self._ledger.verify_chain(self.channel_id).to_dict()

    def event_proof(self, event_id: str) -> dict[str, Any]:
        """An inclusion proof for one event against its block's Merkle root.

        Given the root, a third party can confirm the event is in the block
        without trusting this service.
        """
        event = next((e for e in self._ledger.events if e.event_id == event_id), None)
        if event is None:
            msg = f"unknown event '{event_id}'"
            raise LedgerError(msg)
        block_number = event.block_number or 0
        block = next(
            (b for b in self._ledger.blocks if b.block_number == block_number), None
        )
        if block is None:
            msg = f"event {event_id} has no sealed block"
            raise LedgerError(msg)
        leaves = [
            self._ledger.transactions[tx_id].payload_hash
            for tx_id in block.tx_ids
            if tx_id in self._ledger.transactions
        ]
        tx_index = block.tx_ids.index(event.tx_id) if event.tx_id in block.tx_ids else 0
        proof = merkle_proof(leaves, tx_index)
        return {
            "eventId": event_id,
            "blockNumber": block_number,
            "leaf": leaves[tx_index],
            "proof": [{"sibling": sibling, "isLeft": is_left} for sibling, is_left in proof],
            "merkleRoot": block.merkle_root,
            "verified": verify_merkle_proof(leaves[tx_index], proof, block.merkle_root),
        }

    def seal(self, force: bool = False) -> Block | None:
        return self._ledger.seal_block(self.channel_id, force=force)

    # -- portability -------------------------------------------------------

    def export_package(self, policy_id: str, *, exported_by: str = "carrier.admin") -> PortabilityPackage:
        """Export a policy's whole history as a self-verifying package."""
        policy = self._policies.get(policy_id)
        if policy is None:
            msg = f"unknown policy '{policy_id}'"
            raise LedgerError(msg)
        events = self._ledger.events_for(policy_id)
        if not events:
            msg = f"policy '{policy_id}' has no events to export"
            raise LedgerError(msg)

        blocks = [
            b
            for b in self._ledger.blocks
            if any(e.tx_id in b.tx_ids for e in events)
        ]
        event_hashes = [e.event_hash for e in events]
        root = sha256_hex(event_hashes)
        export_hash = sha256_hex(
            {
                "policyId": policy_id,
                "events": event_hashes,
                "blocks": [b.block_hash for b in blocks],
            }
        )
        package = PortabilityPackage(
            package_id=f"pkg-{export_hash[:16]}",
            policy_id=policy_id,
            source_tenant=policy.tenant_id,
            events=events,
            blocks=blocks,
            export_hash=export_hash,
            merkle_root=root,
            signing_org="org-carrier",
            exported_by=exported_by,
        )
        package.signature = self._ledger.keystore.sign(
            "org-carrier", bytes.fromhex(export_hash)
        )

        self.submit_event(
            policy_id=policy_id,
            event_type=EventType.PORTABILITY_EXPORT,
            payload={"packageId": package.package_id, "exportHash": export_hash},
            actor_org="org-carrier",
            actor_subject=exported_by,
        )
        return package

    def verify_package(self, package: PortabilityPackage) -> dict[str, Any]:
        """Verify a package without trusting the ledger that produced it."""
        checks: dict[str, bool] = {}
        problems: list[str] = []

        recomputed_export = sha256_hex(
            {
                "policyId": package.policy_id,
                "events": [e.event_hash for e in package.events],
                "blocks": [b.block_hash for b in package.blocks],
            }
        )
        checks["exportHashMatches"] = recomputed_export == package.export_hash
        if not checks["exportHashMatches"]:
            problems.append("the export hash does not match the exported content")

        recomputed_root = sha256_hex([e.event_hash for e in package.events])
        checks["merkleRootMatches"] = recomputed_root == package.merkle_root
        if not checks["merkleRootMatches"]:
            problems.append("the Merkle root does not match the exported events")

        checks["eventsSelfConsistent"] = all(
            Ledger.event_hash_for(e) == e.event_hash for e in package.events
        )
        if not checks["eventsSelfConsistent"]:
            problems.append("one or more exported event hashes do not match their content")

        checks["blockChainIntact"] = all(
            recomputed == block.block_hash for block, recomputed in _block_recomputations(package)
        )
        if not checks["blockChainIntact"]:
            problems.append("an exported block hash does not match its content")

        public_key = self._ledger.keystore.public_key(package.signing_org)
        checks["signatureValid"] = bool(
            public_key
            and self._ledger.keystore.verify(
                public_key, bytes.fromhex(package.export_hash), package.signature
            )
        )
        if not checks["signatureValid"]:
            problems.append(f"the package is not validly signed by '{package.signing_org}'")

        return {
            "packageId": package.package_id,
            "policyId": package.policy_id,
            "valid": all(checks.values()),
            "checks": checks,
            "problems": problems,
            "eventCount": len(package.events),
            "sourceTenant": package.source_tenant,
        }

    # -- identity ----------------------------------------------------------

    @property
    def identity(self) -> IdentityService:
        return self._identity

    def beneficiary_credential(
        self,
        beneficiary: Beneficiary,
        *,
        policy_number: str,
        relationship: str = "spouse",
    ) -> dict[str, Any]:
        """Issue a verifiable credential proving a beneficiary's relationship.

        The credential names the policy, so it is only good evidence for that
        policy - a relationship credential that outlived the contract it was
        issued against would be misleading.
        """
        if not beneficiary.did:
            msg = f"beneficiary '{beneficiary.full_name}' has no DID; issue one first"
            raise LedgerError(msg)
        credential = self._identity.issue_credential(
            issuer_subject="org-carrier",
            subject={
                "id": beneficiary.did,
                "fullName": beneficiary.full_name,
                "relationship": relationship,
                "policyNumber": policy_number,
            },
            credential_type="BeneficiaryRelationship",
        )
        return {"credential": credential.to_dict()}

    def health_summary(self) -> dict[str, Any]:
        verification = self._ledger.verify_chain(self.channel_id)
        return {
            "plugin": self._settings.plugin_id,
            "channelId": self.channel_id,
            "organizations": len(self._ledger.organizations),
            "policies": len(self._policies),
            "events": len(self._ledger.events),
            "blocks": len(self.blocks()),
            "transactions": len(self.transactions()),
            "pendingTransactions": len(self._ledger.pending_transactions(self.channel_id)),
            "chainTrustworthy": verification.is_trustworthy,
            "claims": len(self._claims),
            "tokens": len(self._tokens),
            "annuitySchedules": len(self._schedules),
        }


def _block_recomputations(package: PortabilityPackage) -> list[tuple[Any, str]]:
    return [
        (
            block,
            sha256_hex(
                {
                    "blockNumber": block.block_number,
                    "channelId": block.channel_id,
                    "previousHash": block.previous_hash,
                    "dataHash": block.data_hash,
                    "merkleRoot": block.merkle_root,
                }
            ),
        )
        for block in package.blocks
    ]


_default_service: PolicyLedgerService | None = None


def default_ledger_service() -> PolicyLedgerService:
    """Process-wide ledger service with the standard network bootstrapped."""
    global _default_service
    if _default_service is None:
        service = PolicyLedgerService(Plugin7Settings())
        service.bootstrap_network()
        _default_service = service
    return _default_service


__all__ = [
    "LedgerError",
    "PolicyLedgerService",
    "default_ledger_service",
]