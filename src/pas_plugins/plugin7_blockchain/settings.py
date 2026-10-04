"""Plugin 7 - Blockchain-Based Policy Lifecycle Layer.

Policy records fragment. A policy is issued by one system, amended by another,
serviced by a third, and when the carrier migrates platforms the beneficiary
history is the thing that gets lost. The result is slow beneficiary updates,
unreconstructable annuity positions, and an assurance file that cannot be trusted
because nobody can prove what it once said.

This plugin makes the policy record **append-only, hash-chained, and jointly
endorsed**, and puts an SSI layer under the people in it.

### What this is, precisely

The ledger here implements the parts of a permissioned blockchain that actually
matter for a policy record, and it is worth being exact about which parts those
are:

* **A real hash chain.** Every block commits to its predecessor. Altering any
  earlier event invalidates every hash after it, and `verify_chain()` says so
  rather than trusting the data.
* **A real Merkle tree** over transactions, computed per block.
* **A real endorsement policy.** A transaction is only committed once the
  organisations its policy names have endorsed it. This is what makes it
  *permissioned*: the carrier cannot quietly rewrite a beneficiary change on its
  own.
* **Real digital signatures** (Ed25519) on DID documents and verifiable
  credentials.

And what it is **not**:

* It does not talk to Hyperledger Fabric. Fabric is the deployment target; this is
  the chaincode semantics and the query layer, with a seam
  (`LedgerBackend`) where a Fabric gateway plugs in.
* It does not achieve consensus by proof of work. A permissioned network with
  named endorsers does not need one, and pretending otherwise would be a worse
  design than the real thing.

That distinction is the point. What a carrier is actually buying is
*tamper-evidence and joint endorsement*, not consensus over a public network.

### The four capabilities

1. **Policy lifecycle** - issue, modify, lapse, reinstate, terminate, claim, each
   an event with a typed payload and a legal state transition.
2. **Beneficiary change with multi-signature** - no single party, including the
   policyholder, can change a beneficiary alone. Both the policyholder's and the
   carrier's endorsements are required, and both are recorded forever.
3. **Annuity payout schedule** - an auditable drawdown schedule whose every
   payment is a ledger event, so an annuity position can be reconstructed.
4. **Digital identity** - W3C-style DIDs and verifiable credentials for
   beneficiaries, so a beneficiary can present a credential without the carrier
   holding their identity documents.

Plus **cross-vendor continuity**: a signed, verifiable export of a policy's whole
history that another carrier can import and verify.
"""

from __future__ import annotations

from pas_core.config import PluginSettings


class Plugin7Settings(PluginSettings):
    """Configuration for the policy lifecycle ledger."""

    plugin_id: str = "plugin7"
    plugin_version: str = "1.0.0"
    service_name: str = "pas-policy-ledger"

    channel_name: str = "policy-lifecycle"
    block_interval_events: int = 10
    """How many transactions accumulate before a block is sealed."""
    max_transactions_per_block: int = 500
    anchor_every_blocks: int = 5
    """Seal an external anchor receipt every N blocks so tampering is detectable
    even by an operator who can rewrite the whole local chain."""
    beneficiary_signatories_required: int = 2
    """Distinct endorsers needed to change a beneficiary."""
    claim_approval_threshold_bps: int = 0
    """Minimum share of claim amount payable without manual approval."""
    require_claim_approval_above: float = 50_000.0
    """Claims above this amount need an explicit approval endorsement."""
    token_max_supply: int = 1_000_000
    token_transfer_fee_bps: int = 10
    documentation_url: str = "https://docs.pas-plugins.io/plugins/policy-ledger"


__all__ = ["Plugin7Settings"]