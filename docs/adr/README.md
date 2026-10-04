# Architecture decision records

Six decisions that shaped the codebase, stated with what was rejected and why.

---

## ADR-001: Seven plugins, not one platform

**Status:** accepted

**Context.** The natural alternative is one codebase with seven modules.

**Decision.** Seven independently deployable sidecars over a shared `pas_core`
library.

**Consequences.** Each plugin releases, scales, rolls back and fails
independently. The cost is seven deployments, seven spec files and seven CI matrix
entries, and cross-plugin work needs an explicit integration rather than an import.

**Rejected.** One platform: a bad release anywhere rolls back the valuation engine
too, and every buyer takes a dependency on the whole suite. A carrier that wants a
product configurator should not inherit a blockchain ledger's CVEs.

---

## ADR-002: A hash chain is not a blockchain, and the code says so

**Status:** accepted

**Context.** Plugin 7's brief names Hyperledger Fabric. Fabric is not available in
this environment, and pretending otherwise would mean shipping a mock that claims to
be a distributed ledger.

**Decision.** Implement the properties that actually protect a policy record — a
real hash chain, real Merkle roots, real Ed25519 signatures, a real endorsement
policy, real external anchoring — and state plainly in the module docstring that
this is not a Fabric gateway and does not use proof of work. `LedgerBackend` is the
seam a Fabric client plugs into.

**Consequences.** The tamper-evidence and joint-endorsement guarantees are real and
tested. Consensus is delegated to the deployment target, which for a permissioned
network with named endorsers is the right place for it.

**Rejected.** A mock chain that emits fake block hashes; a claim of full Fabric
compatibility. Both mislead whoever has to rely on the guarantee.

---

## ADR-003: No plugin is a ledger of record

**Status:** accepted

**Context.** Plugin 5 takes payments and Plugin 2 measures liabilities. Both touch
money, and both could accumulate enough state to look like a system of record.

**Decision.** Neither does. Plugin 5 distinguishes a *payment intent* (money
requested) from a *payment* (money arrived), and `reconcile()` exists so a partner
and a carrier compare against the same figures rather than each guessing. Plugin 2
measures and discloses; posting is finance's job.

**Consequences.** An integration cannot treat either plugin as authoritative for
cash or reserves, and must not. In exchange neither can become a single point of
failure for the carrier's money.

---

## ADR-004: Generated contracts, committed and checked

**Status:** accepted

**Context.** OpenAPI documents are usually written by hand and go stale silently.

**Decision.** Every plugin's OpenAPI document is generated from the running
application and committed. `scripts/export_openapi.py --check` regenerates in memory
and fails CI on drift. Postman collections and UI sources are generated from the same
definitions.

**Consequences.** A spec can never describe what the code used to do. The cost is
that hand-editing a spec is not possible — deliberately.

---

## ADR-005: Exact SHAP over approximate explanations

**Status:** accepted

**Context.** Plugin 3 must explain its risk score. KernelSHAP is the usual answer
and is expensive, approximate, and adds a second thing that can be wrong.

**Decision.** Make the champion an additive stump ensemble, so each tree's output
*is* its exact Shapley contribution. A test asserts `base_logit + Σ attributions ==
raw_output` to 1e-9. The challenger is L2 logistic regression, exact in log-odds
space — which is also the baseline a regulator expects to see.

**Consequences.** Attributions reconstruct the model's output exactly and cost no
extra computation. In exchange the champion's functional form is constrained, which
is a real limitation — so the challenger exists, and the monitoring module reports
when it beats the champion.

---

## ADR-006: Refuse rather than default

**Status:** accepted

**Context.** Several places in this codebase could plausibly pick a default when
data is missing: a gap in the age bands, a missing consent flag, an absent KYC
result, an expired quote.

**Decision.** Each refuses loudly and says why. A missing rating band raises rather
than falling back to a nearby rate; missing consent produces an explicit consent gap
that routes to a human rather than an assumed "clear"; an expired quote is not
bindable whatever its status field says; binding without a KYC result is refused.

**Consequences.** Some requests fail that a laxer system would have served. Each one
that fails would otherwise have been a wrong price, a mispriced risk, or a policy
issued to someone whose identity was never confirmed.

**Rejected.** Sensible defaults. A default is only sensible when the fallback is as
good as the answer, and in each of these cases it is not.
