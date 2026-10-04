"""Configuration for plugin 2 - the IFRS 17 and regulatory automation engine."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from pas_core.config import PluginSettings

DiscountRateBasis = Literal["locked-in", "updated"]
ConvergenceMode = Literal["full", "reduced"]


class Plugin2Settings(PluginSettings):
    """Environment-driven configuration for the IFRS 17 engine."""

    plugin_id: str = "plugin2"
    plugin_version: str = "1.0.0"
    service_name: str = "pas-ifrs17-regulatory"

    functional_currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    discount_rate_basis: DiscountRateBasis = Field(
        default="locked-in",
        description=(
            "locked-in: the rate at initial recognition, per IFRS 17 paragraph B72. "
            "updated: the current market rate at each reporting date. Carriers hold "
            "both sets of curves, so both must be supported."
        ),
    )
    monte_carlo_paths: int = Field(default=2000, ge=100, le=1_000_000)
    monte_carlo_seed: int = Field(default=20_260_101, ge=0)
    monte_carlo_confidence: float = Field(default=0.95, gt=0.0, lt=1.0)
    max_workers: int = Field(default=4, ge=1, le=64)
    convergence_tolerance: float = Field(default=1e-6, gt=0.0)
    convergence_mode: ConvergenceMode = Field(
        default="reduced",
        description=(
            "reduced: one bootstrap iteration, used for daily production runs. "
            "full: iterate until the CSM change converges, used before a filing."
        ),
    )
    projection_horizon_years: int = Field(default=80, ge=1, le=120)
    curve_points: int = Field(default=33, ge=5, le=200)
    disclosure_formats: list[str] = Field(
        default_factory=lambda: ["xlsx", "json", "pdf", "xbrl"]
    )
    write_reserves_back_to_pas: bool = Field(
        default=False,
        description="Reserved totals back to the PAS. Off by default; a filing-grade "
                    "figure should be reviewed before it overwrites the core system.",
    )
    documentation_url: str = "https://docs.pas-plugins.io/plugins/ifrs17"


__all__ = ["Plugin2Settings"]
