"""Portable MCP tool, resource and prompt specifications.

These types are the canonical description of what a plugin offers an AI agent.
They serialise to the MCP wire format but are defined independently of the SDK so
that the catalogue can be published, diffed, signed and tested on its own.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pas_core.errors import ValidationError

TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
# ``pas://`` scheme. Braces delimit RFC 6570-style template variables, whose
# names follow the camelCase convention used across the canonical models.
RESOURCE_URI_PATTERN = re.compile(r"^pas://[A-Za-z0-9_{}./-]+$")

ToolHandler = Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class McpToolAnnotations:
    """Behavioural hints declared to the agent.

    These are not decorative: an agent uses ``readOnlyHint`` and
    ``destructiveHint`` to decide whether to auto-approve a call or escalate to a
    human. Getting them wrong is a safety problem, not a documentation problem.
    """

    read_only: bool = True
    destructive: bool = False
    idempotent: bool = True
    open_world_hint: bool = False

    def to_wire(self) -> dict[str, Any]:
        return {
            "title": "",
            "readOnlyHint": self.read_only,
            "destructiveHint": self.destructive,
            "idempotentHint": self.idempotent,
            "openWorldHint": self.open_world_hint,
        }


@dataclass(frozen=True, slots=True)
class McpToolSpec:
    """One callable capability exposed to an agent.

    Names use snake_case (``policy_get``, ``premium_calculate``) because they
    become function names in the agent's own runtime; every plugin therefore
    namespaces its tools consistently rather than using vendor jargon.
    """

    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler | None = field(default=None, repr=False)
    output_schema: dict[str, Any] | None = None
    required_scopes: tuple[str, ...] = ()
    annotations: McpToolAnnotations = field(default_factory=McpToolAnnotations)
    operation_id: str | None = None
    plugin_id: str = "pas-core"
    tags: tuple[str, ...] = ()
    examples: tuple[dict[str, Any], ...] = ()
    error_codes: tuple[str, ...] = ()
    rate_limit_policy: str = "mcp-tool"

    def __post_init__(self) -> None:
        if not TOOL_NAME_PATTERN.match(self.name):
            msg = (
                f"invalid MCP tool name {self.name!r}: expected snake_case, 2-63 chars"
            )
            raise ValidationError(msg, name=self.name)
        if not self.input_schema.get("type"):
            object.__setattr__(self, "input_schema", {**self.input_schema, "type": "object"})

    @property
    def destructive(self) -> bool:
        return self.annotations.destructive or not self.annotations.idempotent

    def to_wire(self) -> dict[str, Any]:
        """The MCP ``tools/list`` entry."""
        return {
            "name": self.name,
            "title": self.title or self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "outputSchema": self.output_schema,
            "annotations": self.annotations.to_wire(),
            "_meta": {
                "pas/plugin": self.plugin_id,
                "pas/operationId": self.operation_id,
                "pas/scopes": list(self.required_scopes),
                "pas/tags": list(self.tags),
                "pas/errorCodes": list(self.error_codes),
                "pas/rateLimitPolicy": self.rate_limit_policy,
            },
        }

    def to_schema_dict(self) -> dict[str, Any]:
        """Catalogue projection used by the management UI."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "operationId": self.operation_id,
            "pluginId": self.plugin_id,
            "tags": list(self.tags),
            "requiredScopes": list(self.required_scopes),
            "readOnly": self.annotations.read_only,
            "destructive": self.destructive,
            "idempotent": self.annotations.idempotent,
            "errorCodes": list(self.error_codes),
            "examples": list(self.examples),
            "inputSchema": self.input_schema,
            "outputSchema": self.output_schema,
        }

    def fingerprint(self) -> str:
        payload = json.dumps(
            {"name": self.name, "input": self.input_schema, "output": self.output_schema},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class McpResourceSpec:
    """A readable resource an agent can pull as context.

    Examples: ``pas://policies/{id}``, ``pas://catalog/products``,
    ``pas://ifrs17/csm-rollforward/{group}``.
    """

    uri_template: str
    name: str
    title: str
    description: str
    mime_type: str = "application/json"
    handler: Callable[..., Awaitable[Any]] | None = field(default=None, repr=False)
    plugin_id: str = "pas-core"
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not RESOURCE_URI_PATTERN.match(self.uri_template):
            msg = f"MCP resource URIs must use the pas:// scheme: {self.uri_template!r}"
            raise ValidationError(msg, uri=self.uri_template)

    @property
    def is_template(self) -> bool:
        return "{" in self.uri_template

    def to_wire(self) -> dict[str, Any]:
        return {
            "uriTemplate": self.uri_template,
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "mimeType": self.mime_type,
            "_meta": {"pas/plugin": self.plugin_id, "pas/tags": list(self.tags)},
        }

    def to_schema_dict(self) -> dict[str, Any]:
        return {
            "uriTemplate": self.uri_template,
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "mimeType": self.mime_type,
            "pluginId": self.plugin_id,
            "tags": list(self.tags),
            "isTemplate": self.is_template,
        }


@dataclass(frozen=True, slots=True)
class McpPromptSpec:
    """A reusable prompt template - the MCP equivalent of a saved playbook."""

    name: str
    title: str
    description: str
    template: str
    arguments: tuple[dict[str, Any], ...] = ()
    plugin_id: str = "pas-core"

    def __post_init__(self) -> None:
        if not TOOL_NAME_PATTERN.match(self.name):
            msg = f"invalid MCP prompt name {self.name!r}"
            raise ValidationError(msg, name=self.name)

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title or self.name,
            "description": self.description,
            "arguments": list(self.arguments),
            "_meta": {"pas/plugin": self.plugin_id},
        }

    def to_schema_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "arguments": list(self.arguments),
            "template": self.template,
            "pluginId": self.plugin_id,
        }

    def render(self, values: dict[str, Any]) -> str:
        """Substitute ``{argument}`` placeholders; unknown placeholders are left intact."""
        rendered = self.template
        for argument in self.arguments:
            name = str(argument.get("name", ""))
            if name in values:
                rendered = rendered.replace(f"{{{name}}}", str(values[name]))
        return rendered


@dataclass(frozen=True, slots=True)
class McpServerInfo:
    """Server metadata published in ``initialize``."""

    name: str
    version: str
    title: str = ""
    description: str = ""
    instructions: str = ""
    website_url: str = "https://docs.pas-plugins.io"
    vendor: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocolVersion": "2025-06-18",
            "serverName": self.name,
            "serverVersion": self.version,
            "title": self.title or self.name,
            "description": self.description,
            "instructions": self.instructions,
            "websiteUrl": self.website_url,
        }


DEFAULT_INSTRUCTIONS = """\
This server exposes a carrier's Policy Administration System as a set of atomic,
self-describing operations for Life and Annuity business.

How to use it well:
  1. Call the catalogue tools first (e.g. `catalogue_list_operations`) to see what
     is available for the connected carrier.
  2. Prefer the atomic tools over guessing. Every tool's description states the
     business intent and its error codes.
  3. Writes are not idempotent unless the tool's `idempotentHint` is true. Always
     send an `Idempotency-Key` for creates and binds.
  4. A tool result with `isError: true` carries a stable `code`. Retry only when
     `retryable` is true; otherwise fix the inputs or escalate to a human.
  5. Every call is written to the carrier's audit trail against your tenant. Do not
     attempt to act on another carrier's data.

All dates are ISO-8601. All amounts are decimal numbers in the stated currency.
Coverage and product terminology follows ACORD NGDS Life & Annuity.
"""
