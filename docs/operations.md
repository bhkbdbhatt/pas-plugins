# Operations

## Running locally

```bash
pip install -e ".[dev]"

# one plugin
uvicorn pas_plugins.plugin3_auw.main:app --port 8003 --reload

# all seven
docker compose -f deploy/docker-compose.yml up --build
```

| Plugin | Port | Module |
|---|---|---|
| 1 | 8001 | `pas_plugins.plugin1_gateway.main:app` |
| 2 | 8002 | `pas_plugins.plugin2_ifrs17.main:app` |
| 3 | 8003 | `pas_plugins.plugin3_auw.main:app` |
| 4 | 8004 | `pas_plugins.plugin4_productconfig.main:app` |
| 5 | 8005 | `pas_plugins.plugin5_embedded.main:app` |
| 6 | 8006 | `pas_plugins.plugin6_datamesh.main:app` |
| 7 | 8007 | `pas_plugins.plugin7_blockchain.main:app` |

Every plugin exposes `/health`, `/ready`, `/metrics`, `/openapi.json` and `/mcp`.

## Tenant header

Every request carries `X-Tenant-Id`. In development the anonymous principal is
granted `*`, which is convenient and is exactly why tenant isolation has its own
tests: an authenticated caller sending another tenant's id gets a 403 from
middleware before any handler runs.

```bash
curl -H "X-Tenant-Id: demo-carrier" http://localhost:8003/health
```

## Quality gates

```bash
ruff check src tests scripts        # lint
ruff format --check src tests       # formatting
mypy src --ignore-missing-imports   # types
pytest -q                           # 527 tests
```

## Contract freshness

Generated artefacts are committed and verified in CI, so drift fails the build:

```bash
python scripts/export_openapi.py --check --validate   # specs current and valid
python scripts/generate_postman.py && git diff --exit-code -- contracts/postman
python scripts/generate_ui.py && git diff --exit-code -- ui
```

## Deployment

```bash
# One release per plugin, independently.
helm upgrade --install plugin3 ./deploy/helm/pas-plugin \
  --namespace pas-plugins --create-namespace \
  -f deploy/helm/pas-plugin/values-plugins.yaml \
  --set image.tag=1.4.2

# Or all seven.
cd deploy/terraform && terraform apply
```

The chart is one chart released seven times rather than a parent chart with seven
subcharts: a plugin's rollout must not be coupled to any other plugin's.

Terraform sets tiers rather than per-plugin replica counts, because the reason a
plugin runs larger is a property of what it does — Plugin 7 handles the most
heavily audited surface, Plugin 6 is memory-hungry batch work.

## Troubleshooting

**A plugin returns 403 on everything.** The tenant header is missing or does not
match the token's tenant claim. Check `X-Tenant-Id` first; a 403 from middleware is
almost never a scope problem.

**A plugin returns 422 with "not permitted from status".** That is chaincode
refusing an illegal lifecycle transition (Plugin 7) or a product rule refusing a
configuration (Plugin 4). The message names the permitted source states. It is a
business refusal, not a malformed request.

**`blocked by CORS`.** The plugin sets its own CORS allowlist. Add the origin rather
than disabling it; a wildcard on a sidecar holding policyholder data is how a
browser becomes an attack surface.

**Tests pass alone but fail in the suite.** Something is mutating shared state. The
module-level singletons (`default_workbench_service`, `default_ledger_service`) are
process-wide by design; tests that need isolation should construct their own and,
for the HTTP surface, install it via `platform.extra["..."]`.

**An OpenAPI check fails in CI but not locally.** The spec is stale — a route was
added without regenerating. Run `python scripts/export_openapi.py` and commit.

**A UI build fails on a fresh checkout.** `npm ci` needs a lockfile; the generator
does not produce one. `npm install` creates it, then commit it.

## Observability

Structured JSON logs with `correlation_id` and `tenant_id` on every line; Prometheus
metrics at `/metrics`; OpenTelemetry traces when an OTLP endpoint is configured.
Errors are RFC 9457 problem documents with a stable `code`, so an alert can key on
the code rather than on message text.
