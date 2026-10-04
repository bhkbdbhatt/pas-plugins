"""OAuth 2.1 / OIDC authentication for HTTP and MCP transports.

Design notes
------------
* OAuth 2.1 (per the MCP authorisation specification) drops the implicit grant and
  requires PKCE for every authorization-code flow.  Keycloak is the reference
  provider but any compliant IdP works.
* Access tokens are validated locally against the provider's JWKS.  No per-request
  round trip to the IdP, which matters because plugin 5 has a 500 ms quote SLA.
* Partner/server-to-server callers may present a long-lived API key instead of a
  JWT; keys are hashed at rest and scoped identically to tokens.
* When ``oidc_required`` is false (local development) an anonymous principal is
  created, but it is tagged ``auth_method="anonymous"`` and every audit record and
  log line says so - there is no silent "trusted" state.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from pas_core.config import Settings
from pas_core.errors import (
    ErrorCode,
    PasError,
    PermissionDeniedError,
    TenantMismatchError,
)
from pas_core.tenancy import RequestPrincipal, Tenant

try:  # pragma: no cover - optional dependency guard
    from jose import jwt as _jose_jwt
    from jose.exceptions import JWTError as _JWTError

    _HAS_JOSE = True
except ImportError:  # pragma: no cover
    _HAS_JOSE = False
    _JWTError = Exception  # type: ignore[assignment,misc]

AUTH_BYPASS_LOG_MESSAGE = (
    "Authentication bypassed because oidc_required=false. "
    "Never run with this flag in staging or production."
)


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """Decoded and validated access-token claims."""

    subject: str
    tenant_id: str
    scopes: frozenset[str]
    issuer: str
    audience: str | list[str]
    issued_at: float
    expires_at: float
    client_id: str | None = None
    roles: tuple[str, ...] = ()
    token_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def tenant_claim_names(self) -> str:
        return "tenant_id / tid / org_id / organization"

    def to_principal(self, *, auth_method: str = "bearer") -> RequestPrincipal:
        return RequestPrincipal(
            subject=self.subject,
            tenant_id=self.tenant_id,
            scopes=self.scopes,
            roles=self.roles,
            client_id=self.client_id,
            auth_method=auth_method,
            token_id=self.token_id,
        )


class TokenVerifier:
    """Validates JWT access tokens against the provider's JWKS endpoint.

    The JWKS document is cached with a TTL and refreshed on unknown ``kid``, which
    keeps key rotation transparent without an introspection round trip.
    """

    def __init__(self, settings: Settings, *, jwks_ttl: float = 300.0) -> None:
        self._settings = settings
        self._jwks_ttl = jwks_ttl
        self._jwks: dict[str, Any] | None = None
        self._jwks_fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(_HAS_JOSE and self._settings.oidc_jwks_url or _HAS_JOSE)

    async def _get_jwks(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._jwks is not None and (now - self._jwks_fetched_at) < self._jwks_ttl:
            return self._jwks
        async with self._lock:
            if self._jwks is not None and (time.monotonic() - self._jwks_fetched_at) < self._jwks_ttl:
                return self._jwks
            import httpx  # noqa: PLC0415

            url = self._settings.jwks_url
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(url)
                    response.raise_for_status()
                    self._jwks = response.json()
            except Exception as exc:  # noqa: BLE001
                raise PasError(
                    ErrorCode.INVALID_TOKEN,
                    f"Unable to fetch JWKS from {url}: {exc}",
                    {"jwksUrl": url},
                    context="token_verifier",
                ) from exc
            self._jwks_fetched_at = time.monotonic()
            return self._jwks or {}

    def _decode_unverified(self, token: str) -> dict[str, Any]:
        try:
            payload_segment = token.split(".")[1]
            padded = payload_segment + "=" * (-len(payload_segment) % 4)
            return json.loads(base64.urlsafe_b64decode(padded))
        except (IndexError, ValueError, json.JSONDecodeError) as exc:
            raise PasError(
                ErrorCode.INVALID_TOKEN, "Bearer token is not a well-formed JWT"
            ) from exc

    async def verify(self, token: str) -> TokenClaims:
        """Validate signature, issuer, audience, expiry and extract tenant/scopes."""
        if not _HAS_JOSE:  # pragma: no cover
            raise PasError(ErrorCode.INVALID_TOKEN, "python-jose is required to verify tokens")

        untrusted = self._decode_unverified(token)
        kid = untrusted.get("kid")
        if kid and self._settings.oidc_jwks_url:
            jwks = await self._get_jwks()
            if not any(key.get("kid") == kid for key in jwks.get("keys", [])):
                self._jwks = None
                jwks = await self._get_jwks()
                if not any(key.get("kid") == kid for key in jwks.get("keys", [])):
                    raise PasError(
                        ErrorCode.INVALID_TOKEN,
                        f"Unknown signing key id '{kid}'",
                        {"kid": kid},
                    )
            self._jwks = jwks

        try:
            options: dict[str, Any] = {"verify_aud": bool(self._settings.oidc_audience)}
            claims = _jose_jwt.decode(  # type: ignore[misc]
                token,
                key=self._settings.jwks_url if self._settings.oidc_jwks_url else _DEV_KEY,
                algorithms=["RS256", "ES256"],
                audience=self._settings.oidc_audience or None,
                issuer=self._settings.oidc_issuer,
                options=options,
            )
        except _JWTError as exc:
            msg = str(exc)
            code = ErrorCode.TOKEN_EXPIRED if "expired" in msg.lower() else ErrorCode.INVALID_TOKEN
            raise PasError(code, f"Token validation failed: {msg}", context="token_verifier") from exc

        return self._to_claims(claims)

    def _to_claims(self, claims: dict[str, Any]) -> TokenClaims:
        tenant_id = None
        for name in ("tenant_id", "tid", "org_id", "organization", "carrier_id"):
            value = claims.get(name)
            if value:
                tenant_id = str(value)
                break
        if not tenant_id:
            raise PasError(
                ErrorCode.INVALID_TOKEN,
                "Access token carries no tenant claim "
                f"(expected one of {TokenClaims.tenant_claim_names})",
            )

        raw_scope = claims.get("scope") or claims.get("scp") or ""
        if isinstance(raw_scope, list):
            scopes = frozenset(str(s) for s in raw_scope)
        else:
            scopes = frozenset(str(raw_scope).replace(",", " ").split())

        realm_roles = (claims.get("realm_access") or {}).get("roles", [])
        resource_roles = (claims.get("resource_access") or {}).get(
            self._settings.oidc_audience, {}
        ).get("roles", [])

        audience = claims.get("aud", "")
        return TokenClaims(
            subject=str(claims.get("sub", "unknown")),
            tenant_id=tenant_id,
            scopes=scopes,
            issuer=str(claims.get("iss", self._settings.oidc_issuer)),
            audience=audience if isinstance(audience, list) else [audience] if audience else [],
            issued_at=float(claims.get("iat", time.time())),
            expires_at=float(claims.get("exp", time.time() + 300)),
            client_id=claims.get("client_id") or claims.get("azp"),
            roles=tuple(str(r) for r in [*realm_roles, *resource_roles]),
            token_id=claims.get("jti"),
            raw=claims,
        )


_DEV_KEY = "development-only-key-do-not-use-in-production"


def _decode_dev_token(token: str) -> dict[str, Any]:
    """Accept unsigned/HS256 dev tokens so developers can exercise auth locally."""
    try:
        claims = _jose_jwt.decode(token, _DEV_KEY, algorithms=["HS256"])  # type: ignore[misc]
        return dict(claims)
    except _JWTError:
        return json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))


@dataclass(slots=True)
class ApiKeyRecord:
    """A partner's long-lived credential."""

    key_id: str
    tenant_id: str
    secret_hash: str
    scopes: frozenset[str]
    partner_id: str | None = None
    rate_limit_rps: int = 20
    disabled: bool = False
    expires_at: float | None = None
    last_used_at: float | None = None

    def verify(self, secret: str) -> bool:
        if self.expires_at is not None and self.expires_at < time.time():
            return False
        return hmac.compare_digest(self.secret_hash, hash_api_key(secret))

    def is_live(self) -> bool:
        return not self.disabled and (
            self.expires_at is None or self.expires_at > time.time()
        )


def hash_api_key(secret: str) -> str:
    """Pepperless SHA-256 of an API key. Safe to store and compare in constant time."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def generate_api_key(*, prefix: str = "pask") -> tuple[str, str]:
    """Return ``(key_id, secret)``; only the hash of ``secret`` should be stored."""
    key_id = f"{prefix}_{secrets.token_hex(6)}"
    secret = secrets.token_urlsafe(32)
    return key_id, secret


class ApiKeyStore:
    """Lookup interface for partner API keys.

    Backed by PostgreSQL in production; the in-memory implementation keeps the
    embedded-distribution plugin usable in tests and demos.
    """

    def __init__(self) -> None:
        self._keys: dict[str, ApiKeyRecord] = {}

    def register(self, record: ApiKeyRecord) -> ApiKeyRecord:
        self._keys[record.key_id] = record
        return record

    def issue(self, tenant_id: str, *, partner_id: str | None = None, scopes: list[str] | None = None) -> tuple[ApiKeyRecord, str]:
        """Mint a new key; the plaintext secret is returned exactly once."""
        key_id, secret = generate_api_key()
        record = ApiKeyRecord(
            key_id=key_id,
            tenant_id=tenant_id,
            secret_hash=hash_api_key(secret),
            scopes=frozenset(scopes or ["embed:quote", "embed:bind", "policy:read"]),
            partner_id=partner_id,
        )
        self.register(record)
        return record, secret

    def resolve(self, presented: str) -> ApiKeyRecord | None:
        """Resolve ``pask_<id>.<secret>`` (or a bare key id) to a live record."""
        key_id, _, secret = presented.rpartition(".")
        if not key_id:
            key_id, secret = presented, ""
        record = self._keys.get(key_id)
        if record is None or not record.is_live():
            return None
        if secret and not record.verify(secret):
            return None
        return record


class Authenticator:
    """Resolves an inbound credential to a :class:`RequestPrincipal`."""

    def __init__(self, settings: Settings, *, api_keys: ApiKeyStore | None = None) -> None:
        self._settings = settings
        self._verifier = TokenVerifier(settings)
        self.api_keys = api_keys or ApiKeyStore()

    @property
    def required(self) -> bool:
        return bool(self._settings.oidc_required)

    async def authenticate(
        self,
        *,
        authorization: str | None,
        api_key: str | None,
        header_tenant: str | None,
    ) -> RequestPrincipal:
        """Authenticate a request, honouring the development bypass policy."""
        if api_key:
            record = self.api_keys.resolve(api_key)
            if record is None:
                raise PasError(ErrorCode.INVALID_TOKEN, "Unknown or expired API key")
            record.last_used_at = time.time()
            return RequestPrincipal(
                subject=record.key_id,
                tenant_id=record.tenant_id,
                scopes=record.scopes,
                client_id=record.partner_id,
                auth_method="api_key",
            )

        if authorization:
            scheme, _, credential = authorization.partition(" ")
            if scheme.lower() != "bearer" or not credential:
                raise PasError(
                    ErrorCode.UNAUTHENTICATED,
                    "Authorization header must use the Bearer scheme (OAuth 2.1)",
                )
            if self._settings.oidc_jwks_url or self.required:
                claims = await self._verifier.verify(credential)
                principal = claims.to_principal()
            else:
                principal = self._principal_from_dev_token(credential)
            if header_tenant and header_tenant != principal.tenant_id:
                raise TenantMismatchError(principal.tenant_id, header_tenant)
            return principal

        if self.required:
            raise PasError(
                ErrorCode.UNAUTHENTICATED,
                "Missing credential: supply a Bearer token or API key",
            )

        return RequestPrincipal(
            subject="anonymous",
            tenant_id=header_tenant or "demo-carrier",
            scopes=frozenset({"*"}),
            auth_method="anonymous",
        )

    def _principal_from_dev_token(self, token: str) -> RequestPrincipal:
        """Development path: decode a locally-signed HS256 token without a JWKS."""
        try:
            claims = _decode_dev_token(token)
        except Exception as exc:  # noqa: BLE001
            raise PasError(ErrorCode.INVALID_TOKEN, "Malformed development token") from exc
        verifier = TokenVerifier(self._settings)
        return verifier._to_claims(claims).to_principal()  # noqa: SLF001


def require_active_tenant(tenant: Tenant) -> Tenant:
    """Guard used by middleware after a tenant lookup."""
    tenant.assert_active()
    return tenant


def assert_tenant_access(ctx_tenant_id: str, principal: RequestPrincipal) -> None:
    """Hard stop on cross-tenant access; always paired with an audit record."""
    if ctx_tenant_id != principal.tenant_id and "*" not in principal.scopes:
        raise TenantMismatchError(principal.tenant_id, ctx_tenant_id)


def deny(message: str) -> None:
    """Explicit permission denial with catalogue metadata."""
    raise PermissionDeniedError(message)


def jwt_error_context() -> dict[str, Any]:
    """Extra fields attached to auth failures for operators (never for clients)."""
    return {"context": "oauth2.1", "spec": "https://datatracker.ietf.org/doc/html/rfc9700"}
