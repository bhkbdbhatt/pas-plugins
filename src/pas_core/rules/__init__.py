"""Versioned business rule engine and DSL.

See :mod:`pas_core.rules.engine` for the evaluator.  This package also ships the
bundled rule sets used by the suite so a fresh deployment has working appetite,
eligibility and workflow-precondition rules out of the box.
"""

from __future__ import annotations

from pas_core.rules.engine import (
    Action,
    Condition,
    ConditionGroup,
    EvaluationResult,
    Outcome,
    Rule,
    RuleEngine,
    RuleOutcome,
    RuleSet,
    RuleVersionStore,
    Severity,
    resolve_path,
)
from pas_core.rules.library import BUILTIN_RULE_SETS, default_engine, load_builtin_rule_sets

__all__ = [
    "Action",
    "BUILTIN_RULE_SETS",
    "Condition",
    "ConditionGroup",
    "EvaluationResult",
    "Outcome",
    "Rule",
    "RuleEngine",
    "RuleOutcome",
    "RuleSet",
    "RuleVersionStore",
    "Severity",
    "default_engine",
    "load_builtin_rule_sets",
    "resolve_path",
]
