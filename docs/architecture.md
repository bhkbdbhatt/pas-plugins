# Architecture

## Shape

Seven stateless sidecars over a shared runtime library.

```
                        ┌──────────────────────────┐
   Identity provider ──►│  pas_core: auth, tenancy │◄── every request
                        │  audit, rate limit, RFC  │
                        │  9457, MCP bridge        │
                        └────────────┬─────────────┘
                                     │
   ┌──────────┬──────────┬───────────┼───────────┬──────────┬──────────┬──────────┐
   │ plugin1  │ plugin2  │ plugin3   │ plugin4   │ plugin5  │ plugin6  │ plugin7  │
   │ gateway  │ ifrs17   │ auw       │ product   │ distrib  │ data     │ ledger   │
   └──────────┴──────────┴───────────┴───────────┴──────────┴──────────┴──────────┘
        │           │           │           │           │          │          │
     REST + MCP per plugin, each with its own OpenAPI 3.1 contract and Svelte UI
```

## Why this shape

**A plugin boundary is drawn where the buyer's budget ends.** Each of the seven
corresponds to a funded problem. That is not a coincidence — a module boundary that
does not line up with a purchase decision will accumulate unrelated code forever.

**No plugin holds state another plugin needs.** A quote is priced from a published
product version; a ledger event references a policy by id; a feature is materialised
in the mesh. Where integration is genuinely needed, Plugin 1's PAS adapter is the
seam. The alternative — shared mutable state between sidecars — turns seven
independent releases into seven coordinated ones, which is the property the whole
design exists to avoid.

**`pas_core` owns cross-cutting concerns and nothing else.** Tenancy, auth, audit,
rate limiting, error shape, metrics, MCP plumbing. It holds no business logic, so
there is nothing in it to argue about product behaviour.

## Request path

Every request, in every plugin, takes the same path:

1. **Correlation id** assigned or propagated.
2. **Authentication**: JWT verified against the JWKS, or a development API key.
3. **Tenant resolution**: from the tenant header, checked against the token's
   tenant claim. A mismatch is a 403 here, before any handler runs — and it is
   checked for every caller including service principals.
4. **Plugin enablement**: the resolved tenant must have the plugin enabled.
5. **Scope check**: declared per route, not per plugin.
6. **Rate limit** against the tenant's plan.
7. **Handler**.
8. **Audit record** for anything with a side effect.
9. **Error shaping** to RFC 9457.

Steps 2–6 are middleware, which is the only reason seven plugins cannot each
invent their own interpretation of "authenticated".

## Data and tenancy

Multi-tenant, tenant-scoped everywhere. Development uses the in-process factory
backend; a Postgres implementation is the deployment target behind the same
interface. The tenant id is carried in the token and compared against the request
header — a caller cannot act on another tenant's data by changing a header, because
the comparison fails before the handler.

## Contracts

Every plugin's OpenAPI document is **generated from the running application**, not
hand-written, and committed. `scripts/export_openapi.py --check` regenerates and
fails on drift, so an endpoint cannot be added without its contract. Postman
collections and UI sources are generated from the same definitions.

This is the mechanism that stops a spec becoming fiction. A hand-written spec
describes what the code used to do.

## MCP

Each plugin exposes its capabilities as MCP tools over streamable HTTP at `/mcp`,
sharing `pas_core.mcp`. Three properties are enforced in the registry rather than
left to each plugin:

- **Annotations are honest.** A tool that mutates a decision of record is marked
  destructive and requires `confirm`; a tool that writes only to a draft is not.
- **Confirmation is uniform.** One guard, applied to every destructive tool.
- **Resources use the `pas://` scheme**, so a URI means the same thing everywhere.

## Consistency

Synchronous within a plugin. Across plugins, deliberately not: Plugin 2 does not
call Plugin 5 when a premium is quoted, because a synchronous dependency between
sidecars reintroduces the coupling the split was meant to remove. Where a workflow
genuinely spans plugins it is expressed as a saga with compensation, which is what
Plugin 1's workflow engine is for.

## Failure behaviour

A plugin crash affects its own request. There is no shared connection pool to
exhaust, no shared in-memory cache to thrash, and no shared deployment to roll back.
A rolling update takes pods one at a time with `maxUnavailable: 0`, and the startup
probe tolerates a slow first paint while the liveness probe stays tight so a brief
stall does not restart a healthy pod.
