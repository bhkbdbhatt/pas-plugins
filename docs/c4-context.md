# C4 — System context

## Level 1: System landscape

```
┌───────────────────────────────────────────────────────────────────────────────┐
│                          Insurance carrier                                     │
│                                                                               │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐        │
│  │ PAS core │  │ PAS core │  │ PAS core │  │ PAS core │  │ PAS core │        │
│  │   (IFRS  │  │  (AUW)   │  │ (Product │  │  (Distrib│  │ (Ledger) │        │
│  │   17)    │  │          │  │  config) │  │  ution)  │  │          │        │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘        │
│       │             │             │             │             │              │
└───────┼─────────────┼─────────────┼─────────────┼─────────────┼──────────────┘
        │             │             │             │             │
        └─────────────┴──────┬──────┴─────────────┴─────────────┘
                             │
              ┌──────────────┴──────────────┐
              │                             │
        ┌─────▼──────┐               ┌──────▼─────────┐
        │ Legacy PAS │               │ Data platform   │
        │ (Majesco,  │               │ (warehouse,     │
        │  OIPA, EIS) │               │  lakehouse)     │
        └────────────┘               └─────────────────┘

        External actors                External systems
        ┌────────────────┐             ┌────────────────────┐
        │ Underwriters   │             │ Identity provider  │
        │ Distribution   │             │ (OIDC / OAuth 2.1) │
        │   partners     │             └────────────────────┘
        │ Consumers      │             ┌────────────────────┐
        │ Regulators     │             │ Payment rails      │
        │ Service desks  │             └────────────────────┘
        └────────────────┘             ┌────────────────────┐
                                       │ PAS adapters       │
                                       │ (translation layer)│
                                       └────────────────────┘
```

## Level 2: Containers

```
                        ┌──────────────────────────────────────┐
                        │         Identity provider           │
                        │      OIDC issuer, JWKS endpoint     │
                        └──────────────────┬───────────────────┘
                                           │ JWT
    ┌──────────────┬──────────────┬─────────▼────────┬──────────────┐
    │              │              │                  │              │
┌───▼──────────────▼───┐    ┌─────▼────────┐   ┌─────▼──────┐  ┌────▼──────┐
│  pas_core (library)  │    │  plugin1    │   │  plugin3   │  │  plugin5  │
│                      │    │  PAS        │   │  AUW       │  │  Distrib  │
│  • tenancy           │◄──►│  Gateway    │   │  Workbench │  │  ution    │
│  • OAuth 2.1 / OIDC  │    │             │   │            │  │           │
│  • audit chain       │    │  44 ops     │   │  25 ops    │  │  26 ops   │
│  • rate limiting     │    │  28 MCP     │   │  11 MCP    │  │  14 MCP   │
│  • RFC 9457 errors   │    └──────┬──────┘   └─────┬──────┘  └─────┬─────┘
│  • metrics / tracing │           │                │               │
│  • MCP bridge        │           └────────────────┴───────────────┘
│  • PAS adapters      │                        │
│  • ACORD NGDS        │                  ┌─────▼──────┐
│  • workflow engine   │                  │  Postgres  │
└──────────────────────┘                  │  + Redis   │
                                          └────────────┘
```

## Container responsibilities

| Container | Responsibility | Explicitly not its job |
|---|---|---|
| **pas_core** | Cross-cutting runtime: tenancy, auth, audit, limits, error shape, MCP plumbing, PAS vendor translation | Any product decision |
| **plugin1** | Policy administration operations; one definition drives REST, MCP and the catalogue | Business logic — it executes what the carrier configures |
| **plugin2** | IFRS 17 GMM/VFA/PAA measurement, assumption governance, reconciled disclosure | Posting to a ledger; holding reserve opinions |
| **plugin3** | Submission triage, extraction, enrichment, scoring, appetite rules, audited overrides | Binding cover (Plugin 5 does that) |
| **plugin4** | Product definitions, rating, compliance constraints, versioning, publication | Rating tables from an actuary's pricing engine |
| **plugin5** | Partner catalog, quotes, onboarding, payment intents, commissions | Being the ledger of record |
| **plugin6** | Ingestion, MDM survivorship, quality scoring, lineage, feature store | Business reporting |
| **plugin7** | Hash-chained policy record, multi-signature changes, annuity schedules, DIDs, portability | Consensus on a public network; being Fabric itself |

## Deployment

Seven independent Helm releases of one chart, seven namespaces' worth of
configuration, one image. Each releases on its own schedule, scales on its own
tiers, and rolls back on its own. Plugin 6 has no ingress because an ingestion API
authenticated but not designed for the public internet should not be reachable from
one. Plugin 7 runs the largest tier because policy records are the most heavily
audited surface.
