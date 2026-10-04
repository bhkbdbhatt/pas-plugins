"""RFC 9457 problem details with a life & annuity specific error catalogue.

Every plugin raises :class:`PasError` subclasses so that HTTP responses, MCP tool
results and audit records all carry the same stable, machine-readable codes.  This
is the "error code catalog" deliverable from the specification: clients (and AI
agents) can branch on ``code`` without parsing prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Stable machine-readable error codes (never renumber or reuse)."""

    # --- 400 family -------------------------------------------------------
    VALIDATION_FAILED = "PAS-400-VALIDATION"
    MISSING_REQUIRED_FIELD = "PAS-400-MISSING_FIELD"
    UNSUPPORTED_MEDIA_TYPE = "PAS-400-UNSUPPORTED_MEDIA"
    BUSINESS_RULE_VIOLATION = "PAS-400-BUSINESS_RULE"

    # --- 401 / 403 --------------------------------------------------------
    UNAUTHENTICATED = "PAS-401-UNAUTHENTICATED"
    INVALID_TOKEN = "PAS-401-INVALID_TOKEN"
    TOKEN_EXPIRED = "PAS-401-TOKEN_EXPIRED"
    PERMISSION_DENIED = "PAS-403-PERMISSION_DENIED"
    SCOPE_INSUFFICIENT = "PAS-403-SCOPE_INSUFFICIENT"
    TENANT_MISMATCH = "PAS-403-TENANT_MISMATCH"
    TENANT_SUSPENDED = "PAS-403-TENANT_SUSPENDED"

    # --- 404 / 409 --------------------------------------------------------
    NOT_FOUND = "PAS-404-NOT_FOUND"
    PAS_OPERATION_UNAVAILABLE = "PAS-404-PAS_OPERATION"
    CONFLICT = "PAS-409-CONFLICT"
    VERSION_CONFLICT = "PAS-409-VERSION_CONFLICT"
    IDEMPOTENCY_REPLAY = "PAS-409-IDEMPOTENCY"

    # --- 422 / 429 --------------------------------------------------------
    UNPROCESSABLE = "PAS-422-UNPROCESSABLE"
    QUOTE_NOT_ACCEPTABLE = "PAS-422-QUOTE_NOT_ACCEPTABLE"
    RATE_LIMITED = "PAS-429-RATE_LIMITED"

    # --- 5xx family -------------------------------------------------------
    INTERNAL = "PAS-500-INTERNAL"
    PAS_UNAVAILABLE = "PAS-502-PAS_UNAVAILABLE"
    PAS_TIMEOUT = "PAS-504-PAS_TIMEOUT"
    TRANSLATION_FAILED = "PAS-500-TRANSLATION"
    LEDGER_UNAVAILABLE = "PAS-503-LEDGER"
    MODEL_UNAVAILABLE = "PAS-503-MODEL"
    COMPUTATION_FAILED = "PAS-500-COMPUTATION"


@dataclass(frozen=True, slots=True)
class ErrorDefinition:
    """A single entry in the published error catalogue."""

    code: ErrorCode
    http_status: int
    title: str
    description: str
    retryable: bool = False


ERROR_CATALOG: dict[ErrorCode, ErrorDefinition] = {
    ErrorCode.VALIDATION_FAILED: ErrorDefinition(
        ErrorCode.VALIDATION_FAILED, 400, "Validation failed",
        "The request payload failed schema validation.", retryable=False),
    ErrorCode.MISSING_REQUIRED_FIELD: ErrorDefinition(
        ErrorCode.MISSING_REQUIRED_FIELD, 400, "Missing required field",
        "A field required by the contract was absent."),
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: ErrorDefinition(
        ErrorCode.UNSUPPORTED_MEDIA_TYPE, 400, "Unsupported media type",
        "Content-Type is not application/json."),
    ErrorCode.BUSINESS_RULE_VIOLATION: ErrorDefinition(
        ErrorCode.BUSINESS_RULE_VIOLATION, 400, "Business rule violation",
        "The request is structurally valid but violates an insurance business rule."),
    ErrorCode.UNAUTHENTICATED: ErrorDefinition(
        ErrorCode.UNAUTHENTICATED, 401, "Unauthenticated",
        "No credential was presented."),
    ErrorCode.INVALID_TOKEN: ErrorDefinition(
        ErrorCode.INVALID_TOKEN, 401, "Invalid token",
        "The bearer token failed signature or claim validation.", retryable=False),
    ErrorCode.TOKEN_EXPIRED: ErrorDefinition(
        ErrorCode.TOKEN_EXPIRED, 401, "Token expired",
        "The bearer token has expired; re-authenticate."),
    ErrorCode.PERMISSION_DENIED: ErrorDefinition(
        ErrorCode.PERMISSION_DENIED, 403, "Permission denied",
        "The credential is valid but not permitted to perform this action."),
    ErrorCode.SCOPE_INSUFFICIENT: ErrorDefinition(
        ErrorCode.SCOPE_INSUFFICIENT, 403, "Insufficient scope",
        "The access token lacks a required OAuth scope."),
    ErrorCode.TENANT_MISMATCH: ErrorDefinition(
        ErrorCode.TENANT_MISMATCH, 403, "Tenant mismatch",
        "The token tenant does not match the requested tenant context."),
    ErrorCode.TENANT_SUSPENDED: ErrorDefinition(
        ErrorCode.TENANT_SUSPENDED, 403, "Tenant suspended",
        "The carrier tenant is suspended; contact the platform operator."),
    ErrorCode.NOT_FOUND: ErrorDefinition(
        ErrorCode.NOT_FOUND, 404, "Not found", "The referenced resource does not exist."),
    ErrorCode.PAS_OPERATION_UNAVAILABLE: ErrorDefinition(
        ErrorCode.PAS_OPERATION_UNAVAILABLE, 404, "PAS operation unavailable",
        "The requested atomic operation is not exposed by the connected PAS.",
        retryable=True),
    ErrorCode.CONFLICT: ErrorDefinition(
        ErrorCode.CONFLICT, 409, "Conflict", "The resource is in an incompatible state."),
    ErrorCode.VERSION_CONFLICT: ErrorDefinition(
        ErrorCode.VERSION_CONFLICT, 409, "Version conflict",
        "Optimistic concurrency check failed; re-read and retry.", retryable=True),
    ErrorCode.IDEMPOTENCY_REPLAY: ErrorDefinition(
        ErrorCode.IDEMPOTENCY_REPLAY, 409, "Idempotency replay",
        "This Idempotency-Key was already used with a different payload."),
    ErrorCode.UNPROCESSABLE: ErrorDefinition(
        ErrorCode.UNPROCESSABLE, 422, "Unprocessable entity",
        "The request is understood but cannot be processed."),
    ErrorCode.QUOTE_NOT_ACCEPTABLE: ErrorDefinition(
        ErrorCode.QUOTE_NOT_ACCEPTABLE, 422, "Quote not acceptable",
        "The applicant does not meet the product's acceptance criteria.", retryable=False),
    ErrorCode.RATE_LIMITED: ErrorDefinition(
        ErrorCode.RATE_LIMITED, 429, "Rate limit exceeded",
        "The tenant or caller exceeded its request budget.", retryable=True),
    ErrorCode.INTERNAL: ErrorDefinition(
        ErrorCode.INTERNAL, 500, "Internal error", "An unexpected server error occurred."),
    ErrorCode.PAS_UNAVAILABLE: ErrorDefinition(
        ErrorCode.PAS_UNAVAILABLE, 502, "PAS unavailable",
        "The Policy Administration System returned an error.", retryable=True),
    ErrorCode.PAS_TIMEOUT: ErrorDefinition(
        ErrorCode.PAS_TIMEOUT, 504, "PAS timeout",
        "The Policy Administration System did not respond in time.", retryable=True),
    ErrorCode.TRANSLATION_FAILED: ErrorDefinition(
        ErrorCode.TRANSLATION_FAILED, 500, "Translation failed",
        "The vendor translation rules could not map the payload."),
    ErrorCode.LEDGER_UNAVAILABLE: ErrorDefinition(
        ErrorCode.LEDGER_UNAVAILABLE, 503, "Ledger unavailable",
        "The permissioned ledger network is unreachable.", retryable=True),
    ErrorCode.MODEL_UNAVAILABLE: ErrorDefinition(
        ErrorCode.MODEL_UNAVAILABLE, 503, "Model unavailable",
        "The scoring model could not be loaded.", retryable=True),
    ErrorCode.COMPUTATION_FAILED: ErrorDefinition(
        ErrorCode.COMPUTATION_FAILED, 500, "Computation failed",
        "A numerical engine failed to converge or produced a non-finite result."),
}


@dataclass(slots=True)
class PasError(Exception):
    """Base class for every error raised by the platform.

    Carries the catalogue metadata plus optional per-occurrence detail so that
    an agent or an integrator can react programmatically.  ``retryable_override``
    exists because a code's default retryability is a statement about the *kind* of
    failure, not the specific occurrence: an upstream PAS returning 503 is worth
    retrying even though the same code path can surface a permanent 404.
    """

    code: ErrorCode = ErrorCode.INTERNAL
    message: str = "Unexpected error"
    detail: dict[str, Any] = field(default_factory=dict)
    context: str | None = None
    retryable_override: bool | None = None

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    @property
    def definition(self) -> ErrorDefinition:
        return ERROR_CATALOG[self.code]

    @property
    def http_status(self) -> int:
        return self.definition.http_status

    @property
    def retryable(self) -> bool:
        if self.retryable_override is not None:
            return self.retryable_override
        return self.definition.retryable

    def to_problem(self, *, instance: str | None = None) -> dict[str, Any]:
        """Render as an RFC 9457 ``application/problem+json`` document."""
        definition = self.definition
        problem: dict[str, Any] = {
            "type": f"https://docs.pas-plugins.io/errors/{self.code.value.lower()}",
            "title": definition.title,
            "status": definition.http_status,
            "detail": self.message,
            "code": self.code.value,
            "retryable": definition.retryable,
            "catalog_description": definition.description,
        }
        if instance:
            problem["instance"] = instance
        if self.context:
            problem["context"] = self.context
        if self.detail:
            problem["errors"] = self.detail
        return problem


class ValidationError(PasError):
    """Payload or parameter validation failure."""

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(ErrorCode.VALIDATION_FAILED, message, detail)


class NotFoundError(PasError):
    """A referenced entity does not exist."""

    def __init__(self, entity: str, identifier: str) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            f"{entity} '{identifier}' was not found",
            {"entity": entity, "id": identifier},
        )


class PermissionDeniedError(PasError):
    """The caller is authenticated but not allowed to perform the action."""

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(ErrorCode.PERMISSION_DENIED, message, detail)


class ScopeInsufficientError(PasError):
    """A required OAuth scope is missing from the presented token."""

    def __init__(self, required: str | list[str], granted: list[str] | None = None) -> None:
        required_list = [required] if isinstance(required, str) else list(required)
        super().__init__(
            ErrorCode.SCOPE_INSUFFICIENT,
            f"Required scope(s) not granted: {', '.join(required_list)}",
            {"required": required_list, "granted": granted or []},
        )


class BusinessRuleViolation(PasError):
    """Structurally valid request that an insurance rule rejects."""

    def __init__(self, message: str, *, rule_id: str | None = None, **detail: Any) -> None:
        payload = {"ruleId": rule_id, **detail} if rule_id else dict(detail)
        super().__init__(ErrorCode.BUSINESS_RULE_VIOLATION, message, payload)


class ConflictError(PasError):
    """State conflict, e.g. duplicate policy number."""

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(ErrorCode.CONFLICT, message, detail)


class VersionConflictError(ConflictError):
    """Optimistic concurrency failure."""

    def __init__(self, expected: int, actual: int) -> None:
        PasError.__init__(
            self,
            ErrorCode.VERSION_CONFLICT,
            f"Expected version {expected} but current version is {actual}",
            {"expectedVersion": expected, "currentVersion": actual},
        )


class UpstreamPasError(PasError):
    """The connected Policy Administration System failed."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        vendor: str | None = None,
        operation: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        code = {
            504: ErrorCode.PAS_TIMEOUT,
            404: ErrorCode.PAS_OPERATION_UNAVAILABLE,
        }.get(status_code or 0, ErrorCode.PAS_UNAVAILABLE)
        detail: dict[str, Any] = {"vendor": vendor, "operation": operation, "upstreamStatus": status_code}
        if retryable is not None:
            detail["retryable"] = retryable
        super().__init__(
            code, message, detail, retryable_override=retryable
        )


class RateLimitedError(PasError):
    """Per-tenant request budget exhausted."""

    def __init__(self, *, retry_after: float, limit: int, scope: str) -> None:
        super().__init__(
            ErrorCode.RATE_LIMITED,
            f"Rate limit of {limit} requests exceeded for {scope}",
            {"retryAfterSeconds": retry_after, "limit": limit, "scope": scope},
        )


class TenantMismatchError(PasError):
    """Cross-tenant access attempt - always a hard failure and always audited."""

    def __init__(self, token_tenant: str, requested_tenant: str) -> None:
        super().__init__(
            ErrorCode.TENANT_MISMATCH,
            "Cross-tenant access is not permitted",
            {"tokenTenant": token_tenant, "requestedTenant": requested_tenant},
        )


class ComputationError(PasError):
    """Numerical engine failure (non-convergence, NaN, singular matrix...)."""

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(ErrorCode.COMPUTATION_FAILED, message, detail)


class TranslationError(PasError):
    """Vendor translation rules could not map a payload."""

    def __init__(self, message: str, *, path: str | None = None, **detail: Any) -> None:
        payload = {"path": path, **detail} if path else dict(detail)
        super().__init__(ErrorCode.TRANSLATION_FAILED, message, payload)


def catalog_as_openapi_components() -> dict[str, Any]:
    """Emit the error catalogue as an OpenAPI components object.

    Referenced from every plugin spec so that consumers can enumerate the full
    catalogue (``GET /errors/catalog``) and generate typed clients.
    """
    return {
        "Problem": {
            "type": "object",
            "description": "RFC 9457 problem details with the PAS error catalogue.",
            "required": ["type", "title", "status", "detail", "code"],
            "properties": {
                "type": {"type": "string", "format": "uri"},
                "title": {"type": "string"},
                "status": {"type": "integer", "minimum": 100, "maximum": 599},
                "detail": {"type": "string"},
                "instance": {"type": "string"},
                "code": {"type": "string", "examples": [ErrorCode.VALIDATION_FAILED.value]},
                "retryable": {"type": "boolean"},
                "context": {"type": "string"},
                "catalog_description": {"type": "string"},
                "errors": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": "Field-level validation detail, when applicable.",
                },
            },
        },
        "ErrorCatalogEntry": {
            "type": "object",
            "required": ["code", "httpStatus", "title", "description", "retryable"],
            "properties": {
                "code": {"type": "string"},
                "httpStatus": {"type": "integer"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "retryable": {"type": "boolean"},
            },
        },
    }
