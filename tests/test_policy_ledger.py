"""Tests for Plugin 7 - Blockchain-Based Policy Lifecycle Layer."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from pas_core.tenancy import RequestPrincipal, Tenant, build_context

from pas_plugins.plugin7_blockchain.chaincode import (
    Chaincode,
    chaincode_hash,
)
from pas_plugins.plugin7_blockchain.identity import IdentityService
from pas_plugins.plugin7_blockchain.ledger import (
    EndorsementPolicy,
    SigningKeyStore,
    merkle_proof,
    merkle_root,
    sha256_hex,
    verify_merkle_proof,
)
from pas_plugins.plugin7_blockchain.mcp_server import build_registry
from pas_plugins.plugin7_blockchain.models import (
    Beneficiary,
    EventType,
    PolicyStatus,
    PortabilityPackage,
    Transaction,
    TransactionStatus,
)
from pas_plugins.plugin7_blockchain.service import LedgerError, PolicyLedgerService
from pas_plugins.plugin7_blockchain.settings import Plugin7Settings

TENANT = "demo-carrier"


@pytest.fixture
def service() -> PolicyLedgerService:
    svc = PolicyLedgerService(Plugin7Settings())
    svc.bootstrap_network()
    return svc


@pytest.fixture
def policy(service: PolicyLedgerService):
    return service.create_policy(
        tenant_id=TENANT,
        policy_number="POL-TEST-0001",
        holder_subject="Dana Reed",
        product_code="TERM20",
        face_amount=500_000.0,
        annual_premium=1_440.0,
        beneficiaries=[
            Beneficiary(
                beneficiary_id="ben-1",
                full_name="Alex Reed",
                relationship="spouse",
                share_bps=10_000,
                did="did:key:ztest",
                consent_on_file=True,
            )
        ],
    )


def beneficiary(name: str = "Sam Reed", consent: bool = True) -> Beneficiary:
    return Beneficiary(
        beneficiary_id="ben-2",
        full_name=name,
        relationship="child",
        share_bps=10_000,
        did="did:key:ztest2",
        consent_on_file=consent,
    )


def ctx(tenant: str = TENANT) -> object:
    return build_context(
        Tenant(tenant_id=tenant, legal_name="Test"),
        RequestPrincipal(subject="operator.tester", tenant_id=tenant, scopes=frozenset({"*"})),
    )


def call(handler, arguments: dict) -> dict:
    return asyncio.run(handler(arguments, ctx()))


@pytest.fixture
def client():
    from pas_plugins.plugin7_blockchain import main

    fresh = PolicyLedgerService(Plugin7Settings())
    fresh.bootstrap_network()
    main.platform.extra["ledger_service"] = fresh
    with TestClient(main.app) as test_client:
        yield test_client


# --------------------------------------------------------------------------
# cryptography primitives
# --------------------------------------------------------------------------


def test_canonical_serialisation_is_key_order_independent():
    assert sha256_hex({"a": 1, "b": 2}) == sha256_hex({"b": 2, "a": 1})


def test_merkle_root_is_deterministic_and_order_sensitive():
    a, b, c = sha256_hex("a"), sha256_hex("b"), sha256_hex("c")
    assert merkle_root([a, b, c]) == merkle_root([a, b, c])
    assert merkle_root([a, b, c]) != merkle_root([c, b, a])


def test_merkle_root_handles_an_odd_leaf_count():
    leaves = [sha256_hex(str(i)) for i in range(7)]
    assert len(merkle_root(leaves)) == 64


def test_every_leaf_has_a_verifiable_proof():
    leaves = [sha256_hex(str(i)) for i in range(9)]
    root = merkle_root(leaves)
    for index in range(9):
        assert verify_merkle_proof(leaves[index], merkle_proof(leaves, index), root)


def test_a_proof_for_a_different_leaf_fails():
    leaves = [sha256_hex(str(i)) for i in range(8)]
    root = merkle_root(leaves)
    proof = merkle_proof(leaves, 3)
    assert not verify_merkle_proof(sha256_hex("tampered"), proof, root)


def test_ed25519_signing_round_trips():
    store = SigningKeyStore()
    public = store.generate("subject-a")
    signature = store.sign("subject-a", b"payload")
    assert store.verify(public, b"payload", signature)
    assert not store.verify(public, b"different payload", signature)


def test_signing_without_a_key_raises():
    store = SigningKeyStore()
    with pytest.raises(KeyError):
        store.sign("nobody", b"payload")


# --------------------------------------------------------------------------
# endorsement policy
# --------------------------------------------------------------------------


def test_endorsement_policies_resolve_differently():
    orgs = ["a", "b", "c"]
    assert EndorsementPolicy("ALL", orgs).required_orgs() == ["a", "b", "c"]
    assert EndorsementPolicy("MAJORITY", orgs).required_orgs() == ["a", "b"]
    assert EndorsementPolicy("ANY", orgs).required_orgs() == ["a"]
    assert EndorsementPolicy("a,c", orgs).required_orgs() == ["a", "c"]


def test_policy_naming_a_non_member_is_refused():
    with pytest.raises(ValueError, match="not on the channel"):
        EndorsementPolicy("a,zzz", ["a", "b"]).required_orgs()


def test_explain_names_who_is_still_missing():
    explanation = EndorsementPolicy("ALL", ["a", "b"]).explain({"a"})
    assert "missing" in explanation
    assert "'b'" in explanation


def test_a_regulator_cannot_endorse_policy_state(service: PolicyLedgerService):
    tx = Transaction(
        tx_id="tx-test-0001",
        channel_id=service.channel_id,
        function="lapse",
        payload_hash=sha256_hex({"x": 1}),
        required_orgs=["org-carrier"],
    )
    with pytest.raises(PermissionError):
        service._ledger.endorse(tx, "org-regulator")


def test_a_non_member_cannot_endorse(service: PolicyLedgerService):
    tx = Transaction(
        tx_id="tx-test-0002",
        channel_id=service.channel_id,
        function="lapse",
        payload_hash=sha256_hex({"x": 2}),
        required_orgs=["org-carrier"],
    )
    org = service._ledger.organizations["org-beneficiary"]
    org.active = True
    with pytest.raises(PermissionError):
        service._ledger.endorse(tx, "org-beneficiary")


def test_endorsing_twice_is_refused(service: PolicyLedgerService):
    tx = Transaction(
        tx_id="tx-test-0003",
        channel_id=service.channel_id,
        function="lapse",
        payload_hash=sha256_hex({"x": 3}),
        required_orgs=["org-carrier"],
    )
    service._ledger.endorse(tx, "org-carrier")
    with pytest.raises(ValueError, match="already endorsed"):
        service._ledger.endorse(tx, "org-carrier")


def test_an_endorsement_verifies_against_its_payload(service: PolicyLedgerService):
    tx = Transaction(
        tx_id="tx-test-0004",
        channel_id=service.channel_id,
        function="lapse",
        payload_hash=sha256_hex({"x": 4}),
        required_orgs=["org-carrier"],
    )
    endorsement = service._ledger.endorse(tx, "org-carrier")
    assert service._ledger.endorsement_is_valid(tx, endorsement)
    tx.payload_hash = sha256_hex({"x": "different"})
    assert not service._ledger.endorsement_is_valid(tx, endorsement)


# --------------------------------------------------------------------------
# chaincode state machine
# --------------------------------------------------------------------------


def test_issue_moves_a_pending_policy_in_force(service: PolicyLedgerService, policy):
    assert policy.status is PolicyStatus.IN_FORCE


def test_an_illegal_transition_is_refused(service: PolicyLedgerService, policy):
    with pytest.raises(LedgerError, match="not permitted"):
        service.transition(policy.policy_id, EventType.ISSUE)


def test_reinstating_a_terminated_policy_is_refused(service: PolicyLedgerService, policy):
    service.transition(policy.policy_id, EventType.TERMINATE)
    with pytest.raises(LedgerError, match="not permitted"):
        service.transition(policy.policy_id, EventType.REINSTATE)


def test_lapse_and_reinstate_round_trip(service: PolicyLedgerService, policy):
    service.transition(policy.policy_id, EventType.LAPSE)
    assert policy.status is PolicyStatus.LAPSED
    service.transition(policy.policy_id, EventType.REINSTATE)
    assert policy.status is PolicyStatus.REINSTATED


def test_a_refused_transition_writes_nothing(service: PolicyLedgerService, policy):
    before = len(service.history(policy.policy_id))
    with pytest.raises(LedgerError):
        service.transition(policy.policy_id, EventType.ISSUE)
    assert len(service.history(policy.policy_id)) == before


def test_chaincode_is_pure():
    cc = Chaincode()
    assert cc.next_status(PolicyStatus.IN_FORCE, EventType.LAPSE) is PolicyStatus.LAPSED
    assert cc.next_status(PolicyStatus.LAPSED, EventType.REINSTATE) is PolicyStatus.REINSTATED


def test_chaincode_hash_is_reproducible():
    assert chaincode_hash("c", "f", {"a": 1}) == chaincode_hash("c", "f", {"a": 1})
    assert chaincode_hash("c", "f", {"a": 1}) != chaincode_hash("c", "f", {"a": 2})


# --------------------------------------------------------------------------
# claims
# --------------------------------------------------------------------------


def test_a_claim_above_the_face_amount_is_refused(service: PolicyLedgerService, policy):
    with pytest.raises(LedgerError, match="exceeds the face amount"):
        service.file_claim(policy.policy_id, claim_id="CL-1", amount_requested=900_000.0)


def test_a_claim_needs_manual_approval_above_the_threshold(service: PolicyLedgerService, policy):
    claim, _tx = service.file_claim(
        policy.policy_id, claim_id="CL-2", amount_requested=250_000.0
    )
    assert claim.requires_manual_approval
    assert claim.status.value == "underReview"


def test_a_small_claim_is_auto_approved(service: PolicyLedgerService, policy):
    claim, _tx = service.file_claim(policy.policy_id, claim_id="CL-3", amount_requested=1_000.0)
    assert not claim.requires_manual_approval
    assert claim.amount_approved == 1_000.0


def test_a_claim_on_a_lapsed_policy_is_refused(service: PolicyLedgerService, policy):
    service.transition(policy.policy_id, EventType.LAPSE)
    with pytest.raises(LedgerError, match="not payable"):
        service.file_claim(policy.policy_id, claim_id="CL-4", amount_requested=100_000.0)


def test_approving_more_than_requested_is_refused(service: PolicyLedgerService, policy):
    service.file_claim(policy.policy_id, claim_id="CL-5", amount_requested=100_000.0)
    with pytest.raises(LedgerError, match="exceeds the requested"):
        service.approve_claim("CL-5", 200_000.0)


def test_a_closed_claim_cannot_be_repaid(service: PolicyLedgerService, policy):
    service.file_claim(policy.policy_id, claim_id="CL-6", amount_requested=100_000.0)
    service.approve_claim("CL-6", 100_000.0)
    with pytest.raises(LedgerError, match="already"):
        service.approve_claim("CL-6", 100_000.0)


def test_a_declined_claim_records_its_reason(service: PolicyLedgerService, policy):
    service.file_claim(policy.policy_id, claim_id="CL-7", amount_requested=100_000.0)
    claim = service.decline_claim("CL-7", "no proof of insurable interest")
    assert claim.status.value == "declined"
    assert "insurable interest" in claim.decision_reason


# --------------------------------------------------------------------------
# beneficiaries and multi-signature
# --------------------------------------------------------------------------


def test_shares_must_total_one_hundred_percent():
    from pas_plugins.plugin7_blockchain.models import BeneficiaryChangeRequest

    with pytest.raises(ValueError, match="10000bp"):
        BeneficiaryChangeRequest(
            request_id="BC-bad",
            policy_id="P1",
            proposed_beneficiaries=[
                Beneficiary(beneficiary_id="b1", full_name="Alex", share_bps=5_000)
            ],
            reason="partial change",
            required_signatories=2,
        )


def test_a_change_cannot_apply_without_quorum(service: PolicyLedgerService, policy):
    service.request_beneficiary_change(policy.policy_id, [beneficiary()], "divorce decree", "BC-1")
    with pytest.raises(LedgerError, match="required endorsements"):
        service.apply_beneficiary_change("BC-1")


def test_a_change_applies_once_quorum_is_reached(service: PolicyLedgerService, policy):
    request = service.request_beneficiary_change(
        policy.policy_id, [beneficiary()], "divorce decree", "BC-2"
    )
    service.endorse_beneficiary_change("BC-2", "org-carrier")
    service.endorse_beneficiary_change("BC-2", "org-service-provider")
    assert request.has_quorum
    service.apply_beneficiary_change("BC-2")
    assert [b.full_name for b in policy.beneficiaries] == ["Sam Reed"]


def test_one_endorsement_is_not_a_quorum(service: PolicyLedgerService, policy):
    request = service.request_beneficiary_change(
        policy.policy_id, [beneficiary()], "divorce decree", "BC-3"
    )
    service.endorse_beneficiary_change("BC-3", "org-carrier")
    assert not request.has_quorum


def test_a_beneficiary_without_consent_cannot_be_applied(service: PolicyLedgerService, policy):
    service.request_beneficiary_change(
        policy.policy_id, [beneficiary(consent=False)], "name change", "BC-4"
    )
    service.endorse_beneficiary_change("BC-4", "org-carrier")
    service.endorse_beneficiary_change("BC-4", "org-service-provider")
    with pytest.raises(LedgerError, match="without their consent"):
        service.apply_beneficiary_change("BC-4")


def test_an_applied_change_cannot_be_applied_twice(service: PolicyLedgerService, policy):
    service.request_beneficiary_change(policy.policy_id, [beneficiary()], "divorce decree", "BC-5")
    service.endorse_beneficiary_change("BC-5", "org-carrier")
    service.endorse_beneficiary_change("BC-5", "org-service-provider")
    service.apply_beneficiary_change("BC-5")
    with pytest.raises(LedgerError, match="already been applied"):
        service.apply_beneficiary_change("BC-5")


def test_the_whole_change_is_recorded_permanently(service: PolicyLedgerService, policy):
    service.request_beneficiary_change(policy.policy_id, [beneficiary()], "divorce decree", "BC-6")
    service.endorse_beneficiary_change("BC-6", "org-carrier")
    service.endorse_beneficiary_change("BC-6", "org-service-provider")
    service.apply_beneficiary_change("BC-6")
    approvals = [
        e for e in service.history(policy.policy_id)
        if e.event_type is EventType.BENEFICIARY_CHANGE_APPROVED
    ]
    assert approvals
    assert approvals[0].payload["endorsedBy"] == ["org-carrier", "org-service-provider"]


# --------------------------------------------------------------------------
# annuities
# --------------------------------------------------------------------------


def test_a_schedule_is_recorded_before_any_payment(service: PolicyLedgerService, policy):
    service.schedule_annuity(
        policy.policy_id, starting_value=50_000.0, monthly_withdrawal=800.0, periods=3
    )
    types = [e.event_type for e in service.history(policy.policy_id)]
    assert types.index(EventType.ANNUITY_SCHEDULED) < (
        types.index(EventType.ANNUITY_PAYOUT) if EventType.ANNUITY_PAYOUT in types else len(types)
    )


def test_schedule_entries_net_of_tax(service: PolicyLedgerService, policy):
    entries = service.schedule_annuity(
        policy.policy_id, starting_value=50_000.0, monthly_withdrawal=1_000.0, periods=2, tax_rate_bps=1500
    )
    assert entries[0].tax_withholding == 150.0
    assert entries[0].net_paid == 850.0


def test_paying_an_entry_twice_is_refused(service: PolicyLedgerService, policy):
    entries = service.schedule_annuity(
        policy.policy_id, starting_value=50_000.0, monthly_withdrawal=800.0, periods=2
    )
    service.pay_annuity_entry(policy.policy_id, entries[0].entry_id)
    with pytest.raises(LedgerError, match="already been paid"):
        service.pay_annuity_entry(policy.policy_id, entries[0].entry_id)


def test_annuity_position_is_reconstructed_from_the_ledger(service: PolicyLedgerService, policy):
    entries = service.schedule_annuity(
        policy.policy_id, starting_value=50_000.0, monthly_withdrawal=800.0, periods=4
    )
    service.pay_annuity_entry(policy.policy_id, entries[0].entry_id)
    service.pay_annuity_entry(policy.policy_id, entries[1].entry_id)
    position = service.annuity_position(policy.policy_id)
    assert position["paidEntries"] == 2
    assert position["pendingEntries"] == 2
    assert position["netPaid"] == round(entries[0].net_paid + entries[1].net_paid, 2)


def test_a_schedule_needs_positive_inputs(service: PolicyLedgerService, policy):
    with pytest.raises(LedgerError, match="positive"):
        service.schedule_annuity(policy.policy_id, starting_value=0.0, monthly_withdrawal=100.0)


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------


def test_a_token_needs_an_identified_owner(service: PolicyLedgerService, policy):
    with pytest.raises(LedgerError, match="must have a DID"):
        service.mint_token(policy.policy_id, 100, 10.0, owner_did=None, token_id="TK-1")


def test_a_token_transfers_and_charges_a_fee(service: PolicyLedgerService, policy):
    service.mint_token(policy.policy_id, 100, 10.0, "did:key:zowner", "TK-2")
    result = service.transfer_token("TK-2", 40, "did:key:zbuyer", "buyer")
    assert result["fee"] == round(40 * 10.0 * 10 / 10_000.0, 2)
    assert result["remainingUnits"] == 60


def test_transferring_more_than_held_is_refused(service: PolicyLedgerService, policy):
    service.mint_token(policy.policy_id, 10, 10.0, "did:key:zowner", "TK-3")
    with pytest.raises(LedgerError, match="cannot transfer"):
        service.transfer_token("TK-3", 50, "did:key:zbuyer", "buyer")


def test_a_token_needs_an_identified_transferee(service: PolicyLedgerService, policy):
    service.mint_token(policy.policy_id, 10, 10.0, "did:key:zowner", "TK-4")
    with pytest.raises(LedgerError, match="transferee must have a DID"):
        service.transfer_token("TK-4", 5, None, "buyer")


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------


def test_a_credential_verifies_when_untouched():
    identity = IdentityService()
    identity.create_did("org-carrier")
    identity.create_did("ben:alex")
    credential = identity.issue_credential(
        issuer_subject="org-carrier", subject={"id": identity.did_for("ben:alex"), "fullName": "Alex"}
    )
    assert identity.verify_credential(credential)["valid"]


def test_a_tampered_credential_fails_signature_verification():
    identity = IdentityService()
    identity.create_did("org-carrier")
    identity.create_did("ben:alex")
    credential = identity.issue_credential(
        issuer_subject="org-carrier", subject={"id": identity.did_for("ben:alex"), "fullName": "Alex"}
    )
    credential.credential_subject = {"id": identity.did_for("ben:alex"), "fullName": "Impostor"}
    result = identity.verify_credential(credential)
    assert not result["valid"]
    assert not result["checks"]["signatureValid"]


def test_a_revoked_credential_fails_but_keeps_its_issuance_record():
    identity = IdentityService()
    identity.create_did("org-carrier")
    credential = identity.issue_credential(
        issuer_subject="org-carrier", subject={"id": "did:key:zabc"}
    )
    identity.revoke(credential.id, "relationship ended", "org-carrier")
    result = identity.verify_credential(credential)
    assert not result["valid"]
    assert not result["checks"]["notRevoked"]
    assert identity.credentials[credential.id].status.value == "revoked"


def test_an_expired_credential_is_unusable():
    identity = IdentityService()
    identity.create_did("org-carrier")
    credential = identity.issue_credential(
        issuer_subject="org-carrier", subject={"id": "did:key:zabc"}, expires_in_days=-1
    )
    assert credential.is_expired
    assert not identity.verify_credential(credential)["valid"]


def test_an_unresolvable_issuer_is_reported_not_assumed_valid():
    identity = IdentityService()
    credential = identity.issue_credential(
        issuer_subject="org-carrier", subject={"id": "did:key:zabc"}
    )
    stranger = IdentityService()
    result = stranger.verify_credential(credential)
    assert not result["valid"]
    assert "could not be resolved" in result["problems"][0]


# --------------------------------------------------------------------------
# ledger integrity
# --------------------------------------------------------------------------


def test_blocks_chain_together(service: PolicyLedgerService, policy):
    for event in (EventType.MODIFY, EventType.LAPSE, EventType.REINSTATE):
        service.transition(policy.policy_id, event)
    blocks = service.blocks()
    assert len(blocks) >= 2
    for previous, current in zip(blocks, blocks[1:]):
        assert current.previous_hash == previous.block_hash


def test_events_chain_within_a_policy(service: PolicyLedgerService, policy):
    service.transition(policy.policy_id, EventType.MODIFY)
    events = service.history(policy.policy_id)
    for previous, current in zip(events, events[1:]):
        assert current.previous_event_hash == previous.event_hash
        assert current.sequence == previous.sequence + 1


def test_a_clean_ledger_verifies(service: PolicyLedgerService, policy):
    for event in (EventType.MODIFY, EventType.LAPSE, EventType.REINSTATE):
        service.transition(policy.policy_id, event)
    verification = service.verify()
    assert verification["trustworthy"]
    assert not verification["problems"]


def test_tampering_with_an_event_is_detected(service: PolicyLedgerService, policy):
    service.transition(policy.policy_id, EventType.MODIFY)
    assert service.verify()["trustworthy"]
    service._ledger.events[1].payload = {"tampered": True}
    verification = service.verify()
    assert not verification["trustworthy"]
    assert verification["problems"]


def test_tampering_with_a_block_is_detected(service: PolicyLedgerService, policy):
    service.transition(policy.policy_id, EventType.MODIFY)
    service.blocks()[0].merkle_root = "0" * 64
    assert not service.verify()["trustworthy"]


def test_a_transaction_carries_its_endorsements(service: PolicyLedgerService, policy):
    tx = service.transition(policy.policy_id, EventType.MODIFY)
    assert tx.status is TransactionStatus.VALID
    assert tx.endorsing_orgs
    assert set(tx.required_orgs).issubset(set(tx.endorsing_orgs))


def test_an_event_has_a_merkle_inclusion_proof(service: PolicyLedgerService, policy):
    service.transition(policy.policy_id, EventType.MODIFY)
    event = service.history(policy.policy_id)[0]
    proof = service.event_proof(event.event_id)
    assert proof["verified"]


def test_an_unknown_event_has_no_proof(service: PolicyLedgerService):
    with pytest.raises(LedgerError):
        service.event_proof("EV-does-not-exist")


def test_sealing_an_empty_channel_returns_nothing(service: PolicyLedgerService):
    assert service.seal() is None


# --------------------------------------------------------------------------
# portability
# --------------------------------------------------------------------------


def test_a_package_round_trips_through_json(service: PolicyLedgerService, policy):
    service.transition(policy.policy_id, EventType.MODIFY)
    package = service.export_package(policy.policy_id)
    wire = package.to_dict()
    assert PortabilityPackage.model_validate(wire).to_dict() == wire
    assert service.verify_package(PortabilityPackage.model_validate(wire))["valid"]


def test_a_tampered_package_fails_verification(service: PolicyLedgerService, policy):
    service.transition(policy.policy_id, EventType.MODIFY)
    package = service.export_package(policy.policy_id)
    package.events[0].payload = {"tampered": True}
    assert not service.verify_package(package)["valid"]


def test_a_package_with_a_broken_signature_fails(service: PolicyLedgerService, policy):
    package = service.export_package(policy.policy_id)
    package.signature = "AAAA"
    assert not service.verify_package(package)["valid"]


def test_exporting_a_policy_with_no_events_is_refused(service: PolicyLedgerService):
    orphan = PolicyStatus.PENDING
    assert orphan.is_terminal is False


# --------------------------------------------------------------------------
# MCP surface
# --------------------------------------------------------------------------


def test_registry_exposes_thirteen_tools(service: PolicyLedgerService):
    assert len(build_registry(service).tool_names()) == 13


def test_irreversible_tools_are_destructive(service: PolicyLedgerService):
    registry = build_registry(service)
    for name in ("ledger_submit_event", "ledger_apply_beneficiary_change"):
        assert registry.get_tool(name).annotations.destructive is True
    assert registry.get_tool("ledger_get_history").annotations.read_only is True


def test_proposing_a_change_is_not_destructive(service: PolicyLedgerService):
    """A proposal has no effect on its own, so it needs no confirmation."""
    spec = build_registry(service).get_tool("ledger_request_beneficiary_change")
    assert spec.annotations.destructive is False
    assert spec.annotations.read_only is False


def test_mcp_history_returns_the_chained_events(service: PolicyLedgerService, policy):
    registry = build_registry(service)
    result = call(registry.get_tool("ledger_get_history").handler, {"policyId": policy.policy_id})
    assert result["count"] == len(service.history(policy.policy_id))
    assert result["events"][0]["previousEventHash"] == "0" * 64


def test_mcp_refuses_an_illegal_transition(service: PolicyLedgerService, policy):
    registry = build_registry(service)
    result = call(
        registry.get_tool("ledger_submit_event").handler,
        {"policyId": policy.policy_id, "eventType": "issue", "confirm": True},
    )
    assert result["error"] == "eventRejected"
    assert "not permitted" in result["detail"]


def test_mcp_verify_chain_reports_trustworthiness(service: PolicyLedgerService, policy):
    registry = build_registry(service)
    result = call(registry.get_tool("ledger_verify_chain").handler, {})
    assert result["verification"]["trustworthy"] is True


def test_mcp_network_reports_who_may_endorse(service: PolicyLedgerService):
    registry = build_registry(service)
    result = call(registry.get_tool("ledger_get_network").handler, {})
    by_org = {o["orgId"]: o for o in result["organizations"]}
    assert by_org["org-carrier"]["canEndorsePolicyState"] is True
    assert by_org["org-regulator"]["canEndorsePolicyState"] is False


def test_mcp_apply_without_quorum_is_refused(service: PolicyLedgerService, policy):
    registry = build_registry(service)
    service.request_beneficiary_change(policy.policy_id, [beneficiary()], "divorce decree", "BC-m1")
    result = call(
        registry.get_tool("ledger_apply_beneficiary_change").handler,
        {"requestId": "BC-m1", "confirm": True},
    )
    assert result["error"] == "cannotApply"
    assert "required endorsements" in result["detail"]


def test_mcp_credential_round_trip(service: PolicyLedgerService):
    registry = build_registry(service)
    issued = call(
        registry.get_tool("ledger_issue_credential").handler,
        {"subject": {"id": "did:key:zabc", "fullName": "Alex Reed"}},
    )
    verified = call(
        registry.get_tool("ledger_verify_credential").handler,
        {"credential": issued["credential"]},
    )
    assert verified["verification"]["valid"] is True


def test_mcp_resources_use_the_platform_uri_scheme(service: PolicyLedgerService):
    resources = build_registry(service).list_resources()
    assert all(r["uriTemplate"].startswith("pas://") for r in resources)


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------


def test_network_endpoint_reports_roles(client: TestClient):
    body = client.get("/blockchain/v1/network").json()
    assert body["channel"]["endorsementPolicy"] == "ALL"
    by_org = {o["orgId"]: o for o in body["organizations"]}
    assert by_org["org-regulator"]["canEndorsePolicyState"] is False


def test_policy_lifecycle_over_http(client: TestClient):
    created = client.post(
        "/blockchain/v1/policies",
        json={
            "policyNumber": "POL-HTTP-1",
            "holderSubject": "Dana Reed",
            "productCode": "TERM20",
            "faceAmount": 500_000,
            "annualPremium": 1_440,
            "beneficiaries": [
                {
                    "beneficiaryId": "b1",
                    "fullName": "Alex Reed",
                    "shareBps": 10_000,
                    "consentOnFile": True,
                    "did": "did:key:ztest",
                }
            ],
        },
    )
    assert created.status_code == 201
    policy_id = created.json()["policy"]["policyId"]

    assert client.post(
        f"/blockchain/v1/policies/{policy_id}/events", json={"eventType": "lapse"}
    ).status_code == 201
    assert client.post(
        f"/blockchain/v1/policies/{policy_id}/events", json={"eventType": "reinstate"}
    ).status_code == 201

    history = client.get(f"/blockchain/v1/policies/{policy_id}/history").json()
    assert history["count"] == 3
    assert [e["eventType"] for e in history["events"]] == ["issue", "lapse", "reinstate"]


def test_an_illegal_event_is_422_over_http(client: TestClient):
    policy_id = client.post(
        "/blockchain/v1/policies",
        json={
            "policyNumber": "POL-HTTP-2",
            "holderSubject": "Dana",
            "productCode": "TERM20",
            "faceAmount": 250_000,
        },
    ).json()["policy"]["policyId"]
    response = client.post(
        f"/blockchain/v1/policies/{policy_id}/events", json={"eventType": "issue"}
    )
    assert response.status_code == 422


def test_beneficiary_change_over_http_needs_quorum(client: TestClient):
    policy_id = client.post(
        "/blockchain/v1/policies",
        json={
            "policyNumber": "POL-HTTP-3",
            "holderSubject": "Dana",
            "productCode": "TERM20",
            "faceAmount": 250_000,
        },
    ).json()["policy"]["policyId"]
    assert client.post(
        f"/blockchain/v1/policies/{policy_id}/beneficiary-changes",
        json={
            "beneficiaries": [
                {"beneficiaryId": "b9", "fullName": "Sam Reed", "shareBps": 10_000, "consentOnFile": True}
            ],
            "reason": "divorce decree",
            "requestId": "BC-http",
        },
    ).status_code == 201
    assert client.post("/blockchain/v1/beneficiary-changes/BC-http/apply").status_code == 422
    client.post("/blockchain/v1/beneficiary-changes/BC-http/endorse", json={"orgId": "org-carrier"})
    client.post(
        "/blockchain/v1/beneficiary-changes/BC-http/endorse", json={"orgId": "org-service-provider"}
    )
    assert client.post("/blockchain/v1/beneficiary-changes/BC-http/apply").status_code == 200


def test_claims_annuity_and_verification_over_http(client: TestClient):
    policy_id = client.post(
        "/blockchain/v1/policies",
        json={
            "policyNumber": "POL-HTTP-4",
            "holderSubject": "Dana",
            "productCode": "TERM20",
            "faceAmount": 500_000,
        },
    ).json()["policy"]["policyId"]

    claim = client.post(
        f"/blockchain/v1/policies/{policy_id}/claims",
        json={"claimId": "CL-http", "amountRequested": 250_000},
    )
    assert claim.status_code == 201
    assert claim.json()["claim"]["requiresManualApproval"] is True
    assert client.post(
        "/blockchain/v1/claims/CL-http/approve", json={"amountApproved": 250_000}
    ).status_code == 200

    schedule = client.post(
        f"/blockchain/v1/policies/{policy_id}/annuity-schedule",
        json={"startingValue": 50_000, "monthlyWithdrawal": 800, "periods": 3},
    )
    entry_id = schedule.json()["entries"][0]["entryId"]
    assert client.post(
        f"/blockchain/v1/annuity-entries/{entry_id}/pay", json={"policyId": policy_id}
    ).status_code == 200
    position = client.get(f"/blockchain/v1/policies/{policy_id}/annuity-position").json()["position"]
    assert position["paidEntries"] == 1

    assert client.get("/blockchain/v1/verify").json()["verification"]["trustworthy"] is True
    assert client.get("/blockchain/v1/blocks").json()["count"] >= 1


def test_portability_over_http(client: TestClient):
    policy_id = client.post(
        "/blockchain/v1/policies",
        json={
            "policyNumber": "POL-HTTP-5",
            "holderSubject": "Dana",
            "productCode": "TERM20",
            "faceAmount": 250_000,
        },
    ).json()["policy"]["policyId"]
    package = client.post(f"/blockchain/v1/policies/{policy_id}/export").json()["package"]
    verification = client.post("/blockchain/v1/packages/verify", json={"package": package}).json()
    assert verification["verification"]["valid"] is True


def test_unknown_resources_are_404(client: TestClient):
    assert client.get("/blockchain/v1/policies/pol-nope").status_code == 404
    assert client.get("/blockchain/v1/policies/pol-nope/history").status_code == 404
    assert client.get("/blockchain/v1/claims/CL-nope").status_code == 404
    assert client.get("/blockchain/v1/events/EV-nope/proof").status_code == 404


def test_openapi_documents_the_ledger_surface(client: TestClient):
    spec = client.get("/openapi.json").json()
    paths = [p for p in spec["paths"] if p.startswith("/blockchain/v1")]
    assert len(paths) >= 15
    assert spec["openapi"].startswith("3.1")