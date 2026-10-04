# Pricing

Three tiers, metered on the dimension that actually costs the vendor.

The guiding rule: **price the outcome, not the traffic.** A carrier does not care
that a valuation took 200ms; it cares that the close is done and reconciled. A
per-request price punishes a customer for exactly the automation they bought.

---

## Plans

| | **Developer** | **Team** | **Enterprise** |
|---|---|---|---|
| Monthly | $0 | $4,900 | from $22,000 |
| Plugins | 1 | all 7 | all 7 |
| Environments | 1 | 3 | unlimited |
| Tenants | 1 | 10 | unlimited |
| Requests / month | 100k | 5M | 25M, then $0.20 per 10k |
| **IFRS 17 valuation runs** | 5 | 250 | 2,000, then $18 each |
| **Submissions scored** | 500 | 25,000 | 200,000, then $0.60 each |
| **Policies written to the ledger** | 1,000 | 250,000 | 2M, then $0.05 each |
| Support | community | next business day | 4h response, named CSM |
| Uptime commitment | — | 99.5% | 99.9% |
| Sandbox partners | — | 2 | unlimited |

## What is metered, and why

**Valuation runs, not requests.** A quarterly close is a handful of calls that each
do the expensive work. Metering requests would charge almost nothing for the thing
the customer bought, and a great deal for the thing they did not. One run is one
cohort measured, disclosed and exported.

**Submissions scored, not submissions received.** Extraction and triage are cheap;
scoring is the marginal cost. Charging for received submissions would penalise a
carrier for routing *more* cases through the fast path — the opposite of what the
plugin is for.

**Ledger writes, separately.** Policy 7's anchoring and verification cost real work
per block, and it is the one plugin whose cost scales with the number of permanent
records created rather than with usage.

**Embedded distribution is not metered per quote.** A partner's quote volume is
their decision, and charging for it makes the partner resent the integration. The
charge lands on bound policies instead, which is the carrier's own cost.

## Why the metered dimensions are not uniform

Three of the seven plugins have a marginal cost that scales with something the
customer controls, and three do not.

- **Metered:** IFRS 17 (per run), AUW (per scored submission), Ledger (per write).
  Each consumes CPU in proportion to work done.
- **Subscription:** Gateway, Product Config, Data Mesh, Embedded Distribution. These
  are configuration and integration surfaces. Their cost is the support obligation,
  which scales with how many products, partners and pipelines are live — not with
  traffic.

Charging per request for a configuration surface would mean a customer who
configures a thousand products and sends ten requests pays more than one who
configures one product and sends ten thousand. That is backwards.

## Services

| Service | Price |
|---|---|
| PAS vendor adapter (new legacy vendor) | $18,000 one-off |
| Product migration onto Plugin 4 | from $12,000 per product family |
| Ledger integration with a Hyperledger Fabric network | from $45,000 |
| Distributed delivery partner referral | 15% of year one |

## What is not charged for

- **Sandbox and partner certification.** Getting a partner live is the hard part.
- **Contract updates.** Insurance changes constantly; a vendor who charges for
  amendments makes customers batch them, and batching is how defects ship.
- **Security advisories and incident response.**
- **Read-only access to your own audit trail.**

## Unit economics

An Enterprise tenant running a typical mid-size book generates roughly: 12
valuation runs, 60k scored submissions and 40k ledger writes per month. That is
about $59k of metered usage against a $22k+ platform fee, so metered overage is a
marginal cost rather than the business. The business is the platform fee plus the
integration work, which is also the part that does not scale down.

That ratio is deliberate. A vendor whose revenue is mostly overage is incentivised
towards customers whose bills grow when they succeed. Here the platform fee is
paid for the platform, and usage pays for the work.
