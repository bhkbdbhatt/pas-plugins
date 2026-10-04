"""Model monitoring: drift, stability and the champion/challenger verdict.

A model that is not watched is a model that will quietly go bad. Three things are
checked here, and each produces an actionable finding rather than a dashboard
number nobody reads:

1. **Feature drift** - population stability index against the training baseline,
   per feature, with the features responsible for the most drift named first.
2. **Discrimination and calibration** - AUC and observed-vs-predicted bands.
3. **Champion vs challenger** - the promotion recommendation, which is refused by
   default when the challenger wins on AUC but loses on calibration, since a model
   that ranks well and prices badly is not promotable.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pas_plugins.plugin3_auw.models import Submission
from pas_plugins.plugin3_auw.scoring import (
    FEATURE_NAMES,
    ScoringEngine,
    build_features,
    roc_auc,
)
from pas_plugins.plugin3_auw.settings import Plugin3Settings

# PSI bands below 0.1 are conventionally "no meaningful shift".
PSI_NEGLIGIBLE = 0.10
PSI_MODERATE = 0.20


def population_stability_index(
    baseline: Sequence[float], current: Sequence[float], bins: int = 10
) -> float:
    """Population Stability Index between two samples of one feature.

    PSI is sum((current% - baseline%) * ln(current% / baseline%)) over bins.

    Bins are proportional shares, smoothed with a half-count prior so an empty bin
    cannot produce a runaway log term. Without that smoothing a feature that is
    mostly zeros - which is most underwriting features - reports a PSI in the
    tens on a bin boundary artefact rather than on any real shift, and the number
    stops being read.
    """
    if len(baseline) < bins or len(current) < bins:
        return 0.0
    edges = [min(baseline) + i * (max(baseline) - min(baseline)) / bins for i in range(bins + 1)]
    edges[0] -= 1e-9
    edges[-1] += 1e-9
    baseline_counts = _counts(baseline, edges)
    current_counts = _counts(current, edges)

    prior = 0.5
    denominator_b = len(baseline) + prior * bins
    denominator_c = len(current) + prior * bins
    psi = 0.0
    for b, c in zip(baseline_counts, current_counts):
        p_b = (b + prior) / denominator_b
        p_c = (c + prior) / denominator_c
        psi += (p_c - p_b) * math.log(p_c / p_b)
    return round(max(psi, 0.0), 6)


def _counts(values: Sequence[float], edges: Sequence[float]) -> list[int]:
    counts = [0] * (len(edges) - 1)
    for value in values:
        for index in range(len(edges) - 1):
            if edges[index] <= value < edges[index + 1]:
                counts[index] += 1
                break
        else:
            counts[-1] += 1
    return counts


def _psi_band(psi: float) -> str:
    if psi < PSI_NEGLIGIBLE:
        return "stable"
    if psi < PSI_MODERATE:
        return "moderateShift"
    return "significantShift"


@dataclass(slots=True)
class FeatureDrift:
    feature: str
    psi: float
    band: str
    baseline_mean: float
    current_mean: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "psi": self.psi,
            "band": self.band,
            "baselineMean": round(self.baseline_mean, 6),
            "currentMean": round(self.current_mean, 6),
            "meanShift": round(self.current_mean - self.baseline_mean, 6),
        }


@dataclass(slots=True)
class MonitoringReport:
    """The full monitoring picture for one window."""

    window_start: datetime
    window_end: datetime
    sample_size: int
    feature_drift: list[FeatureDrift] = field(default_factory=list)
    champion_auc: float = 0.0
    challenger_auc: float = 0.0
    calibration_gap: float = 0.0
    recommendation: str = "hold"
    findings: list[str] = field(default_factory=list)
    retrain_recommended: bool = False
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def drifted_features(self) -> list[FeatureDrift]:
        return [d for d in self.feature_drift if d.band != "stable"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "windowStart": self.window_start.isoformat(),
            "windowEnd": self.window_end.isoformat(),
            "sampleSize": self.sample_size,
            "featureDrift": [d.to_dict() for d in self.feature_drift],
            "driftedFeatureCount": len(self.drifted_features),
            "championAuc": self.champion_auc,
            "challengerAuc": self.challenger_auc,
            "calibrationGap": round(self.calibration_gap, 6),
            "recommendation": self.recommendation,
            "findings": self.findings,
            "retrainRecommended": self.retrain_recommended,
            "generatedAt": self.generated_at.isoformat(),
        }


class MonitoringEngine:
    """Computes drift and the promotion recommendation over a submission window."""

    def __init__(self, settings: Plugin3Settings | None = None, scoring: ScoringEngine | None = None) -> None:
        self._settings = settings or Plugin3Settings()
        self._scoring = scoring or ScoringEngine(self._settings)

    def baseline_features(self) -> dict[str, list[float]]:
        """Feature distributions from the model's own training book.

        Comparing production drift against the training book is the honest
        baseline. Comparing against recent production instead hides drift that
        happened gradually before monitoring started.
        """
        from pas_plugins.plugin3_auw.scoring import synthetic_book

        baseline: dict[str, list[float]] = {name: [] for name in FEATURE_NAMES}
        for features, _label in synthetic_book(samples=1200, seed=7717):
            for name in FEATURE_NAMES:
                baseline[name].append(features[name])
        return baseline

    def report(
        self,
        submissions: list[Submission],
        signals: dict[str, dict[str, float]],
        labels: dict[str, int] | None = None,
    ) -> MonitoringReport:
        """Build a monitoring report for a window of scored submissions."""
        now = datetime.now(UTC)
        window = timedelta(days=self._settings.monitoring_window_days)
        report = MonitoringReport(
            window_start=now - window,
            window_end=now,
            sample_size=len(submissions),
        )
        if not submissions:
            report.findings.append("noSubmissionsInWindow")
            return report

        baseline = self.baseline_features()
        current: dict[str, list[float]] = {name: [] for name in FEATURE_NAMES}
        for submission in submissions:
            features = build_features(submission, signals.get(submission.submission_id, {}))
            for name in FEATURE_NAMES:
                current[name].append(features[name])

        for name in FEATURE_NAMES:
            psi = population_stability_index(baseline[name], current[name])
            report.feature_drift.append(
                FeatureDrift(
                    feature=name,
                    psi=psi,
                    band=_psi_band(psi),
                    baseline_mean=sum(baseline[name]) / len(baseline[name]),
                    current_mean=sum(current[name]) / len(current[name]),
                )
            )
        report.feature_drift.sort(key=lambda d: d.psi, reverse=True)

        champion_scores: list[float] = []
        challenger_scores: list[float] = []
        observed: list[int] = []
        for submission in submissions:
            submission_signals = signals.get(submission.submission_id, {})
            both = self._scoring.score_both(submission, submission_signals)
            champion_scores.append(both["champion"].score)
            challenger_scores.append(both["challenger"].score)
            if labels and submission.submission_id in labels:
                observed.append(labels[submission.submission_id])

        if observed:
            report.champion_auc = roc_auc(
                [(dict(), label) for label in observed], champion_scores[: len(observed)]
            )
            report.challenger_auc = roc_auc(
                [(dict(), label) for label in observed], challenger_scores[: len(observed)]
            )
            report.calibration_gap = _calibration_gap(
                champion_scores[: len(observed)], observed, bins=5
            )

        report.recommendation, report.retrain_recommended = self._recommend(report)
        report.findings = self._findings(report)
        return report

    def _recommend(self, report: MonitoringReport) -> tuple[str, bool]:
        """Decide whether to promote the challenger and/or retrain."""
        significant = [d for d in report.feature_drift if d.band == "significantShift"]
        moderate = [d for d in report.feature_drift if d.band == "moderateShift"]
        champion_wins = (
            report.champion_auc > 0 and report.challenger_auc > 0
            and report.champion_auc >= report.challenger_auc
        )

        retrain = bool(significant) or report.sample_size < self._settings.min_submissions_for_model

        if report.champion_auc > 0 and report.challenger_auc > report.champion_auc:
            if report.calibration_gap > 0.15:
                return "holdChallengerBetterRanksButMiscalibrates", retrain
            return "promoteChallenger", retrain
        if champion_wins and report.sample_size >= self._settings.min_submissions_for_model:
            return "keepChampion", retrain
        if moderate:
            return "holdMonitorDrift", retrain
        return "hold", retrain

    @staticmethod
    def _findings(report: MonitoringReport) -> list[str]:
        findings: list[str] = []
        for drift in report.drifted_features[:5]:
            findings.append(f"drift:{drift.feature}={drift.band}(psi={drift.psi})")
        if report.champion_auc and report.challenger_auc > report.champion_auc:
            findings.append("challengerOutperformsChampionOnAuc")
        if report.calibration_gap > 0.15:
            findings.append("championMiscalibrated")
        if report.sample_size < 200:
            findings.append("insufficientSampleForPromotion")
        return findings


def _calibration_gap(predictions: Sequence[float], labels: Sequence[int], bins: int = 5) -> float:
    """Mean absolute gap between predicted and observed rate per decile band."""
    if not predictions:
        return 0.0
    width = 1.0 / bins
    total = 0.0
    counted = 0
    for index in range(bins):
        low, high = index * width, (index + 1) * width
        in_band = [p for p in predictions if low <= p < high]
        observed_band = [label for p, label in zip(predictions, labels) if low <= p < high]
        if len(observed_band) < 5:
            continue
        predicted_rate = sum(in_band) / len(in_band)
        observed_rate = sum(observed_band) / len(observed_band)
        total += abs(predicted_rate - observed_rate)
        counted += 1
    return total / counted if counted else 0.0


__all__ = [
    "FeatureDrift",
    "MonitoringEngine",
    "MonitoringReport",
    "PSI_MODERATE",
    "PSI_NEGLIGIBLE",
    "population_stability_index",
]