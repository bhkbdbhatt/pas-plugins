"""Per-carrier tenant isolation.

Multi-tenancy is the single hardest cross-cutting concern in the suite, so it is
implemented once, here, and enforced by middleware rather than left to each
plugin.  Every query helper requires a :class:`TenantContext`; there is no code
path that reads or writes tenant data without one.

Isolation strategy
------------------
* **Control plane** - a tenant registry (PostgreSQL) holds status, plan, limits
  and per-tenant configuration.  Tenants can be created, suspended and rotated
  without a redeploy.
* **Data plane** - every tenant-owned table carries a ``tenant_id`` column with a
  composite primary key or a leading ``tenant_id`` index; all repositories take
  the tenant id from the request context, never from the payload.
* **Cache / rate limits / idempotency** - keys are always prefixed with the tenant
  id so a collision cannot cross a carrier boundary.
* **MCP sessions** - the tenant is bound at session initialisation, so an agent
  physically cannot call a tool against another carrier.
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pas_core.errors import PasError, ErrorCode, TenantMismatchError

TENANT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}[a-z0-9]$")
_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


class TenantStatus(StrEnum):
    """Lifecycle of a carrier tenant."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    ONBOARDING = "onboarding"
    DECOMMISSIONED = "decommissioned"


class TenantPlan(StrEnum):
    """Commercial plans - mirrors the pricing model in the specification."""

    TRIAL = "trial"
    STARTER = "starter"
    ENTERPRISE = "enterprise"
    STRATEGIC = "strategic"


@dataclass(frozen=True, slots=True)
class TenantLimits:
    """Hard request budgets. Enforced by :mod:`pas_core.ratelimit`."""

    requests_per_second: int = 50
    requests_per_day: int = 1_000_000
    mcp_calls_per_hour: int = 20_000
    concurrent_workflows: int = 25
    max_policies_per_batch: int = 50_000
    storage_quota_gb: int = 500


@dataclass(frozen=True, slots=True)
class Tenant:
    """A carrier (or carrier division) on the platform."""

    tenant_id: str
    legal_name: str
    status: TenantStatus = TenantStatus.ACTIVE
    plan: TenantPlan = TenantPlan.STARTER
    pas_vendor: str | None = None
    data_region: str = "us-east-1"
    enabled_plugins: frozenset[str] = field(default_factory=lambda: frozenset())
    limits: TenantLimits = field(default_factory=TenantLimits)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not TENANT_ID_PATTERN.match(self.tenant_id):
            msg = (
                f"Invalid tenant id {self.tenant_id!r}: expected 3-64 chars, "
                "lowercase alphanumeric with '-' or '_' separators"
            )
            raise PasError(ErrorCode.VALIDATION_FAILED, msg, {"tenantId": self.tenant_id})
        object.__setattr__(self, "enabled_plugins", frozenset(self.enabled_plugins))

    @property
    def slug(self) -> str:
        """URL/path-safe slug derived from the legal name."""
        return _SLUG_PATTERN.sub("-", self.legal_name.lower()).strip("-")

    def has_plugin(self, plugin_id: str) -> bool:
        """Whether this tenant licensed the given plugin."""
        return plugin_id in self.enabled_plugins

    def assert_plugin_enabled(self, plugin_id: str) -> None:
        if not self.has_plugin(plugin_id):
            raise PasError(
                ErrorCode.PERMISSION_DENIED,
                f"Plugin '{plugin_id}' is not licensed for tenant '{self.tenant_id}'",
                {"tenantId": self.tenant_id, "pluginId": plugin_id},
            )

    def assert_active(self) -> None:
        if self.status is TenantStatus.SUSPENDED:
            raise PasError(
                ErrorCode.TENANT_SUSPENDED,
                f"Tenant '{self.tenant_id}' is suspended",
                {"tenantId": self.tenant_id},
            )
        if self.status is TenantStatus.DECOMMISSIONED:
            raise PasError(
                ErrorCode.PERMISSION_DENIED,
                f"Tenant '{self.tenant_id}' has been decommissioned",
                {"tenantId": self.tenant_id},
            )

    def with_overrides(self, **overrides: Any) -> Tenant:
        return replace(self, **overrides)


@dataclass(frozen=True, slots=True)
class RequestPrincipal:
    """The authenticated actor behind a request."""

    subject: str
    tenant_id: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    roles: tuple[str, ...] = ()
    client_id: str | None = None
    auth_method: str = "anonymous"
    token_id: str | None = None
    expires_at: datetime | None = None

    @property
    def is_service(self) -> bool:
        """True for machine callers (partner APIs, workflow workers, MCP agents)."""
        return self.auth_method in {"api_key", "client_credentials", "mcp"}

    def has_scope(self, scope: str) -> bool:
        if "*" in self.scopes:
            return True
        if scope in self.scopes:
            return True
        # hierarchical scopes: "policy:write" implies "policy:read"
        parts = scope.split(":")
        for cut in range(len(parts) - 1, 0, -1):
            if ":".join(parts[:cut]) + ":*" in self.scopes:
                return True
        return False

    def require_scopes(self, *scopes: str) -> None:
        from pas_core.errors import ScopeInsufficientError  # noqa: PLC0415

        missing = [s for s in scopes if not self.has_scope(s)]
        if missing:
            raise ScopeInsufficientError(missing, sorted(self.scopes))


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Immutable per-request tenant binding.

    Propagated through ``ContextVar`` so that deep call stacks (repositories,
    workflow steps, MCP tools) never need the tenant passed as an argument.
    """

    tenant: Tenant
    principal: RequestPrincipal
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    pas_vendor: str | None = None

    def __post_init__(self) -> None:
        if self.principal.tenant_id != self.tenant.tenant_id:
            raise TenantMismatchError(self.principal.tenant_id, self.tenant.tenant_id)

    @property
    def tenant_id(self) -> str:
        return self.tenant.tenant_id

    @property
    def cache_prefix(self) -> str:
        """Prefix applied to every cache, rate-limit and idempotency key."""
        return f"t:{self.tenant_id}:"

    @property
    def effective_pas_vendor(self) -> str:
        return self.pas_vendor or self.tenant.pas_vendor or "generic"

    def cache_key(self, *parts: str) -> str:
        return self.cache_prefix + ":".join(parts)

    def require_plugin(self, plugin_id: str) -> None:
        self.tenant.assert_plugin_enabled(plugin_id)

    def as_log_context(self) -> dict[str, Any]:
        return {
            "tenantId": self.tenant_id,
            "subject": self.principal.subject,
            "correlationId": self.correlation_id,
            "pasVendor": self.effective_pas_vendor,
        }


_CURRENT_CONTEXT: ContextVar[TenantContext | None] = ContextVar("pas_tenant_context", default=None)


def set_tenant_context(ctx: TenantContext) -> "object":
    """Install ``ctx`` as the ambient context; returns a reset token."""
    return _CURRENT_CONTEXT.set(ctx)


def reset_tenant_context(token: object) -> None:  # noqa: ANN401
    _CURRENT_CONTEXT.reset(token)  # type: ignore[arg-type]


def current_context() -> TenantContext:
    """Return the active context or raise if called outside a request."""
    ctx = _CURRENT_CONTEXT.get()
    if ctx is None:
        raise PasError(
            ErrorCode.INTERNAL,
            "No tenant context bound to the current execution context",
        )
    return ctx


def current_tenant_id() -> str:
    """Convenience accessor used by repositories and audit sinks."""
    ctx = _CURRENT_CONTEXT.get()
    return ctx.tenant_id if ctx else "system"


def maybe_current_context() -> TenantContext | None:
    return _CURRENT_CONTEXT.get()


@dataclass(slots=True)
class InMemoryTenantRegistry:
    """Process-local tenant registry.

    The production implementation is :class:`pas_core.db.repositories.TenantRepository`
    backed by PostgreSQL; this exists so unit tests and single-container demos run
    without a database.  Both satisfy :class:`TenantRegistry`.
    """

    _tenants: dict[str, Tenant] = field(default_factory=dict)

    def register(self, tenant: Tenant) -> Tenant:
        self._tenants[tenant.tenant_id] = tenant
        return tenant

    def get(self, tenant_id: str) -> Tenant | None:
        return self._tenants.get(tenant_id)

    def require(self, tenant_id: str) -> Tenant:
        tenant = self.get(tenant_id)
        if tenant is None:
            raise PasError(
                ErrorCode.VALIDATION_FAILED,
                f"Unknown tenant '{tenant_id}'",
                {"tenantId": tenant_id},
            )
        return tenant

    def list(self) -> list[Tenant]:
        return sorted(self._tenants.values(), key=lambda t: t.tenant_id)

    def suspend(self, tenant_id: str) -> Tenant:
        return self.register(self.require(tenant_id).with_overrides(status=TenantStatus.SUSPENDED))

    def activate(self, tenant_id: str) -> Tenant:
        return self.register(self.require(tenant_id).with_overrides(status=TenantStatus.ACTIVE))

    def remove(self, tenant_id: str) -> None:
        self._tenants.pop(tenant_id, None)


class TenantRegistry(InMemoryTenantRegistry):
    """Interface implemented by both in-memory and database-backed registries."""


def build_context(
    tenant: Tenant,
    principal: RequestPrincipal,
    *,
    pas_vendor: str | None = None,
    correlation_id: str | None = None,
) -> TenantContext:
    """Convenience factory used by tests, MCP sessions and background workers."""
    return TenantContext(
        tenant=tenant,
        principal=principal,
        pas_vendor=pas_vendor,
        correlation_id=correlation_id or uuid.uuid4().hex,
    )


SYSTEM_PRINCIPAL = RequestPrincipal(
    subject="system",
    tenant_id="system",
    scopes=frozenset({"*"}),
    roles=("platform-operator",),
    auth_method="system",
)


def system_context(tenant_id: str = "system") -> TenantContext:
    """Context for background workers (workflow steps, CDC consumers)."""
    tenant = Tenant(tenant_id=tenant_id, legal_name=f"System ({tenant_id})")
    return TenantContext(
        tenant=tenant,
        principal=replace(SYSTEM_PRINCIPAL, tenant_id=tenant_id),
    )
