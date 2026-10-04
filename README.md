# PAS Plugins

Seven independently deployable insurance platform sidecars, plus the shared runtime
they are built on. Each plugin solves one operational problem, ships a REST API, an
MCP tool surface, an OpenAPI 3.1 contract and a Svelte administration UI, and is
released on its own schedule.

**527 tests passing. 219 REST operations across 7 plugins. 87 MCP tools. One image,
seven entry points.**

---

## Why seven plugins and not one platform

Because they fail differently, change differently and are bought differently.

A carrier that only needs to fix its IFRS 17 close should not have to take a
dependency on a blockchain ledger, and should not have to accept a data-mesh release
to get a valuation fix. Independent deployment means a plugin can be upgraded,
rolled back or dropped without touching the others, and means the blast radius of a
bad release is one problem area rather than the whole book.

What they share is `pas_core`: tenancy, OAuth, audit, rate limiting, RFC 9457
errors, metrics and the MCP bridge. That shared runtime is where consistency is
enforced, so it is tested once rather than seven times.

---

## The plugins

| # | Plugin | Problem it attacks | Ops | MCP | Tests |
|---|--------|--------------------|-----|-----|-------|
| 1 | [PAS Gateway](src/pas_plugins/plugin1_gateway/) | Legacy policy systems are expensive to change; one definition drives REST, MCP and the operation catalogue | 44 | 28 | 87 |
| 2 | [IFRS 17 Valuation](src/pas_plugins/plugin2_ifrs17/) | IFRS 17 measurement, assumption governance and reconciled disclosure | 26 | 9 | 70 |
| 3 | [AUW Workbench](src/pas_plugins/plugin3_auw/) | Underwriting cycle time, with explainable ML and audited overrides | 25 | 11 | 70 |
| 4 | [Product Config](src/pas_plugins/plugin4_productconfig/) | Product configuration is a bottleneck because a product means code | 23 | 12 | 85 |
| 5 | [Embedded Distribution](src/pas_plugins/plugin5_embedded/) | Distribution still runs on phone calls and PDFs; partners cannot integrate | 26 | 14 | 72 |
| 6 | [Data Mesh](src/pas_plugins/plugin6_datamesh/) | Data quality, survivorship, lineage and a versioned feature store | 34 | 11 | 63 |
| 7 | [Policy Ledger](src/pas_plugins/plugin7_blockchain/) | Fragmented policy records, slow beneficiary changes, unreconstructable annuities | 36 | 13 | 80 |

---

## Quick start

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows
pip install -e ".[dev]"

pytest -q                                        # 527 tests
python scripts/export_openapi.py --validate      # 7 specs, 219 operations
python scripts/generate_postman.py               # 219 requests
python scripts/smoke_plugins.py                  # every app imports and serves

uvicorn pas_plugins.plugin3_auw.main:app --port 8003
```

Every plugin runs the same way; only the module and port differ. All seven at once:

```bash
docker compose -f deploy/docker-compose.yml up --build
```

---

## Design commitments

These are the decisions that shape the code, stated so they can be argued with.

**Nothing is authoritative that is not hashed.** Plugin 7's ledger is a real hash
chain with real Merkle roots and real Ed25519 signatures. It is not a Fabric
gateway — Fabric is the deployment target and `LedgerBackend` is the seam. It does
not use proof of work, because a permissioned network with named endorsers does not
need one. What a carrier buys is tamper-evidence and joint endorsement, and the
code does not pretend otherwise.

**Every automated decision carries its basis.** Plugin 3's SHAP values are exact —
the champion is an additive model, so each tree's output *is* its Shapley
contribution, and a test asserts the attributions reconstruct the model's output to
1e-9. Plugin 4's guardrails each carry a remediation string. Plugin 5's suitability
assessment states the ratio that triggered it. A decision nobody can explain is a
decision nobody can defend.

**Money is never a ledger of record.** Plugin 5 records that money was *requested*
and separately that it *arrived*, and leaves the carrier's finance system
authoritative. Plugin 2 measures; it does not post.

**Absence is not a negative.** Missing consent in Plugin 3 produces an explicit
consent gap that routes to a human, never an assumed "clear". A missing rating band
fails loudly rather than falling back to a default rate.

**Guardrails that block everything get switched off.** Plugin 4's findings carry a
severity, and a warning does not block publication — but whoever accepted it is
recorded on the published version.

---

## Repository layout

```
src/pas_core/          shared runtime: tenancy, auth, audit, MCP, PAS adapters, ACORD
src/pas_plugins/       plugin1..plugin7
ui/                    seven generated Svelte + TypeScript admin UIs
contracts/openapi/     generated OpenAPI 3.1 (committed, CI checks freshness)
contracts/postman/     generated Postman collections
deploy/                Dockerfile, compose, Helm chart, Terraform
scripts/               export_openapi, generate_postman, generate_ui, smoke_plugins
tests/                 527 tests across all plugins
docs/                  architecture, ADRs, C4, pricing
```

## Regenerating contracts and UIs

Everything generated is committed and verified in CI, so a stale artefact fails the
build rather than misleading a consumer:

```bash
python scripts/export_openapi.py   # contracts/openapi
python scripts/generate_postman.py # contracts/postman
python scripts/generate_ui.py      # ui/
```

## Documentation

- [Architecture](docs/architecture.md) and [C4 context](docs/c4-context.md)
- [Decisions](docs/adr/) — why seven sidecars, why hash chains, why no ledger of record
- [Pricing](docs/pricing.md) and the [funding pitch](docs/funding-pitch.md)
- [Operations](docs/operations.md) — running, upgrading and troubleshooting

## Status

Production-shaped, not production-proven. Every plugin is complete and tested
end to end, and each one says plainly in its module docstring where the
implementation is a simulator standing in for a licensed vendor, a real chain, or an
actuarial basis. Those boundaries are documented rather than hidden, because the
place a demo overstates itself is the place a buyer discovers it was misled.
