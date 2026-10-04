# Funding

## The problem, sized

Core policy administration software in the US life and P&C market is dominated by
systems built in the 1990s and 2000s: expensive to change, expensive to integrate,
and understood by a shrinking pool of people who can still maintain them. The
consensus is that the constraint is not appetite. 67% of carriers say they have
product ideas they cannot ship.

At the same time the industry has committed to reporting that changes what those
systems are allowed to do: IFRS 17 measurement, one-click and transparent
distribution, self-service data access, verifiable policy records.

**Carriers are being asked to modernise through the same integration surface they
are trying to escape.**

## What we are building

Seven independently deployable sidecars covering the seven places carriers spend
their modernization budget: policy administration, IFRS 17 valuation, accelerated
underwriting, product configuration, embedded distribution, data quality and
policy records.

Not a platform to replace everything. A set of components a carrier adopts one at a
time, each of which works against the legacy systems they already run.

## Why this shape wins

**Land with one problem, expand from there.** A carrier that only needs IFRS 17
does not have to buy a data mesh. The first deployment is a single plugin against
their existing core — the migration risk is one module, not a platform.

**The legacy system stays.** Plugin 1's translation layer is the reason adoption
is possible. A carrier can replace policy administration without replacing the
system underneath it, which is the only way a 25-year-old core gets replaced at all.

**Each plugin is a standalone P&L.** Seven independent products mean seven chances
for one to compound, and a failed one does not take the other six down.

## Why now

Three things changed recently and all three are irreversible:

**IFRS 17 forced the measurement layer to be rebuilt anyway.** Carriers are
replacing calculation code they were not planning to replace. That is a live budget
and a live willingness to change vendors.

**Distribution moved to platforms carriers do not control.** Embedding in an
aggregator's app is now how a large share of small-case business is written, and
incumbent point-of-sale platforms cannot be reached from outside. The integration
demand is being created by the market, not by us.

**The buyer's decision unit changed.** Product and actuarial leadership now own
modernisation decisions that used to sit with IT. A component with a business-case
shape gets funded differently from a platform programme.

## Unit economics

Enterprise pricing is a $22k platform fee plus metered usage. A typical mid-size
carrier generates about $59k of metered usage monthly, and integration is a
one-off $12k–45k depending on scope.

Gross margin is high because the product is inference and configuration rather
than data centres. The costs that matter are support and integration engineering,
which is why the plan is built around a named CSM rather than around seats.

## The ask

**$3.5M seed** for eighteen months:

| | |
|---|---|
| Engineering (6 → 14) | $1.95M |
| Integration and professional services capacity | $0.60M |
| Compliance, security certification, SOC 2 Type II | $0.35M |
| Go-to-market | $0.40M |
| Infrastructure and run-rate | $0.20M |

## Milestones this buys

**Month 6 — first two carrier contracts, one IFRS 17 and one embedded
distribution.** Both prove the thesis in the only way that counts: a production
carrier running it against a real core.

**Month 12 — IFRS 17 in production across three carriers, SOC 2 Type II, and the
ledger deployed against a real Hyperledger Fabric network.** The ledger is the
credibility piece; it is also the one nobody else has.

**Month 18 — $2M ARR, seven of seven plugins in production somewhere, and a
migration reference that shortens the next sale from nine months to four.** The
reference is the compounding asset. Every carrier in this market asks the same
first question — who has done this before — and the answer is currently nobody.

## What we are not claiming

This is a production-shaped suite that has not yet run in production. The
engineering is real, the tests are real, and every plugin states plainly in its
docstring where a simulator stands in for a licensed vendor, a real chain or an
actuarial basis.

The honest risk is not the technology. It is that carriers take eighteen months to
decide anything, which is why the first two customers matter more than the pipeline
that follows them.
