"""Plugin 3 - AI-Powered Accelerated Underwriting (AUW) Workbench.

38% of carriers cite legacy IT as their primary underwriting obstacle, and 45% say
self-service tools would move speed most. This plugin attacks both: it takes the
manual steps out of a submission and gives the underwriter a reason for every
recommendation.

Components
----------
1. **Submission intake and triage** - complexity scoring that routes each
   submission to auto-decide, fast-track or a human.
2. **Document extraction** - field-level extraction from unstructured documents,
   with provenance for every extracted value.
3. **Data enrichment** - MIB, prescription and health signals, each gated on
   consent.
4. **ML risk scoring** - gradient-boosted scoring plus SHAP explanations, with a
   logistic-regression challenger for the regulatory baseline.
5. **Appetite rules and the decision API** - versioned rules, an explainable
   decision, and an audited underwriter override.
6. **Model monitoring** - drift detection and retraining triggers.

The governing constraint is *explainability*. A life carrier cannot accept an
undeclined applicant on a model's say-so, so every automated decision carries
reason codes, the rules that fired, and the model's feature contributions.
"""

from __future__ import annotations

from pas_core.config import PluginSettings


class Plugin3Settings(PluginSettings):
    """Configuration for the AUW workbench."""

    plugin_id: str = "plugin3"
    plugin_version: str = "1.0.0"
    service_name: str = "pas-auw-workbench"

    model_version: str = "auw-gbm-1.4.0"
    model_algorithm: str = "gradientBoostedTrees"
    auto_decide_threshold: float = 0.90
    """Score at or above which a submission may be auto-decided."""
    fast_track_threshold: float = 0.65
    """Score at or above which a submission may be fast-tracked without an agent review."""
    decline_threshold: float = 0.25
    """Score at or below which a submission may be auto-declined."""
    drift_psi_threshold: float = 0.20
    monitoring_window_days: int = 30
    min_submissions_for_model: int = 200
    """Below this the challenger model is not trained and the baseline is used."""
    require_explanation: bool = True
    """Refuse to emit a decision that carries no explanation."""
    allow_auto_decline: bool = False
    """Declines always route to a human unless a carrier explicitly opts in."""
    documentation_url: str = "https://docs.pas-plugins.io/plugins/auw"


__all__ = ["Plugin3Settings"]
