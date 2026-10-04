"""The FastAPI application factory shared by all seven plugins.

Each plugin calls :func:`create_plugin_app` with its own router, settings,
operations and MCP registry and receives a fully wired application carrying the
platform's cross-cutting behaviour:

* OAuth 2.1 / OIDC authentication and scope enforcement
* per-carrier tenant resolution and isolation
* per-tenant rate limiting with ``Retry-After``
* correlation IDs, structured logs, Prometheus metrics, optional tracing
* RFC 9457 problem+json error rendering for every failure mode
* idempotency-key handling for writes
* the ACORD NGDS discovery and error-catalogue endpoints
* an MCP streamable-HTTP transport mounted at ``/mcp``
* readiness/liveness probes and a version endpoint
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from pas_core import __version__
from pas_core.acord.schema import bundled_schema, registry_of_schemas
from pas_core.acord.transaction import registry_as_json
from pas_core.audit import AuditTrail, CompositeAuditSink, StructuredLogSink, get_audit_trail
from pas_core.config import PluginSettings
from pas_core.errors import (
    ERROR_CATALOG,
    ErrorCode,
    PasError,
    catalog_as_openapi_components,
)
from pas_core.mcp.registry import McpToolRegistry
from pas_core.observability import (
    GLOBAL_METRICS,
    correlation_scope,
    configure_logging,
    new_correlation_id,
    prometheus_payload,
    record_http_request,
)
from pas_core.ratelimit import (
    DEFAULT_POLICIES,
    ConcurrencyLimiter,
    InMemoryRateLimiter,
    RateLimitPolicy,
)
from pas_core.security import ApiKeyStore, Authenticator
from pas_core.tenancy import (
    InMemoryTenantRegistry,
    RequestPrincipal,
    Tenant,
    TenantContext,
    build_context,
    reset_tenant_context,
    set_tenant_context,
)

logger = logging.getLogger("pas_core.app")

CORRELATION_HEADER = "X-Correlation-Id"
IDEMPOTENCY_HEADER = "Idempotency-Key"


@dataclass(slots=True)
class Platform:
    """Container for the shared services a plugin's routes depend on."""

    settings: PluginSettings
    tenants: InMemoryTenantRegistry
    authenticator: Authenticator
    limiter: InMemoryRateLimiter
    concurrency: ConcurrencyLimiter
    audit: AuditTrail
    operations: Any = None
    mcp_registry: McpToolRegistry | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def api_keys(self) -> ApiKeyStore:
        return self.authenticator.api_keys


def bootstrap(settings: PluginSettings) -> Platform:
    """Create the shared services and register the default demo tenants."""
    configure_logging(settings.log_level, fmt=settings.log_format)

    tenants = InMemoryTenantRegistry()
    if not tenants.list():
        tenants.register(
            Tenant(
                tenant_id="demo-carrier",
                legal_name="Demo Mutual Life Insurance",
                plan="enterprise",
                pas_vendor="simulated",
                enabled_plugins=frozenset(
                    {f"plugin{i}" for i in range(1, 8)} | {"plugin0"}
                ),
                metadata={"purpose": "development and evaluation"},
            )
        )
        tenants.register(
            Tenant(
                tenant_id="acme-life",
                legal_name="Acme Life Insurance Company",
                plan="starter",
                pas_vendor="majesco-lifeplus",
                enabled_plugins=frozenset({"plugin1", "plugin2", "plugin5"}),
                metadata={"purpose": "second tenant proves isolation"},
            )
        )
        tenants.register(
            Tenant(
                tenant_id="northstar-annuity",
                legal_name="Northstar Annuity Partners",
                plan="strategic",
                pas_vendor="oracle-oipa",
                enabled_plugins=frozenset({"plugin1", "plugin2", "plugin3", "plugin6", "plugin7"}),
                metadata={"purpose": "annuity-focused carrier"},
            )
        )

    authenticator = Authenticator(settings)
    limiter = InMemoryRateLimiter(DEFAULT_POLICIES)
    concurrency = ConcurrencyLimiter(limit=50)
    audit = AuditTrail(CompositeAuditSink(StructuredLogSink()), service_name=settings.service_name)

    return Platform(
        settings=settings,
        tenants=tenants,
        authenticator=authenticator,
        limiter=limiter,
        concurrency=concurrency,
        audit=audit,
    )


async def resolve_context(
    request: Request,
    platform: Platform,
    *,
    require_plugin: bool = False,
) -> TenantContext:
    """FastAPI dependency: authenticate, resolve the tenant and bind the context.

    This is the single entry point through which every plugin route obtains its
    tenant.  Because it is a dependency, a route that forgets to declare it cannot
    run - the tenant is structurally unavailable.
    """
    settings = platform.settings
    authorization = request.headers.get("Authorization")
    api_key = request.headers.get(settings.api_key_header)
    header_tenant = request.headers.get(settings.tenant_header)

    principal = await platform.authenticator.authenticate(
        authorization=authorization, api_key=api_key, header_tenant=header_tenant
    )
    tenant = platform.tenants.get(principal.tenant_id)
    if tenant is None:
        if principal.auth_method == "anonymous":
            tenant = platform.tenants.require(header_tenant or "demo-carrier")
            principal = RequestPrincipal(
                subject=principal.subject,
                tenant_id=tenant.tenant_id,
                scopes=principal.scopes,
                roles=principal.roles,
                auth_method=principal.auth_method,
            )
        else:
            raise PasError(
                ErrorCode.VALIDATION_FAILED,
                f"Unknown tenant '{principal.tenant_id}'",
                {"tenantId": principal.tenant_id},
            )
    tenant.assert_active()

    correlation_id = request.headers.get(CORRELATION_HEADER) or new_correlation_id()
    context = build_context(tenant, principal, correlation_id=correlation_id)
    set_tenant_context(context)
    request.state.tenant_context = context
    if require_plugin:
        context.require_plugin(settings.plugin_id)
    return context


def context_dependency(platform: Platform, *, require_plugin: bool = False) -> Callable[..., Awaitable[TenantContext]]:
    """Build a FastAPI dependency bound to this plugin's platform."""

    async def dependency(request: Request) -> TenantContext:
        return await resolve_context(request, platform, require_plugin=require_plugin)

    return dependency


def require_scopes(*scopes: str) -> Callable[..., Awaitable[RequestPrincipal]]:
    """Dependency that enforces OAuth scopes, returning the principal."""

    async def dependency(request: Request) -> RequestPrincipal:
        context: TenantContext | None = getattr(request.state, "tenant_context", None)
        if context is None:
            platform: Platform = request.app.state.platform
            context = await resolve_context(request, platform)
        context.principal.require_scopes(*scopes)
        return context.principal

    return dependency


def idempotency_key(request: Request) -> str | None:
    """Read the ``Idempotency-Key`` header, generating one when absent."""
    return request.headers.get(IDEMPOTENCY_HEADER) or str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
def install_middleware(app: FastAPI, platform: Platform) -> None:
    """Correlation IDs, metrics and access logging."""

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        correlation_id = request.headers.get(CORRELATION_HEADER) or new_correlation_id()
        tenant_hint = request.headers.get(platform.settings.tenant_header, "unknown")
        with correlation_scope(correlation_id, tenant_hint):
            request.state.correlation_id = correlation_id
            started = time.perf_counter()
            try:
                response = await call_next(request)
            except Exception:
                duration = time.perf_counter() - started
                record_http_request(
                    platform.settings.plugin_id, request.method, _template(request), "500", duration
                )
                logger.exception(
                    "unhandled error",
                    extra={"path": request.url.path, "correlationId": correlation_id},
                )
                raise
            duration = time.perf_counter() - started
            record_http_request(
                platform.settings.plugin_id, request.method, _template(request),
                str(response.status_code), duration,
            )
            response.headers[CORRELATION_HEADER] = correlation_id
            response.headers["X-PAS-Plugin"] = platform.settings.plugin_id
            response.headers["X-PAS-Plugin-Version"] = platform.settings.plugin_version
            return response

    @app.middleware("http")
    async def size_guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        declared = request.headers.get("content-length")
        limit = platform.settings.max_request_bytes
        if declared and declared.isdigit() and int(declared) > limit:
            error = PasError(
                ErrorCode.VALIDATION_FAILED,
                f"Request body of {declared} bytes exceeds the {limit} byte limit",
                {"contentLength": int(declared), "limit": limit},
            )
            return _problem_response(error, request)
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=platform.settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[CORRELATION_HEADER, "Retry-After", IDEMPOTENCY_HEADER],
    )


def _template(request: Request) -> str:
    """Route template for metric cardinality control (never the raw path)."""
    route = request.scope.get("route")
    return getattr(route, "path", "unmatched") if route is not None else "unmatched"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------
def install_exception_handlers(app: FastAPI, platform: Platform) -> None:
    """Render every failure as RFC 9457 problem+json with catalogue codes."""

    @app.exception_handler(PasError)
    async def pas_error_handler(request: Request, exc: PasError) -> JSONResponse:
        if exc.http_status >= 500:
            logger.error("pas error", extra={"code": exc.code.value, "detail": exc.message})
        else:
            logger.info(
                "pas error",
                extra={"code": exc.code.value, "status": exc.http_status, "path": request.url.path},
            )
        if exc.code in {ErrorCode.TENANT_MISMATCH, ErrorCode.PERMISSION_DENIED, ErrorCode.SCOPE_INSUFFICIENT}:
            platform.audit.record_access_denied(
                resource_type="request", resource_id=request.url.path, reason=exc.code.value
            )
        response = _problem_response(exc, request)
        if exc.code is ErrorCode.RATE_LIMITED:
            retry = float(exc.detail.get("retryAfterSeconds", 1.0))
            response.headers["Retry-After"] = str(max(1, int(retry)))
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        error = PasError(
            ErrorCode.VALIDATION_FAILED,
            "Request failed validation against the published contract",
            {
                "violations": [
                    {
                        "path": ".".join(str(p) for p in item.get("loc", [])[1:]),
                        "message": item.get("msg", ""),
                        "type": item.get("type", ""),
                    }
                    for item in exc.errors()[:25]
                ]
            },
        )
        return _problem_response(error, request)

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        mapping = {
            401: ErrorCode.UNAUTHENTICATED,
            403: ErrorCode.PERMISSION_DENIED,
            404: ErrorCode.NOT_FOUND,
            405: ErrorCode.NOT_FOUND,
            429: ErrorCode.RATE_LIMITED,
        }
        code = mapping.get(exc.status_code, ErrorCode.INTERNAL)
        error = PasError(code, str(exc.detail), {"path": request.url.path})
        return _problem_response(error, request, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled exception", extra={"path": request.url.path})
        error = PasError(
            ErrorCode.INTERNAL,
            "An unexpected error occurred. Quote the correlation id when reporting it.",
            {"correlationId": getattr(request.state, "correlation_id", "")},
        )
        return _problem_response(error, request)


def _problem_response(error: PasError, request: Request, *, status_code: int | None = None) -> JSONResponse:
    instance = request.url.path
    payload = error.to_problem(instance=instance)
    payload["correlationId"] = getattr(request.state, "correlation_id", "")
    response = JSONResponse(
        status_code=status_code or error.http_status,
        content=payload,
        media_type="application/problem+json",
    )
    return response


# ---------------------------------------------------------------------------
# Platform routers
# ---------------------------------------------------------------------------
def platform_router(platform: Platform) -> APIRouter:
    """Health, version, error catalogue, ACORD discovery and MCP catalogue."""
    router = APIRouter(tags=["platform"])

    @router.get("/health", summary="Liveness probe", operation_id="platform.health")
    async def health() -> dict[str, Any]:
        """Liveness probe. Never touches the database so it cannot produce a false negative."""
        return {
            "status": "healthy",
            "plugin": platform.settings.plugin_id,
            "version": platform.settings.plugin_version,
            "suiteVersion": __version__,
            "environment": platform.settings.env,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    @router.get("/health/ready", summary="Readiness probe", operation_id="platform.ready")
    async def ready() -> dict[str, Any]:
        """Readiness probe: checks that the tenant registry and MCP server are usable."""
        checks: dict[str, Any] = {}
        try:
            checks["tenants"] = {"status": "ok", "count": len(platform.tenants.list())}
        except Exception as exc:  # noqa: BLE001
            checks["tenants"] = {"status": "error", "detail": str(exc)}
        if platform.mcp_registry is not None:
            checks["mcp"] = {
                "status": "ok",
                "server": platform.mcp_registry.info.name,
                "tools": len(platform.mcp_registry.tool_names()),
            }
        if platform.operations is not None:
            checks["operations"] = {"status": "ok", "count": len(platform.operations)}
        ok = all(c.get("status") == "ok" for c in checks.values())
        return JSONResponse(
            status_code=status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "ready" if ok else "not_ready", "checks": checks},
        )

    @router.get("/version", summary="Build and capability information", operation_id="platform.version")
    async def version() -> dict[str, Any]:
        """Version, enabled plugins and the connected PAS vendors."""
        from pas_core.pas.registry import available_vendors, describe_vendor  # noqa: PLC0415

        return {
            "suiteVersion": __version__,
            "pluginId": platform.settings.plugin_id,
            "pluginVersion": platform.settings.plugin_version,
            "environment": platform.settings.env,
            "authRequired": platform.settings.oidc_required,
            "rateLimiterBackend": platform.settings.factory_backend,
            "workflowEngine": getattr(platform.settings, "workflow_engine", "local"),
            "vendors": available_vendors(),
            "supportedPasVendors": [
                describe_vendor(v["vendor"]) for v in available_vendors()
            ],
        }

    @router.get("/errors/catalog", summary="Published error catalogue", operation_id="platform.errorCatalog")
    async def error_catalog() -> dict[str, Any]:
        """The full, stable error code catalogue so clients can branch on codes."""
        return {
            "specification": "RFC 9457 (Problem Details for HTTP APIs)",
            "entries": [
                {
                    "code": e.code.value,
                    "httpStatus": e.http_status,
                    "title": e.title,
                    "description": e.description,
                    "retryable": e.retryable,
                }
                for e in ERROR_CATALOG.values()
            ],
        }

    @router.get("/acord/ngds", summary="ACORD NGDS reference", operation_id="platform.acordRegistry")
    async def acord_registry_endpoint() -> dict[str, Any]:
        """ACORD NGDS transaction code registry for Life and Annuity business."""
        return registry_as_json()

    @router.get("/acord/schemas", summary="ACORD NGDS JSON Schemas", operation_id="platform.acordSchemas")
    async def acord_schemas() -> dict[str, Any]:
        """Draft 2020-12 JSON Schemas for every canonical NGDS entity."""
        return {
            "bundled": bundled_schema(),
            "entities": registry_of_schemas(),
        }

    @router.get("/mcp/catalogue", summary="MCP discovery document", operation_id="platform.mcpCatalogue")
    async def mcp_catalogue() -> dict[str, Any]:
        """Tools, resources and prompts this plugin exposes over MCP.

        An agent that cannot speak MCP can still read this document and call the
        equivalent REST endpoints.
        """
        if platform.mcp_registry is None:
            return {"enabled": False, "reason": "MCP is not enabled for this plugin"}
        return {"enabled": True, **platform.mcp_registry.catalogue()}

    @router.get("/mcp/stats", summary="MCP tool invocation statistics", operation_id="platform.mcpStats")
    async def mcp_stats(request: Request) -> dict[str, Any]:
        if platform.mcp_registry is None:
            return {"enabled": False}
        await resolve_context(request, platform)
        return platform.mcp_registry.stats()

    @router.get("/metrics", summary="Prometheus metrics", operation_id="platform.metrics")
    async def metrics() -> Response:
        """Prometheus text exposition format."""
        return Response(content=prometheus_payload(), media_type="text/plain; version=0.0.4")

    @router.get("/metrics/snapshot", summary="In-process metric snapshot", operation_id="platform.metricsSnapshot")
    async def metrics_snapshot() -> dict[str, Any]:
        """Metric snapshot used by the Svelte management UI when Prometheus is not reachable."""
        return GLOBAL_METRICS.snapshot()

    @router.get("/tenants", summary="Tenant registry", operation_id="platform.listTenants")
    async def list_tenants(request: Request) -> dict[str, Any]:
        """Tenants visible to the caller. Cross-tenant reads are refused, not just hidden."""
        context = await resolve_context(request, platform)
        if "*" in context.principal.scopes:
            return {
                "tenants": [
                    {
                        "tenantId": t.tenant_id,
                        "legalName": t.legal_name,
                        "status": str(t.status),
                        "plan": str(t.plan),
                        "pasVendor": t.pas_vendor,
                        "enabledPlugins": sorted(t.enabled_plugins),
                    }
                    for t in platform.tenants.list()
                ]
            }
        tenant = context.tenant
        return {
            "tenants": [
                {
                    "tenantId": tenant.tenant_id,
                    "legalName": tenant.legal_name,
                    "status": str(tenant.status),
                    "plan": str(tenant.plan),
                    "pasVendor": tenant.pas_vendor,
                    "enabledPlugins": sorted(tenant.enabled_plugins),
                }
            ]
        }

    return router


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------
@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    platform: Platform = app.state.platform
    logger.info(
        "plugin starting",
        extra={
            "plugin": platform.settings.plugin_id,
            "version": platform.settings.plugin_version,
            "env": platform.settings.env,
            "authRequired": platform.settings.oidc_required,
        },
    )
    yield
    logger.info("plugin stopping", extra={"plugin": platform.settings.plugin_id})


def compose_lifespan(plugin_lifespan: Any = None) -> Any:  # noqa: ANN401
    """Compose the platform's start/stop logging with a plugin's own lifespan."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with _lifespan(app):
            if plugin_lifespan is None:
                yield
                return
            async with plugin_lifespan(app) as value:
                yield value

    return lifespan


# Scopes advertised in the generated OAuth flows. Kept explicit rather than
# discovered at runtime so the spec is stable between runs: a client generator
# that sees a different scope list on each generation produces churn.
_KNOWN_SCOPES: tuple[str, ...] = (
    "insurance:read",
    "insurance:write",
    "ifrs17:read",
    "ifrs17:write",
    "uw:read",
    "uw:submit",
    "uw:override",
    "uw:kyc",
    "products:read",
    "products:write",
    "products:publish",
    "distribution:read",
    "distribution:write",
    "distribution:kyc",
    "distribution:publish",
    "data:read",
    "data:write",
    "ledger:read",
    "ledger:write",
    "workflow:write",
    "admin:read",
    "admin:write",
)


def create_plugin_app(
    settings: PluginSettings,
    *,
    routers: list[APIRouter] | None = None,
    operations: Any = None,  # noqa: ANN401
    mcp_registry: McpToolRegistry | None = None,
    platform: Platform | None = None,
    description: str = "",
    tags: list[str] | None = None,
    extra_components: dict[str, Any] | None = None,
    lifespan: Any = None,  # noqa: ANN401
) -> FastAPI:
    """Build a fully wired plugin application.

    ``operations`` is an :class:`~pas_core.pas.base.OperationRegistry`; when
    supplied the atomic operations are mounted automatically under ``/operations``
    with their declared schemas, which is what makes plugin 1's catalogue live.
    ``lifespan`` lets a plugin seed its stores on startup and tear them down on
    shutdown; it composes with the platform's own start/stop logging.
    """
    platform = platform or bootstrap(settings)
    if operations is not None:
        platform.operations = operations
    if mcp_registry is not None:
        platform.mcp_registry = mcp_registry

    app = FastAPI(
        title=f"{settings.plugin_id} - PAS Plugins",
        description=description
        or (
            f"{settings.plugin_id} - a sidecar plugin for Life and Annuity Policy "
            "Administration Systems. Works alongside an existing PAS; it does not "
            "replace it. All interfaces are OpenAPI 3.1 and ACORD NGDS aligned."
        ),
        version=settings.plugin_version,
        openapi_version="3.1.0",
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_tags=tags or [{"name": "platform", "description": "Health, discovery and governance"}],
        lifespan=compose_lifespan(lifespan),
        contact={"name": "PAS Plugins Platform", "url": "https://docs.pas-plugins.io"},
        license_info={"name": "Proprietary"},
    )
    app.state.platform = platform
    for key, value in (extra_components or {}).items():
        app.state.__dict__[key] = value

    install_middleware(app, platform)
    install_exception_handlers(app, platform)
    app.include_router(platform_router(platform))
    for router in routers or []:
        app.include_router(router)
    if operations is not None:
        from pas_core.pas.router import build_operations_router  # noqa: PLC0415

        app.include_router(build_operations_router(platform, operations))
    if mcp_registry is not None:
        _mount_mcp(app, platform, mcp_registry)

    _patch_openapi(app, operations, mcp_registry)
    return app


def _mount_mcp(app: FastAPI, platform: Platform, registry: McpToolRegistry) -> None:
    """Mount the MCP streamable-HTTP transport, degrading gracefully if absent."""
    from pas_core.mcp.server import (  # noqa: PLC0415
        McpTenantResolver,
        _HAS_SDK,
        clear_mcp_request_headers,
        mcp_app,
        set_mcp_request_headers,
    )

    if not _HAS_SDK:
        logger.warning(
            "MCP transport not mounted: the mcp package is not installed. "
            "The REST catalogue at /mcp/catalogue still works."
        )
        return
    resolver = McpTenantResolver(platform.tenants, api_key_store=platform.api_keys)

    @app.middleware("http")
    async def capture_mcp_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        set_mcp_request_headers(dict(request.headers))
        try:
            return await call_next(request)
        finally:
            clear_mcp_request_headers()

    app.mount("/mcp", mcp_app(registry, path="/", resolver=resolver))
    logger.info("MCP transport mounted at /mcp", extra={"server": registry.info.name})


def _patch_openapi(app: FastAPI, operations: Any, mcp_registry: McpToolRegistry | None) -> None:  # noqa: ANN401
    """Enrich the generated OpenAPI document.

    * pin it to OpenAPI 3.1.0 (JSON Schema 2020-12, which MCP input schemas use)
    * inject the error catalogue and ACORD schemas as reusable components
    * publish the ACORD transaction-code registry as an extension
    """

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        from fastapi.openapi.utils import get_openapi  # noqa: PLC0415

        schema = get_openapi(
            title=app.title,
            version=app.version,
            openapi_version="3.1.0",
            description=app.description,
            routes=app.routes,
            tags=app.openapi_tags,
        )
        components = schema.setdefault("components", {})
        schemas = components.setdefault("schemas", {})
        schemas.update(catalog_as_openapi_components())

        # Real security schemes, not just an extension string.
        #
        # Every plugin sits behind a tenant header and an OAuth token, and a spec
        # that does not say so produces a client generator that emits code with no
        # auth at all. Declaring them here rather than per route is deliberate: the
        # platform authenticates every request, so a route that forgets a security
        # requirement would otherwise look unauthenticated in the contract while
        # being protected in fact.
        components["securitySchemes"] = {
            "oauth2_authorization_code_pkce": {
                "type": "oauth2",
                "description": "Interactive user login with PKCE.",
                "flows": {
                    "authorizationCode": {
                        "authorizationUrl": "https://id.pas.example/authorize",
                        "tokenUrl": "https://id.pas.example/token",
                        "refreshUrl": "https://id.pas.example/token",
                        "scopes": {
                            scope: f"Grants {scope}." for scope in _KNOWN_SCOPES
                        },
                    }
                },
            },
            "oauth2_client_credentials": {
                "type": "oauth2",
                "description": "Machine-to-machine access for service accounts.",
                "flows": {
                    "clientCredentials": {
                        "tokenUrl": "https://id.pas.example/token",
                        "scopes": {
                            scope: f"Grants {scope}." for scope in _KNOWN_SCOPES
                        },
                    }
                },
            },
            "apiKey": {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key",
                "description": "Development and service API key. Not for production traffic.",
            },
            "tenantHeader": {
                "type": "apiKey",
                "in": "header",
                "name": "PAS_TENANT_HEADER",
                "description": (
                    "Tenant identifier header. The resolved tenant is compared against "
                    "the token's tenant claim; a mismatch is rejected before routing."
                ),
            },
        }
        schema["security"] = [
            {"oauth2_authorization_code_pkce": [], "tenantHeader": []},
            {"oauth2_client_credentials": [], "tenantHeader": []},
            {"apiKey": [], "tenantHeader": []},
        ]
        schema.setdefault("components", {}).setdefault("securitySchemes", {})

        acord_components: dict[str, Any] = {}
        for name, entity_schema in registry_of_schemas().items():
            entity_schema = dict(entity_schema)
            entity_schema.pop("$defs", None)
            acord_components[f"Acord{name}"] = entity_schema
        schemas.update(acord_components)

        schema["info"]["x-pas-plugin"] = app.state.platform.settings.plugin_id
        schema["info"]["x-pas-plugin-version"] = app.state.platform.settings.plugin_version
        schema["info"]["x-pas-suite-version"] = __version__
        schema["info"]["x-acord-ngds"] = {
            "standardVersion": registry_as_json()["standardVersion"],
            "sourceRef": registry_as_json()["sourceRef"],
            "transactionCodes": len(registry_as_json()["transactions"]),
        }
        schema["info"]["x-security-schemes"] = [
            "oauth2_authorization_code_pkce",
            "oauth2_client_credentials",
            "apiKey",
        ]
        if operations is not None:
            schema["info"]["x-pas-atomic-operations"] = len(operations)
        if mcp_registry is not None:
            schema["info"]["x-mcp"] = {
                "server": mcp_registry.info.name,
                "transport": "streamable-http",
                "path": "/mcp",
                "toolCount": len(mcp_registry.tool_names()),
            }
        schema["components"] = components
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


def problem_response(error: PasError, instance: str | None = None) -> JSONResponse:
    """Build a problem+json response outside the middleware stack (for RPC paths)."""
    return JSONResponse(
        status_code=error.http_status,
        content=error.to_problem(instance=instance),
        media_type="application/problem+json",
    )


def json_response(payload: Any, status_code: int = 200) -> JSONResponse:  # noqa: ANN401
    """Fast JSON response using orjson for the throughput-sensitive plugin 5 paths."""
    from fastapi.responses import ORJSONResponse  # noqa: PLC0415

    return ORJSONResponse(content=payload, status_code=status_code)


def text_response(text: str, status_code: int = 200) -> PlainTextResponse:
    return PlainTextResponse(text, status_code=status_code)


def dumps(payload: Any) -> str:  # noqa: ANN401
    """Canonical JSON serialisation used for hashing and idempotency comparison."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def register_default_tenants(platform: Platform) -> None:
    """Ensure at least the demo tenants exist (used by tests and the compose stack)."""
    if platform.tenants.get("demo-carrier") is None:
        platform.tenants.register(
            Tenant(
                tenant_id="demo-carrier",
                legal_name="Demo Mutual Life Insurance",
                pas_vendor="simulated",
                enabled_plugins=frozenset({f"plugin{i}" for i in range(0, 8)}),
            )
        )


def get_platform(request: Request) -> Platform:
    """Dependency returning this app's :class:`Platform`."""
    return request.app.state.platform  # type: ignore[no-any-return]


def track_change(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    """Field-level change record for the audit trail."""
    from pas_core.audit import diff_dicts  # noqa: PLC0415

    return diff_dicts(before, after)


def clear_context() -> None:
    """Reset the ambient tenant context (used by tests between cases)."""
    from pas_core.tenancy import maybe_current_context  # noqa: PLC0415

    ctx = maybe_current_context()
    if ctx is not None:
        reset_tenant_context(ctx.correlation_id)


def get_audit() -> AuditTrail:
    return get_audit_trail()


def rate_policy(name: str, rps: float, burst: int) -> RateLimitPolicy:
    """Convenience constructor for plugin-specific limit policies."""
    return RateLimitPolicy(name=name, requests_per_second=rps, burst=burst)


def dependant_context(platform: Platform) -> Any:  # noqa: ANN401
    """FastAPI ``Depends``-ready context dependency for this plugin."""
    return Depends(context_dependency(platform))


def plugin_tags(*names: str) -> list[dict[str, Any]]:
    return [{"name": name} for name in names]


def assert_operation_supported(operations: Any, operation_id: str) -> None:  # noqa: ANN401
    if operations is None:
        return
    operations.get(operation_id)


__all__ = [
    "CORRELATION_HEADER",
    "IDEMPOTENCY_HEADER",
    "Platform",
    "bootstrap",
    "clear_context",
    "context_dependency",
    "create_plugin_app",
    "dependant_context",
    "dumps",
    "get_audit",
    "get_platform",
    "idempotency_key",
    "install_exception_handlers",
    "install_middleware",
    "json_response",
    "platform_router",
    "problem_response",
    "rate_policy",
    "register_default_tenants",
    "require_scopes",
    "resolve_context",
    "text_response",
    "track_change",
]
