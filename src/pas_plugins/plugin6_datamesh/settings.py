"""Configuration for plugin 6 - the unified data foundation."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from pas_core.config import PluginSettings

MaskingMode = Literal["enforce", "observe", "off"]


class Plugin6Settings(PluginSettings):
    """Environment-driven configuration for the data mesh plugin."""

    plugin_id: str = "plugin6"
    plugin_version: str = "1.0.0"
    service_name: str = "pas-data-mesh"

    feature_store: Literal["memory", "redis", "feast"] = "memory"
    feature_ttl_seconds: int = Field(default=900, ge=30, le=86_400)
    feature_baseline_window_days: int = Field(default=30, ge=1, le=365)
    pii_masking: MaskingMode = Field(
        default="enforce",
        description=(
            "enforce: mask on every read unless the caller holds an unmasking scope. "
            "observe: log what would be masked. off: return raw values (never use in production)."
        ),
    )
    unmask_scope: str = Field(
        default="pii:unmask",
        description="OAuth scope required to receive unmasked PII.",
    )
    lineage_enabled: bool = True
    max_ingest_batch: int = Field(default=10_000, ge=1, le=1_000_000)
    golden_record_match_threshold: float = Field(
        default=0.86, ge=0.0, le=1.0,
        description="Minimum similarity score for automatic entity match.",
    )
    quality_gate_threshold: float = Field(
        default=0.95, ge=0.0, le=1.0,
        description="Overall dataset quality score below which promotion to golden is blocked.",
    )
    iceberg_catalog: str = "pas-unified"
    s3_bucket: str = "pas-data-mesh-lake"
    documentation_url: str = "https://docs.pas-plugins.io/plugins/data-mesh"


__all__ = ["Plugin6Settings", "MaskingMode"]
