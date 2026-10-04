"""Risk scoring with exact feature attribution.

Two models are maintained, because a carrier cannot run an unvalidated challenger
forever but also cannot defend a champion that nobody has tried to beat:

* **Champion** - an additive gradient-boosted stump ensemble. Because the
  ensemble is a sum of trees, each tree's output *is* its exact Shapley
  contribution. The attributions below are therefore not an approximation fitted
  after the fact (as in KernelSHAP); they are the model.
* **Challenger** - L2-regularised logistic regression. Its attributions are exact
  in log-odds space, and it is the baseline a regulator expects to see.

Both are fitted at import time against a deterministic synthetic book, so the
workbench is demonstrable and testable without a data warehouse. The trainer is
real code, not a stub: it greedily picks the split with the largest reduction in
squared error on the log-odds residual, which is what makes this a boosted model
rather than a lookup table.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Protocol

from pas_plugins.plugin3_auw.models import RiskScore, ScoreContribution, Submission
from pas_plugins.plugin3_auw.settings import Plugin3Settings

FEATURE_NAMES: tuple[str, ...] = (
    "ageNorm",
    "tobacco",
    "bmiNorm",
    "hazardNorm",
    "faceAmountNorm",
    "mibAdverse",
    "mibElevated",
    "rxScriptVolume",
    "rxPoorAdherence",
    "paramedOutstanding",
    "financialStress",
    "felonyConviction",
    "bankruptcyRecent",
    "earlyDeathHistory",
    "hazardousSports",
    "sanctionsMatch",
    "pepMatch",
)

# Midpoints used to normalise the numeric features into [0, 1].
_AGE_MID, _AGE_SCALE = 45.0, 25.0
_BMI_MID, _BMI_SCALE = 27.0, 13.0
_FACE_MID, _FACE_SCALE = 400_000.0, 600_000.0


def build_features(submission: Submission, signals: dict[str, float]) -> dict[str, float]:
    """Project a submission plus enrichment signals onto the feature vector."""
    applicant = submission.applicant
    features: dict[str, float] = {
        "ageNorm": _unit(applicant.age, _AGE_MID, _AGE_SCALE),
        "tobacco": 1.0 if applicant.tobacco_use else 0.0,
        "bmiNorm": _unit(applicant.bmi or 24.0, _BMI_MID, _BMI_SCALE),
        "hazardNorm": _unit(applicant.avocation.hazard_multiplier, 1.0, 1.6),
        "faceAmountNorm": _unit(submission.request.face_amount, _FACE_MID, _FACE_SCALE),
        "mibAdverse": signals.get("mibAdverse", 0.0),
        "mibElevated": signals.get("mibElevated", 0.0),
        "rxScriptVolume": signals.get("rxScriptVolume", 0.0),
        "rxPoorAdherence": signals.get("rxPoorAdherence", 0.0),
        "paramedOutstanding": signals.get("paramedOutstanding", 0.0),
        "financialStress": _financial_stress(submission),
        "felonyConviction": 1.0 if applicant.felony_conviction else 0.0,
        "bankruptcyRecent": 1.0 if 0 < applicant.bankruptcy_last_years <= 5 else 0.0,
        "earlyDeathHistory": 1.0 if applicant.family_history_early_death else 0.0,
        "hazardousSports": 1.0 if applicant.hazardous_sports else 0.0,
        "sanctionsMatch": signals.get("sanctionsMatch", 0.0),
        "pepMatch": signals.get("pepMatch", 0.0),
    }
    return {name: features.get(name, 0.0) for name in FEATURE_NAMES}


def _ranked(attribution: dict[str, float], features: dict[str, float]) -> list[ScoreContribution]:
    """Turn an attribution map into contributions ranked by absolute impact.

    Features with no measurable contribution are omitted: a zero on every case is
    noise in an explanation a human is trying to read.
    """
    material = [
        (name, value) for name, value in attribution.items() if abs(value) > 1e-9
    ]
    material.sort(key=lambda item: abs(item[1]), reverse=True)
    return [
        ScoreContribution(
            feature=name,
            value=round(features[name], 6),
            shap_value=round(value, 8),
            rank=rank,
        )
        for rank, (name, value) in enumerate(material, start=1)
    ]


def _financial_stress(submission: Submission) -> float:
    applicant = submission.applicant
    stress = 0.0
    if applicant.annual_income > 0 and submission.request.face_amount > applicant.annual_income * 20:
        stress += 0.5
    if applicant.net_worth < submission.request.face_amount * 0.5:
        stress += 0.3
    if applicant.bankruptcy_last_years > 0:
        stress += 0.3
    return min(stress, 1.0)


def _unit(value: float, midpoint: float, scale: float) -> float:
    return max(0.0, min(1.0, 0.5 + (value - midpoint) / (2.0 * scale)))


@dataclass(frozen=True, slots=True)
class Stump:
    """A single-node decision tree: split on one feature, adjust the log-odds."""

    feature: str
    threshold: float
    left_delta: float
    right_delta: float

    def contribution(self, features: dict[str, float]) -> float:
        return self.left_delta if features[self.feature] <= self.threshold else self.right_delta

    def used_feature(self) -> str:
        return self.feature


@dataclass(slots=True)
class FittedEnsemble:
    """A boosted ensemble of stumps with its fitted prior."""

    stumps: list[Stump] = field(default_factory=list)
    base_rate: float = 0.35
    learning_rate: float = 0.35
    train_roc_auc: float = 0.0
    train_samples: int = 0
    feature_gains: dict[str, float] = field(default_factory=dict)

    def raw(self, features: dict[str, float]) -> float:
        """Return the ensemble's log-odds output."""
        total = math.log(self.base_rate / (1.0 - self.base_rate))
        for stump in self.stumps:
            total += self.learning_rate * stump.contribution(features)
        return total

    def probability(self, features: dict[str, float]) -> float:
        return _sigmoid(self.raw(features))

    def attributions(self, features: dict[str, float]) -> dict[str, float]:
        """Exact Shapley values: for an additive model, each term is its own SHAP."""
        attribution = {name: 0.0 for name in FEATURE_NAMES}
        for stump in self.stumps:
            attribution[stump.feature] += self.learning_rate * stump.contribution(features)
        return attribution


@dataclass(slots=True)
class FittedLogistic:
    """L2-regularised logistic regression on standardised features."""

    weights: dict[str, float] = field(default_factory=dict)
    bias: float = 0.0
    means: dict[str, float] = field(default_factory=dict)
    scales: dict[str, float] = field(default_factory=dict)
    train_accuracy: float = 0.0
    train_roc_auc: float = 0.0

    def log_odds(self, features: dict[str, float]) -> float:
        total = self.bias
        for name, weight in self.weights.items():
            z = (features[name] - self.means.get(name, 0.0)) / (self.scales.get(name, 1.0) or 1.0)
            total += weight * z
        return total

    def probability(self, features: dict[str, float]) -> float:
        return _sigmoid(self.log_odds(features))

    def attributions(self, features: dict[str, float]) -> dict[str, float]:
        """Exact attribution for a linear-in-log-odds model, in log-odds units."""
        attribution: dict[str, float] = {}
        for name, weight in self.weights.items():
            z = (features[name] - self.means.get(name, 0.0)) / (self.scales.get(name, 1.0) or 1.0)
            attribution[name] = weight * z
        return attribution


class RiskScorer(Protocol):
    """The interface the workbench depends on."""

    version: str
    algorithm: str

    def score(self, submission: Submission, signals: dict[str, float]) -> RiskScore: ...


class GradientBoostedRiskModel:
    """Champion: additive boosted stumps."""

    algorithm = "gradientBoostedStumps"

    def __init__(self, ensemble: FittedEnsemble, version: str) -> None:
        self._ensemble = ensemble
        self.version = version

    def score(self, submission: Submission, signals: dict[str, float]) -> RiskScore:
        features = build_features(submission, signals)
        attribution = self._ensemble.attributions(features)
        contributions = _ranked(attribution, features)
        return RiskScore(
            score=round(self._ensemble.probability(features), 6),
            model_version=self.version,
            algorithm=self.algorithm,
            contributions=contributions,
            base_value=round(self._ensemble.base_rate, 6),
        )

    @property
    def ensemble(self) -> FittedEnsemble:
        return self._ensemble


class LogisticRegressionRiskModel:
    """Challenger: the interpretable baseline."""

    algorithm = "logisticRegression"

    def __init__(self, fitted: FittedLogistic, version: str) -> None:
        self._fitted = fitted
        self.version = version

    def score(self, submission: Submission, signals: dict[str, float]) -> RiskScore:
        features = build_features(submission, signals)
        attribution = self._fitted.attributions(features)
        return RiskScore(
            score=round(self._fitted.probability(features), 6),
            model_version=self.version,
            algorithm=self.algorithm,
            contributions=_ranked(attribution, features),
            base_value=round(_sigmoid(self._fitted.bias), 6),
        )

    @property
    def fitted(self) -> FittedLogistic:
        return self._fitted


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------


def synthetic_book(samples: int = 4000, seed: int = 20260101) -> list[tuple[dict[str, float], int]]:
    """A deterministic synthetic book with a known, monotone-ish risk function.

    Real interactions are included on purpose - tobacco interacts with age, hazard
    interacts with face amount - because a model fitted on purely additive data
    would make the SHAP attributions look better than they are.
    """
    import random

    rng = random.Random(seed)
    rows: list[tuple[dict[str, float], int]] = []
    for _ in range(samples):
        age = rng.gauss(45, 15)
        age = max(18.0, min(78.0, age))
        tobacco = 1.0 if rng.random() < 0.22 else 0.0
        bmi = max(16.0, min(52.0, rng.gauss(27, 6)))
        hazard = max(1.0, min(2.6, rng.gauss(1.5, 0.45)))
        face = max(25_000.0, min(6_000_000.0, rng.lognormvariate(12.9, 0.8)))
        mib_adverse = 1.0 if rng.random() < 0.08 else 0.0
        mib_elevated = 1.0 if (rng.random() < 0.2 and not mib_adverse) else 0.0
        rx_volume = max(0.0, min(1.0, rng.betavariate(2, 5)))
        rx_poor = 1.0 if rng.random() < 0.12 else 0.0
        paramed = 1.0 if (face > 1_000_000 or age > 60) and rng.random() < 0.7 else 0.0
        financial = 1.0 if rng.random() < 0.18 else 0.0
        felony = 1.0 if rng.random() < 0.03 else 0.0
        bankruptcy = 1.0 if rng.random() < 0.07 else 0.0
        early_death = 1.0 if rng.random() < 0.09 else 0.0
        hazardous = 1.0 if rng.random() < 0.11 else 0.0
        sanctions = 1.0 if rng.random() < 0.01 else 0.0
        pep = 1.0 if rng.random() < 0.008 else 0.0

        features = {
            "ageNorm": _unit(age, _AGE_MID, _AGE_SCALE),
            "tobacco": tobacco,
            "bmiNorm": _unit(bmi, _BMI_MID, _BMI_SCALE),
            "hazardNorm": _unit(hazard, 1.0, 1.6),
            "faceAmountNorm": _unit(face, _FACE_MID, _FACE_SCALE),
            "mibAdverse": mib_adverse,
            "mibElevated": mib_elevated,
            "rxScriptVolume": rx_volume,
            "rxPoorAdherence": rx_poor,
            "paramedOutstanding": paramed,
            "financialStress": financial,
            "felonyConviction": felony,
            "bankruptcyRecent": bankruptcy,
            "earlyDeathHistory": early_death,
            "hazardousSports": hazardous,
            "sanctionsMatch": sanctions,
            "pepMatch": pep,
        }

        logit = -3.1
        logit += 1.25 * max(0.0, age - 50) / 10.0
        logit += 1.55 * tobacco
        logit += 0.55 * max(0.0, bmi - 30) / 5.0
        logit += 1.05 * (hazard - 1.0)
        logit += 0.45 * math.log(max(face, 1.0) / 400_000.0)
        logit += 2.10 * mib_adverse
        logit += 0.85 * mib_elevated
        logit += 0.75 * rx_volume
        logit += 0.90 * rx_poor
        logit += 0.55 * paramed
        logit += 0.95 * financial
        logit += 1.30 * felony
        logit += 0.80 * bankruptcy
        logit += 1.15 * early_death
        logit += 0.70 * hazardous
        logit += 3.50 * sanctions
        logit += 2.80 * pep
        # Interactions.
        logit += 0.85 * tobacco * max(0.0, age - 45) / 15.0
        logit += 0.60 * max(0.0, hazard - 2.0)
        probability = _sigmoid(logit)
        label = 1 if rng.random() < probability else 0
        rows.append((features, label))
    return rows


def fit_ensemble(
    rows: list[tuple[dict[str, float], int]], *, rounds: int = 90, learning_rate: float = 0.25
) -> FittedEnsemble:
    """Greedily boost stumps on the log-odds residual.

    The residual changes every round, but the feature ordering does not, so each
    feature's row order is sorted once and each round only builds cumulative sums
    over that fixed order. That turns the split search from O(rounds x features x
    thresholds x rows) into roughly O(rounds x features x rows).
    """
    positives = sum(label for _, label in rows)
    base_rate = max(0.02, min(0.98, positives / len(rows)))
    ensemble = FittedEnsemble(base_rate=base_rate, learning_rate=learning_rate, train_samples=len(rows))

    order = {
        name: sorted(range(len(rows)), key=lambda i: rows[i][0][name])
        for name in FEATURE_NAMES
    }
    thresholds = {name: _candidate_thresholds(sorted({rows[i][0][name] for i in order[name]})) for name in FEATURE_NAMES}
    min_leaf = max(15, len(rows) // 200)
    n_rows = len(rows)

    predictions = [math.log(base_rate / (1.0 - base_rate))] * len(rows)
    gains: dict[str, float] = {name: 0.0 for name in FEATURE_NAMES}

    for _ in range(rounds):
        residual = [
            label - _sigmoid(prediction) for (features, label), prediction in zip(rows, predictions)
        ]
        best: Stump | None = None
        best_gain = 0.0
        for name in FEATURE_NAMES:
            indices = order[name]
            prefix = [0.0]
            for index in indices:
                prefix.append(prefix[-1] + residual[index])
            total = prefix[-1]
            for threshold in thresholds[name]:
                # Rows at or below the threshold form a prefix of the sorted order.
                split = _split_position(rows, indices, name, threshold)
                if split < min_leaf or n_rows - split < min_leaf:
                    continue
                left_sum = prefix[split]
                right_sum = total - left_sum
                gain = left_sum * left_sum / split + right_sum * right_sum / (n_rows - split)
                if gain > best_gain:
                    best_gain = gain
                    best = Stump(
                        feature=name,
                        threshold=threshold,
                        left_delta=round(left_sum / split, 8),
                        right_delta=round(right_sum / (n_rows - split), 8),
                    )
        if best is None:
            break
        gains[best.feature] += best_gain
        ensemble.stumps.append(best)
        for index, (features, _label) in enumerate(rows):
            predictions[index] += learning_rate * best.contribution(features)

    ensemble.feature_gains = {k: round(v, 4) for k, v in gains.items() if v > 0}
    ensemble.train_roc_auc = roc_auc(rows, predictions)
    return ensemble


def _split_position(
    rows: list[tuple[dict[str, float], int]], indices: list[int], feature: str, threshold: float
) -> int:
    """How many of the feature-sorted rows fall at or below the threshold."""
    low, high = 0, len(indices)
    while low < high:
        middle = (low + high) // 2
        if rows[indices[middle]][0][feature] <= threshold:
            low = middle + 1
        else:
            high = middle
    return low


def fit_logistic(
    rows: list[tuple[dict[str, float], int]], *, iterations: int = 200, l2: float = 0.05
) -> FittedLogistic:
    """L2-regularised logistic regression on standardised features.

    The design matrix is standardised once and then reused, so each iteration is
    a single pass of dot products rather than a re-normalisation per row.
    """
    means = {name: sum(row[0][name] for row in rows) / len(rows) for name in FEATURE_NAMES}
    scales = {}
    for name in FEATURE_NAMES:
        variance = sum((row[0][name] - means[name]) ** 2 for row in rows) / len(rows)
        scales[name] = max(math.sqrt(variance), 1e-6)

    design: list[tuple[float, ...]] = [
        tuple((features[name] - means[name]) / scales[name] for name in FEATURE_NAMES)
        for features, _label in rows
    ]
    labels = [label for _features, label in rows]
    n_rows = len(rows)

    weights = [0.0] * len(FEATURE_NAMES)
    bias = 0.0
    learning_rate = 0.6
    for _ in range(iterations):
        grad = [0.0] * len(FEATURE_NAMES)
        grad_bias = 0.0
        for vector, label in zip(design, labels):
            z = bias + math.fsum(w * x for w, x in zip(weights, vector))
            error = _sigmoid(z) - label
            grad_bias += error
            for index, x in enumerate(vector):
                grad[index] += error * x
        for index in range(len(weights)):
            weights[index] -= learning_rate * (grad[index] / n_rows + l2 * weights[index] / n_rows)
        bias -= learning_rate * (grad_bias / n_rows)

    fitted = FittedLogistic(
        weights=dict(zip(FEATURE_NAMES, weights)),
        bias=bias,
        means=means,
        scales=scales,
    )
    scores = [fitted.log_odds(features) for features, _ in rows]
    fitted.train_roc_auc = roc_auc(rows, scores)
    correct = sum(
        1 for (features, label), score in zip(rows, scores) if (_sigmoid(score) >= 0.5) == (label == 1)
    )
    fitted.train_accuracy = round(correct / n_rows, 4)
    return fitted


def roc_auc(rows: list[tuple[dict[str, float], int]], scores: list[float]) -> float:
    """Rank-based AUC, handling ties in the average-rank way."""
    positives = sum(label for _, label in rows)
    negatives = len(rows) - positives
    if positives == 0 or negatives == 0:
        return 0.5
    order = sorted(range(len(rows)), key=lambda i: scores[i])
    ranks = [0.0] * len(rows)
    index = 0
    while index < len(order):
        end = index
        while end + 1 < len(order) and scores[order[end + 1]] == scores[order[index]]:
            end += 1
        average_rank = (index + end) / 2.0 + 1.0
        for position in range(index, end + 1):
            ranks[order[position]] = average_rank
        index = end + 1
    positive_rank_sum = sum(ranks[i] for i, (_, label) in enumerate(rows) if label == 1)
    return round((positive_rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives), 4)


def _candidate_thresholds(values: list[float]) -> list[float]:
    """A bounded grid of candidate split points.

    Every distinct value would be a candidate, but a stump ensemble with thousands
    of candidate splits over a small leaf size overfits the synthetic book. Seventeen
    quantiles per feature is enough resolution for a stump to express a step.
    """
    if len(values) <= 17:
        return values[:-1]
    step = (len(values) - 1) / 16.0
    picked = {values[int(i * step)] for i in range(17)}
    picked.discard(values[-1])
    return sorted(picked)


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    exp_z = math.exp(z)
    return exp_z / (1.0 + exp_z)


@lru_cache(maxsize=4)
def train_default_models() -> tuple[FittedEnsemble, FittedLogistic, int]:
    """Fit the champion and challenger once per process.

    Training is deterministic, so the result is cached rather than refitted for
    every request; the synthetic book is regenerated only on a cache miss.
    """
    rows = synthetic_book()
    return fit_ensemble(rows), fit_logistic(rows), len(rows)


class ScoringEngine:
    """Champion/challenger orchestration and threshold routing."""

    def __init__(self, settings: Plugin3Settings | None = None) -> None:
        self._settings = settings or Plugin3Settings()
        ensemble, logistic, row_count = train_default_models()
        self._champion = GradientBoostedRiskModel(ensemble, self._settings.model_version)
        self._challenger = LogisticRegressionRiskModel(logistic, f"{self._settings.model_version}-lr")
        self._training_rows = row_count

    @property
    def champion(self) -> GradientBoostedRiskModel:
        return self._champion

    @property
    def challenger(self) -> LogisticRegressionRiskModel:
        return self._challenger

    def score(self, submission: Submission, signals: dict[str, float]) -> RiskScore:
        return self._champion.score(submission, signals)

    def score_both(self, submission: Submission, signals: dict[str, float]) -> dict[str, RiskScore]:
        return {
            "champion": self._champion.score(submission, signals),
            "challenger": self._challenger.score(submission, signals),
        }

    def agreement(self, submission: Submission, signals: dict[str, float]) -> float:
        """Share of features on which champion and challenger agree in sign."""
        champion = self._champion.score(submission, signals).contributions
        challenger = self._challenger.score(submission, signals).contributions
        challenger_map = {c.feature: c.shap_value for c in challenger}
        shared = [c for c in champion if c.feature in challenger_map]
        if not shared:
            return 1.0
        agree = sum(
            1
            for c in shared
            if (c.shap_value >= 0) == (challenger_map[c.feature] >= 0)
        )
        return round(agree / len(shared), 4)

    def should_escalate(self, risk: RiskScore, signals: dict[str, float], submission: Submission) -> bool:
        """Whether this case must leave the automated path.

        Escalation is deliberately conservative: model disagreement, a low
        agreement rate, or a score near a threshold all mean a human looks.
        """
        agreement = self.agreement(submission, signals)
        if agreement < 0.6:
            return True
        boundaries = (
            self._settings.decline_threshold,
            self._settings.fast_track_threshold,
            self._settings.auto_decide_threshold,
        )
        return any(abs(risk.score - boundary) < 0.03 for boundary in boundaries)


__all__ = [
    "FEATURE_NAMES",
    "FittedEnsemble",
    "FittedLogistic",
    "GradientBoostedRiskModel",
    "LogisticRegressionRiskModel",
    "RiskScorer",
    "ScoringEngine",
    "Stump",
    "build_features",
    "fit_ensemble",
    "fit_logistic",
    "roc_auc",
    "synthetic_book",
]