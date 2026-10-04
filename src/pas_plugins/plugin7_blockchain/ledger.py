"""The ledger: hash chain, Merkle tree, endorsement policy and anchoring.

This module implements the cryptography and the permissioning. It is deliberately
small and auditable, because the value of a tamper-evident record depends on a
reviewer being able to read the whole mechanism.

Four primitives:

* **Hash chain.** `block_hash = H(previous_hash | data_hash)`. Change any event
  and every subsequent block hash changes, so `verify_chain()` detects it.
* **Merkle tree.** Real binary tree over transaction payload hashes, computed
  pairwise with the standard odd-node duplication rule.
* **Endorsement policy.** `ALL`, `MAJORITY`, `ANY`, or an explicit organisation
  list. A transaction is only committable once satisfied - this is what makes the
  network permissioned rather than merely append-only.
* **Anchoring.** Every N blocks, the Merkle root of the chain so far is published
  as an external receipt. This is the defence against an operator who can rewrite
  the *entire* local chain: they would also have to rewrite what was published
  elsewhere.

Cryptographic signatures use Ed25519 from `cryptography`. That library is the only
place a real secret is touched, and the private keys never leave it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pas_plugins.plugin7_blockchain.models import (
    Block,
    ChainVerification,
    Channel,
    Endorsement,
    OrgRole,
    Organization,
    PolicyEvent,
    Transaction,
    TransactionStatus,
)
from pas_plugins.plugin7_blockchain.settings import Plugin7Settings

GENESIS_HASH = "0" * 64


def canonical(payload: Any) -> bytes:
    """Byte-stable serialisation.

    Sorted keys and no whitespace, so two structurally equal payloads always
    hash the same. Without this, a hash chain's integrity depends on dict ordering
    that has nothing to do with the data.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()


def sha256_hex(payload: Any) -> str:
    return hashlib.sha256(canonical(payload)).hexdigest()


def merkle_root(leaves: Iterable[str]) -> str:
    """Merkle root over hex leaf hashes, pairing and duplicating an odd node."""
    level = [bytes.fromhex(leaf) for leaf in leaves]
    if not level:
        return hashlib.sha256(b"").hexdigest()
    while len(level) > 1:
        if len(level) % 2 == 1:
            # Duplicate the last node, as Bitcoin and Fabric both do.
            level.append(level[-1])
        level = [
            hashlib.sha256(level[i] + level[i + 1]).digest() for i in range(0, len(level), 2)
        ]
    return level[0].hex()


def merkle_proof(leaves: list[str], index: int) -> list[tuple[str, bool]]:
    """Inclusion proof for one leaf.

    Each step returns the sibling hash and whether that sibling sits to the *left*
    of the running node, which is what lets a verifier rebuild the tree without
    guessing the pairing order.
    """
    if index < 0 or index >= len(leaves):
        msg = f"leaf index {index} is out of range for {len(leaves)} leaves"
        raise IndexError(msg)
    level = [bytes.fromhex(leaf) for leaf in leaves]
    proof: list[tuple[str, bool]] = []
    position = index
    while len(level) > 1:
        if len(level) % 2 == 1:
            # Duplicate the last node, matching merkle_root exactly.
            level.append(level[-1])
        sibling_index = position ^ 1
        proof.append((level[sibling_index].hex(), sibling_index < position))
        level = [
            hashlib.sha256(level[i] + level[i + 1]).digest() for i in range(0, len(level), 2)
        ]
        position //= 2
    return proof


def verify_merkle_proof(leaf: str, proof: list[tuple[str, bool]], root: str) -> bool:
    """Recompute the root from a leaf and its proof.

    The pairing order is left-then-right with no sorting, so verification must use
    the `sibling_is_left` flag from the proof rather than guessing.
    """
    current = bytes.fromhex(leaf)
    for sibling_hex, sibling_is_left in proof:
        sibling = bytes.fromhex(sibling_hex)
        current = (
            hashlib.sha256(sibling + current).digest()
            if sibling_is_left
            else hashlib.sha256(current + sibling).digest()
        )
    return current.hex() == root


class EndorsementPolicy:
    """Resolves an endorsement policy string into required organisations."""

    def __init__(self, policy: str, channel_orgs: list[str]) -> None:
        self._policy = (policy or "MAJORITY").strip()
        self._orgs = sorted(channel_orgs)

    @property
    def description(self) -> str:
        return self._policy

    def required_orgs(self) -> list[str]:
        """Which organisations must endorse for a transaction to commit."""
        if self._policy.upper() == "ALL":
            return list(self._orgs)
        if self._policy.upper() == "ANY":
            return self._orgs[:1]
        if self._policy.upper() == "MAJORITY":
            needed = max(1, (len(self._orgs) + 1) // 2)
            return self._orgs[:needed]
        # An explicit comma-separated org list.
        explicit = [org.strip() for org in self._policy.split(",") if org.strip()]
        unknown = [org for org in explicit if org not in self._orgs]
        if unknown:
            msg = f"endorsement policy names organisations not on the channel: {unknown}"
            raise ValueError(msg)
        return explicit

    def satisfied_by(self, endorsing_orgs: set[str]) -> bool:
        required = set(self.required_orgs())
        return required.issubset(endorsing_orgs)

    def explain(self, endorsing_orgs: set[str]) -> str:
        required = self.required_orgs()
        missing = [org for org in required if org not in endorsing_orgs]
        if not missing:
            return f"satisfied: {', '.join(required)}"
        return f"policy '{self._policy}' requires {required}; still missing {missing}"


class SigningKeyStore:
    """Ed25519 keypairs per subject, held only in memory.

    A real deployment keeps these in a hardware security module or a Fabric MSP;
    an in-process store is honest for a reference implementation and is the seam
    where that swap belongs.
    """

    def __init__(self) -> None:
        self._private: dict[str, Any] = {}
        self._public: dict[str, bytes] = {}

    def generate(self, subject: str) -> str:
        """Create a key for a subject and return its public key (base58-style, hex)."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: PLC0415
            Ed25519PrivateKey,
        )

        key = Ed25519PrivateKey.generate()
        self._private[subject] = key
        self._public[subject] = key.public_key().public_bytes_raw()
        return self._public[subject].hex()

    def public_key(self, subject: str) -> str | None:
        raw = self._public.get(subject)
        return raw.hex() if raw else None

    def subjects(self) -> list[str]:
        """Every subject holding a key in this store."""
        return sorted(self._private)

    def sign(self, subject: str, message: bytes) -> str:
        key = self._private.get(subject)
        if key is None:
            msg = f"no signing key for subject '{subject}'"
            raise KeyError(msg)
        return base64.b64encode(key.sign(message)).decode()

    def verify(self, public_key_hex: str, message: bytes, signature_b64: str) -> bool:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: PLC0415
            Ed25519PublicKey,
        )

        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex)).verify(
                base64.b64decode(signature_b64), message
            )
        except Exception:
            return False
        return True


class AnchorBackend(Protocol):
    """Where chain roots get published outside this process."""

    def publish(self, channel_id: str, block_number: int, root: str) -> str: ...


class DeterministicAnchor:
    """A simulated external anchor.

    Fabric's `configtxlator` and public anchor services behave the same way from
    the ledger's point of view: given a root, hand back a receipt that somebody
    else is holding. This one derives the receipt from the root, so a rewritten
    chain produces a different receipt and `verify_chain()` notices.
    """

    def __init__(self, network: str = "pas-anchor-sim") -> None:
        self._network = network
        self._published: dict[tuple[str, int], str] = {}

    def publish(self, channel_id: str, block_number: int, root: str) -> str:
        digest = hashlib.sha256(f"{self._network}|{channel_id}|{block_number}|{root}".encode())
        receipt = f"anchor:{self._network}:{digest.hexdigest()[:32]}"
        self._published[(channel_id, block_number)] = receipt
        return receipt

    def expected_receipt(self, channel_id: str, block_number: int, root: str) -> str:
        digest = hashlib.sha256(f"{self._network}|{channel_id}|{block_number}|{root}".encode())
        return f"anchor:{self._network}:{digest.hexdigest()[:32]}"

    def is_published(self, channel_id: str, block_number: int, receipt: str) -> bool:
        return self._published.get((channel_id, block_number)) == receipt


@dataclass(slots=True)
class Ledger:
    """The append-only chain: blocks, transactions, events and organisations."""

    settings: Plugin7Settings = field(default_factory=Plugin7Settings)
    organizations: dict[str, Organization] = field(default_factory=dict)
    channels: dict[str, Channel] = field(default_factory=dict)
    blocks: list[Block] = field(default_factory=list)
    transactions: dict[str, Transaction] = field(default_factory=dict)
    events: list[PolicyEvent] = field(default_factory=list)
    keystore: SigningKeyStore = field(default_factory=SigningKeyStore)
    anchor: AnchorBackend = field(default_factory=DeterministicAnchor)

    # -- membership --------------------------------------------------------

    def add_organization(self, org: Organization) -> Organization:
        """Admit an organisation to the network."""
        self.organizations[org.org_id] = org
        return org

    def create_channel(self, channel: Channel) -> Channel:
        """Create a channel. Its member list defines its endorsement policy scope."""
        unknown = [org for org in channel.member_orgs if org not in self.organizations]
        if unknown:
            msg = f"channel '{channel.name}' names unknown organisations: {unknown}"
            raise ValueError(msg)
        if not channel.member_orgs:
            msg = f"channel '{channel.name}' has no member organisations"
            raise ValueError(msg)
        # Validate the policy eagerly: a channel that cannot resolve its own
        # endorsement policy would fail at the first transaction instead.
        EndorsementPolicy(channel.endorsement_policy, channel.member_orgs).required_orgs()
        self.channels[channel.channel_id] = channel
        return channel

    def policy_for(self, channel_id: str) -> EndorsementPolicy:
        channel = self.channels.get(channel_id)
        if channel is None:
            msg = f"unknown channel '{channel_id}'"
            raise KeyError(msg)
        return EndorsementPolicy(channel.endorsement_policy, channel.member_orgs)

    def org_role(self, org_id: str) -> OrgRole:
        org = self.organizations.get(org_id)
        if org is None:
            msg = f"unknown organisation '{org_id}'"
            raise KeyError(msg)
        if not org.active:
            msg = f"organisation '{org_id}' is not active"
            raise KeyError(msg)
        return org.role

    # -- endorsement -------------------------------------------------------

    def endorse(
        self, tx: Transaction, org_id: str, *, peer_id: str = "", skip_role_check: bool = False
    ) -> Endorsement:
        """Record an organisation's signature on a transaction's payload.

        Every endorsement signs the *payload hash*, not the transaction id. That
        means a signature cannot be lifted onto a different transaction with the
        same id, which is the substitution attack a naive design permits.
        """
        if not skip_role_check:
            role = self.org_role(org_id)
            if not role.can_endorse_policy_state:
                msg = f"organisation role '{role}' may not endorse policy state changes"
                raise PermissionError(msg)
        if org_id not in self.channels[tx.channel_id].member_orgs:
            msg = f"organisation '{org_id}' is not a member of channel '{tx.channel_id}'"
            raise PermissionError(msg)
        if any(e.org_id == org_id for e in tx.endorsements):
            msg = f"organisation '{org_id}' has already endorsed this transaction"
            raise ValueError(msg)

        signature = self.keystore.sign(org_id, bytes.fromhex(tx.payload_hash))
        endorsement = Endorsement(
            org_id=org_id,
            peer_id=peer_id or f"{org_id}-peer0",
            signature=signature,
            signed_payload_hash=tx.payload_hash,
        )
        tx.endorsements.append(endorsement)
        return endorsement

    def endorsement_is_valid(self, tx: Transaction, endorsement: Endorsement) -> bool:
        """Verify an endorsement signature against the endorsement's public key."""
        public_key = self.keystore.public_key(endorsement.org_id)
        if public_key is None:
            return False
        if endorsement.signed_payload_hash != tx.payload_hash:
            return False
        return self.keystore.verify(public_key, bytes.fromhex(tx.payload_hash), endorsement.signature)

    # -- blocks ------------------------------------------------------------

    def current_block(self, channel_id: str) -> Block | None:
        channel_blocks = [b for b in self.blocks if b.channel_id == channel_id]
        return channel_blocks[-1] if channel_blocks else None

    def pending_transactions(self, channel_id: str) -> list[Transaction]:
        """Transactions proposed but not yet endorsed by their required orgs."""
        return [
            tx
            for tx in self.transactions.values()
            if tx.channel_id == channel_id
            and not tx.status.is_committed
            and not tx.is_endorsed
        ]

    def sealable_transactions(self, channel_id: str) -> list[Transaction]:
        """Endorsed transactions that are not yet in a block."""
        return [
            tx
            for tx in self.transactions.values()
            if tx.channel_id == channel_id and tx.is_endorsed and tx.block_number is None
        ]

    def seal_block(self, channel_id: str, *, force: bool = False) -> Block | None:
        """Seal endorsed transactions into a block.

        Returns None when there is nothing to seal. Blocks are only ever appended,
        and each commits to the previous block's hash.
        """
        pending = self.sealable_transactions(channel_id)
        if not pending:
            if not force:
                return None
            pending = []
        if len(pending) > self.settings.max_transactions_per_block:
            pending = pending[: self.settings.max_transactions_per_block]

        channel_blocks = [b for b in self.blocks if b.channel_id == channel_id]
        previous_hash = channel_blocks[-1].block_hash if channel_blocks else GENESIS_HASH
        block_number = len(channel_blocks)

        leaves = [tx.payload_hash for tx in pending]
        root = merkle_root(leaves)
        data_hash = sha256_hex({"txIds": [tx.tx_id for tx in pending], "merkleRoot": root})
        block_hash = sha256_hex(
            {
                "blockNumber": block_number,
                "channelId": channel_id,
                "previousHash": previous_hash,
                "dataHash": data_hash,
                "merkleRoot": root,
            }
        )
        block = Block(
            block_number=block_number,
            channel_id=channel_id,
            previous_hash=previous_hash,
            merkle_root=root,
            data_hash=data_hash,
            block_hash=block_hash,
            tx_ids=[tx.tx_id for tx in pending],
            tx_count=len(pending),
        )
        for tx in pending:
            tx.status = TransactionStatus.VALID
            tx.block_number = block_number

        self.blocks.append(block)

        if (block_number + 1) % self.settings.anchor_every_blocks == 0:
            chain_root = self.chain_merkle_root(channel_id)
            receipt = self.anchor.publish(channel_id, block_number, chain_root)
            block.anchor_receipt = receipt

        return block

    def chain_merkle_root(self, channel_id: str) -> str:
        """Merkle root over every block hash in the channel.

        This is what gets anchored: a single commitment to the entire history, so
        publishing it periodically detects a rewrite of anything at all.
        """
        channel_blocks = [b for b in self.blocks if b.channel_id == channel_id]
        return merkle_root([b.block_hash for b in channel_blocks])

    # -- verification ------------------------------------------------------

    def verify_chain(self, channel_id: str) -> ChainVerification:
        """Audit the whole channel: block links, Merkle roots, event hashes, anchors."""
        result = ChainVerification(channel_id=channel_id)
        channel_blocks = [b for b in self.blocks if b.channel_id == channel_id]
        channel_events = [e for e in self.events if e.policy_id]
        result.blocks_checked = len(channel_blocks)
        result.events_checked = len(channel_events)

        previous_hash = GENESIS_HASH
        for block in channel_blocks:
            if block.previous_hash != previous_hash:
                result.chain_intact = False
                result.problems.append(
                    f"block {block.block_number} claims previous hash "
                    f"{block.previous_hash[:12]} but the chain is at {previous_hash[:12]}"
                )
            leaves = [
                self.transactions[tx_id].payload_hash
                for tx_id in block.tx_ids
                if tx_id in self.transactions
            ]
            if merkle_root(leaves) != block.merkle_root:
                result.merkle_roots_valid = False
                result.problems.append(
                    f"block {block.block_number} Merkle root does not match its transactions"
                )
            recomputed = sha256_hex(
                {
                    "blockNumber": block.block_number,
                    "channelId": block.channel_id,
                    "previousHash": block.previous_hash,
                    "dataHash": block.data_hash,
                    "merkleRoot": block.merkle_root,
                }
            )
            if recomputed != block.block_hash:
                result.chain_intact = False
                result.problems.append(f"block {block.block_number} hash does not match its content")
            if block.anchor_receipt:
                expected = getattr(self.anchor, "expected_receipt", None)
                if callable(expected):
                    # The anchor was published against the chain root *as of that
                    # block*, not the final one. Recomputing over every block would
                    # make every anchor look invalid the moment another block lands.
                    as_of_root = merkle_root(
                        [b.block_hash for b in channel_blocks[: block.block_number + 1]]
                    )
                    if expected(channel_id, block.block_number, as_of_root) != block.anchor_receipt:
                        result.anchors_valid = False
                        result.problems.append(
                            f"block {block.block_number} anchor receipt does not match its content"
                        )
            previous_hash = block.block_hash

        result.event_hashes_valid = self.verify_event_chain()
        if not result.event_hashes_valid:
            result.problems.append("one or more policy event hashes do not match their content")

        return result

    def verify_event_chain(self) -> bool:
        """Verify each policy's own event chain, independently of blocks."""
        by_policy: dict[str, list[PolicyEvent]] = {}
        for event in self.events:
            by_policy.setdefault(event.policy_id, []).append(event)

        for policy_id, events in by_policy.items():
            events = sorted(events, key=lambda e: e.sequence)
            previous = GENESIS_HASH
            for event in events:
                if event.previous_event_hash != previous:
                    return False
                if self.event_hash_for(event) != event.event_hash:
                    return False
                previous = event.event_hash
        return True

    @staticmethod
    def event_hash_for(event: PolicyEvent) -> str:
        """Recompute an event hash from its content.

        Deliberately excludes the block number: a block can be re-sealed without
        the event itself changing, and the event chain should survive that.
        """
        return sha256_hex(
            {
                "eventId": event.event_id,
                "policyId": event.policy_id,
                "sequence": event.sequence,
                "eventType": str(event.event_type),
                "statusBefore": str(event.status_before),
                "statusAfter": str(event.status_after),
                "payload": event.payload,
                "previousEventHash": event.previous_event_hash,
                "actorOrg": event.actor_org,
                "actorSubject": event.actor_subject,
            }
        )

    def events_for(self, policy_id: str) -> list[PolicyEvent]:
        return sorted(
            (e for e in self.events if e.policy_id == policy_id),
            key=lambda e: e.sequence,
        )


def new_event_id(policy_id: str, sequence: int, nonce: str | None = None) -> str:
    """Deterministic-ish unique event id.

    A random suffix would make ids non-reproducible across a portability import,
    so the id is derived from the policy, the sequence and a caller-supplied or
    generated nonce.
    """
    token = nonce or secrets.token_hex(6)
    return f"EV-{policy_id}-{sequence:04d}-{token[:8]}"


__all__ = [
    "AnchorBackend",
    "DeterministicAnchor",
    "EndorsementPolicy",
    "GENESIS_HASH",
    "Ledger",
    "SigningKeyStore",
    "canonical",
    "merkle_proof",
    "merkle_root",
    "new_event_id",
    "sha256_hex",
    "verify_merkle_proof",
]