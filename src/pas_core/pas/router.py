"""HTTP router that exposes the atomic operation catalogue.

Every operation in :class:`~pas_core.pas.base.OperationRegistry` becomes a real,
documented REST endpoint whose OpenAPI entry is generated from the operation's
own declaration.  Spec and behaviour cannot drift because there is only one
definition per operation.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request, Response

from pas_core.app import Platform, resolve_context
from pas_core.audit import AuditAction, AuditOutcome
from pas_core.errors import ErrorCode, PasError, ValidationError
from pas_core.observability import GLOBAL_METRICS
from pas_core.pas.base import (
    AtomicOperation,
    OperationRegistry,
    OperationSide,
    validate_request,
)
from pas_core.pas.registry import describe_vendor, get_adapter, vendor_supports
from pas_core.pas.translation import get_engine
from pas_core.tenancy import TenantContext

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable


def build_operations_router(
    platform: Platform, operations: OperationRegistry
) -> APIRouter:
    """Mount one route per atomic operation plus the catalogue endpoints."""
    router = APIRouter(tags=["operations"])

    @router.get(
        "/operations",
        summary="Atomic operation catalogue",
        operation_id="catalogue.listOperations",
    )
    async def list_operations(
        request: Request,
        tag: str | None = None,
        side: str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        """Every atomic operation the gateway exposes for this carrier.

        This is the machine-readable inventory an agent or integrator reads first.
        """
        ctx: TenantContext = await resolve_context(request, platform)
        rows = operations.list(
            tag=tag,
            side=OperationSide(side) if side else None,
            search=search,
        )
        return {
            "tenantId": ctx.tenant_id,
            "pasVendor": ctx.effective_pas_vendor,
            "count": len(rows),
            "operations": [_catalogue_row(o, ctx.effective_pas_vendor) for o in rows],
            "tags": sorted({t for o in operations.list() for t in o.tags}),
        }

    @router.get(
        "/operations/{operation_id:path}",
        summary="Atomic operation detail",
        operation_id="catalogue.describeOperation",
    )
    async def describe_operation(operation_id: str, request: Request) -> dict[str, Any]:
        """Full contract for one operation: parameters, schemas, examples, errors."""
        ctx: TenantContext = await resolve_context(request, platform)
        operation = operations.get(operation_id)
        return {
            **_catalogue_row(operation, ctx.effective_pas_vendor),
            "description": operation.description,
            "requestSchema": operation.request_schema,
            "responseSchema": operation.response_schema,
            "requestExample": operation.request_example,
            "responseExample": operation.response_example,
            "parameters": [
                {
                    "name": p.name,
                    "in": p.location,
                    "required": p.required or p.location == "path",
                    "schema": p.schema_,
                    "description": p.description,
                    "examples": list(p.examples),
                }
                for p in operation.parameters
            ],
            "vendorOperation": operation.vendor_operation,
        }

    @router.get("/operations/{operation_id:path}/openapi", include_in_schema=False)
    async def operation_openapi(operation_id: str, request: Request) -> dict[str, Any]:
        """The OpenAPI fragment for a single operation (used to compose client SDKs)."""
        await resolve_context(request, platform)
        return operations.get(operation_id).to_openapi_operation()

    @router.get("/pas/vendors", summary="Supported PAS vendors", operation_id="pas.listVendors")
    async def list_vendors(request: Request) -> dict[str, Any]:
        """Vendor capability matrix - transport, API style, idempotency support."""
        await resolve_context(request, platform)
        from pas_core.pas.registry import available_vendors  # noqa: PLC0415

        return {
            "vendors": [describe_vendor(v["vendor"]) for v in available_vendors()],
        }

    @router.get("/pas/vendors/{vendor}/health", summary="PAS connectivity probe", operation_id="pas.health")
    async def vendor_health(vendor: str, request: Request) -> dict[str, Any]:
        adapter = get_adapter(vendor)
        return await adapter.health()

    @router.get(
        "/pas/translation/rules",
        summary="Translation rules per vendor",
        operation_id="pas.translationRules",
    )
    async def translation_rules(request: Request) -> dict[str, Any]:
        """The loaded vendor translation profiles, as configuration."""
        await resolve_context(request, platform)
        engine = get_engine()
        return {
            "rulesDirectory": "src/pas_core/pas/rules",
            "vendors": engine.vendors(),
            "profiles": {
                vendor: {
                    "vendor": profile.vendor,
                    "displayName": profile.display_name,
                    "version": profile.version,
                    "description": profile.description,
                    "sourceRef": profile.source_ref,
                    **profile.stats(),
                    "operations": [
                        {
                            "operationId": t.operation_id,
                            "description": t.description,
                            "toVendorRules": len(t.to_vendor),
                            "fromVendorRules": len(t.from_vendor),
                        }
                        for t in profile.operations
                    ],
                }
                for vendor in engine.vendors()
                if (profile := engine.profile(vendor)) is not None
            },
        }

    _mount_operation_routes(router, platform, operations)
    return router


def _mount_operation_routes(
    router: APIRouter, platform: Platform, operations: OperationRegistry
) -> None:
    """Register one route per operation, path-templated from its declaration."""

    async def dispatch(
        request: Request,
        operation: AtomicOperation,
        body: Any,  # noqa: ANN401
        ctx: TenantContext,
        **query_params: Any,
    ) -> Any:  # noqa: ANN401
        # The request schema describes the body only. Path parameters are already
        # validated by the router (typed from the operation's own declaration) and
        # query parameters are optional, so mixing them in would produce spurious
        # "additional property" failures against a closed schema.
        validated_body = validate_request(operation, dict(body or {}))

        ctx.principal.require_scopes(*operation.required_scopes)

        payload = _extract_payload(operation, request, validated_body, query_params)
        path_params = _extract_path_params(operation, request)
        key = request.headers.get("Idempotency-Key")

        vendor = ctx.effective_pas_vendor
        adapter = get_adapter(vendor)
        if not adapter.supports(operation.operation_id):
            raise PasError(
                ErrorCode.PAS_OPERATION_UNAVAILABLE,
                f"PAS vendor '{vendor}' does not expose operation '{operation.operation_id}'",
                {"vendor": vendor, "operationId": operation.operation_id},
            )

        engine = get_engine()
        outbound = engine.to_vendor(vendor, operation.operation_id, payload)
        if not outbound.ok:
            raise PasError(
                ErrorCode.TRANSLATION_FAILED,
                f"Could not translate the request for vendor '{vendor}'",
                {"vendor": vendor, "operationId": operation.operation_id, **outbound.as_trace()},
            )

        adapter_payload = adapter.translate_request(operation, outbound.payload)
        try:
            raw = await adapter.execute(
                operation, adapter_payload, ctx, path_params=path_params, idempotency_key=key
            )
        except PasError as exc:
            platform.audit.record(
                AuditAction.ACCESS,
                resource_type="operation",
                resource_id=operation.operation_id,
                outcome=AuditOutcome.FAILURE,
                ctx=ctx,
                acord_tx_type=str(operation.acord_tx_type) if operation.acord_tx_type else None,
                metadata={"vendor": vendor, "errorCode": exc.code.value},
            )
            raise

        normalised = adapter.translate_response(operation, raw)
        inbound = engine.from_vendor(vendor, operation.operation_id, normalised)
        result = inbound.payload if inbound.ok else normalised

        platform.audit.record(
            AuditAction.RATE if "rate" in operation.operation_id else AuditAction.READ,
            resource_type="operation",
            resource_id=operation.operation_id,
            outcome=AuditOutcome.SUCCESS,
            ctx=ctx,
            acord_tx_type=str(operation.acord_tx_type) if operation.acord_tx_type else None,
            metadata={
                "vendor": vendor,
                "idempotencyKey": key,
                "translation": outbound.as_trace(),
            },
        )
        GLOBAL_METRICS.increment("operation_invocations_total", operation=operation.operation_id)
        result.setdefault("_meta", {})  # type: ignore[union-attr]
        result["_meta"] = {  # type: ignore[index]
            "operationId": operation.operation_id,
            "vendor": vendor,
            "correlationId": ctx.correlation_id,
            "acordTxType": str(operation.acord_tx_type) if operation.acord_tx_type else None,
            "sloMs": operation.slo_ms,
        }
        return result

    for operation in operations.list():
        _register_route(router, platform, operations, operation, dispatch)


def _register_route(
    router: APIRouter,
    platform: Platform,
    operations: OperationRegistry,
    operation: AtomicOperation,
    dispatch: Callable[..., Any],
) -> None:  # noqa: ANN401
    """Register one route, generating a signature FastAPI can introspect.

    Query and header parameters are declared on the generated endpoint function so
    they appear in the OpenAPI document and are validated by FastAPI itself,
    instead of being scraped out of the raw request inside the handler.
    """
    import inspect  # noqa: PLC0415

    type_map: dict[str, type] = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }

    declared_params: list[Any] = []
    for parameter in operation.parameters:
        if parameter.location == "path":
            continue
        python_type = type_map.get(str(parameter.schema_.get("type", "string")), str)
        default = parameter.examples[0] if parameter.examples else (
            None if parameter.required else _python_default(parameter.schema_.get("type", "string"))
        )
        declared_params.append(
            inspect.Parameter(
                parameter.name,
                inspect.Parameter.KEYWORD_ONLY,
                default=(
                    inspect.Parameter.empty if parameter.required else default
                ),
                annotation=python_type,
            )
        )

    path_parameters = [
        p.name for p in operation.parameters if p.location == "path"
    ]
    path_annotations = {
        name: type_map.get(
            str(next(
                (p.schema_.get("type", "string") for p in operation.parameters if p.name == name),
                "string",
            )),
            str,
        )
        for name in path_parameters
    }

    async def endpoint(request: Request, **kwargs: Any) -> Any:  # noqa: ANN401
        ctx: TenantContext = await resolve_context(request, platform)
        body: dict[str, Any] | None = None
        if operation.side is not OperationSide.READ and request.method in {"POST", "PUT", "PATCH"}:
            raw = await request.body()
            if raw:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as exc:
                    from pas_core.errors import ValidationError as _ValidationError  # noqa: PLC0415

                    raise _ValidationError(
                        "Request body is not valid JSON", position=exc.pos
                    ) from exc
                body = parsed if isinstance(parsed, dict) else {"value": parsed}
        return await dispatch(request, operation, body, ctx, **kwargs)

    endpoint.__name__ = operation.operation_id.replace(".", "_").replace("-", "_")
    endpoint.__doc__ = operation.description or operation.summary
    endpoint.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Request),
            *[
                inspect.Parameter(
                    name, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=annotation
                )
                for name, annotation in path_annotations.items()
            ],
            *declared_params,
        ]
    )

    path = _fastapi_path(operation.path)
    method = operation.method.lower()
    func = getattr(router, method, None)
    if func is None:  # pragma: no cover - guarded by AtomicOperation construction
        raise ValidationError(f"unsupported HTTP method '{operation.method}'", method=operation.method)
    decorator = func(
        path,
        name=operation.operation_id.replace(".", "_").replace("-", "_"),
        summary=operation.summary,
        description=operation.description or operation.summary,
        tags=list(operation.tags) or ["operations"],
        response_model=None,
        status_code=200,
        operation_id=operation.operation_id,
        response_description=operation.summary,
        openapi_extra={
            "x-pas-intent": operation.intent,
            "x-pas-side": str(operation.side),
            "x-pas-acord-tx-type": str(operation.acord_tx_type) if operation.acord_tx_type else None,
            "x-pas-required-scopes": list(operation.required_scopes),
            "x-pas-error-codes": [e.value for e in operation.errors],
            "x-pas-slo-ms": operation.slo_ms,
        },
    )
    decorator(endpoint)


def _python_default(kind: str) -> Any:  # noqa: ANN401
    return {"string": None, "integer": None, "number": None, "boolean": False, "array": None, "object": None}.get(
        kind, None
    )


def _fastapi_path(path: str) -> str:
    """Convert an OpenAPI path template into a Starlette route path."""
    cleaned = "/" + path.strip("/")
    return cleaned if cleaned != "/" else "/"


def _extract_payload(
    operation: AtomicOperation,
    request: Request,
    body: dict[str, Any] | None,
    query_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge path parameters, query parameters and the JSON body into one payload.

    Path parameters are also injected as top-level keys so a vendor rule can
    reference ``policyId`` without knowing whether the caller put it in the path
    or the body.
    """
    payload: dict[str, Any] = dict(body or {})
    for name, value in request.path_params.items():
        payload.setdefault(name, value)
    for name, value in (query_params or {}).items():
        if value is not None:
            payload[name] = value
    if operation.side is OperationSide.READ:
        payload.setdefault("limit", 25)
    return payload


def _extract_path_params(operation: AtomicOperation, request: Request) -> dict[str, Any]:
    names = {p.name for p in operation.parameters if p.location == "path"}
    return {name: request.path_params[name] for name in names if name in request.path_params}


def _catalogue_row(operation: AtomicOperation, vendor: str) -> dict[str, Any]:
    return {
        "operationId": operation.operation_id,
        "summary": operation.summary,
        "intent": operation.intent,
        "method": operation.method,
        "path": operation.path,
        "side": str(operation.side),
        "tags": list(operation.tags),
        "requiredScopes": list(operation.required_scopes),
        "acordTxType": str(operation.acord_tx_type) if operation.acord_tx_type else None,
        "errorCodes": [e.value for e in operation.errors],
        "sloMs": operation.slo_ms,
        "requiresIdempotencyKey": operation.side.requires_idempotency_key,
        "availableForVendor": vendor_supports(vendor, operation.operation_id),
    }


def attach_response_headers(response: Response, ctx: TenantContext) -> None:
    """Standard response headers so clients can correlate without parsing bodies."""
    response.headers["X-PAS-Tenant-Id"] = ctx.tenant_id
    response.headers["X-PAS-Correlation-Id"] = ctx.correlation_id
