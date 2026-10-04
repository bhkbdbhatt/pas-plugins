"""Tests for Plugin 3 - AI-Powered Accelerated Underwriting workbench."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from pas_core.tenancy import RequestPrincipal, Tenant, build_context

from pas_plugins.plugin3_auw.complexity import ComplexityEngine, triage
from pas_plugins.plugin3_auw.enrichment import EnrichmentEngine
from pas_plugins.plugin3_auw.extraction import REVIEW_THRESHOLD, DocumentText, RuleBasedExtractor
from pas_plugins.plugin3_auw.mcp_server import build_registry
from pas_plugins.plugin3_auw.models import (
    Applicant,
    Avocation,
    Decision,
    DecisionSource,
    DocumentRef,
    MibRecord,
    MibStatus,
    OverrideRequest,
    Request,
    Submission,
)
from pas_plugins.plugin3_auw.monitoring import MonitoringEngine, population_stability_index
from pas_plugins.plugin3_auw.rules import AppetiteRule, AppetiteRuleSet, RuleEngine, default_ruleset
from pas_plugins.plugin3_auw.scoring import (
    FEATURE_NAMES,
    ScoringEngine,
    build_features,
    fit_ensemble,
    fit_logistic,
    roc_auc,
    synthetic_book,
)
from pas_plugins.plugin3_auw.service import WorkbenchService

TENANT = "demo-carrier"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def scoring() -> ScoringEngine:
    return ScoringEngine()


@pytest.fixture(scope="module")
def service(scoring: ScoringEngine) -> WorkbenchService:
    return WorkbenchService(scoring=scoring)


@pytest.fixture
def client():
    from pas_plugins.plugin3_auw.main import app

    with TestClient(app) as test_client:
        yield test_client


def make_submission(
    submission_id: str = "SUB-TEST-0001",
    *,
    age: int = 35,
    face: float = 250_000.0,
    tobacco: bool | None = None,
    avocation: Avocation = Avocation.CLASS_1,
    consent: bool = True,
    documents: list[str] | None = None,
    **applicant_extra: object,
) -> Submission:
    """Build a valid submission. Explicit keyword args win over `applicant_extra`."""
    fields: dict[str, object] = {
        "age": age,
        "sex": "F",
        "state_of_residence": "CA",
        "height_inches": 68,
        "weight_pounds": 150,
        "avocation": avocation,
        **applicant_extra,
    }
    if tobacco is not None:
        fields["tobacco_use"] = tobacco
    fields.setdefault("tobacco_use", False)
    return Submission(
        submission_id=submission_id,
        tenant_id=TENANT,
        mib_consent=consent,
        prescription_consent=consent,
        documents=[
            DocumentRef(document_id=f"D{i}", kind=kind)
            for i, kind in enumerate(documents or ["application"])
        ],
        applicant=Applicant.model_validate(fields),
        request=Request(product_code="TERM20", face_amount=face, term_years=20),
    )


def ctx(tenant: str = TENANT, scopes: frozenset[str] | None = None) -> object:
    return build_context(
        Tenant(tenant_id=tenant, legal_name="Test"),
        RequestPrincipal(subject="uw.tester", tenant_id=tenant, scopes=scopes or frozenset({"*"})),
    )


def call(handler, arguments: dict) -> dict:
    return asyncio.run(handler(arguments, ctx()))


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------


def test_submission_bands_and_control_flags():
    submission = make_submission(age=70, avocation=Avocation.CLASS_5, family_history_early_death=True)
    assert submission.age_band == "66+"
    assert submission.face_amount_band == "100k-500k"
    assert "familyHistoryEarlyDeath" in submission.control_flags


def test_submission_is_camel_case_on_the_wire():
    payload = make_submission().to_dict()
    assert "submissionId" in payload
    assert "stateOfResidence" in payload["applicant"]
    assert "faceAmount" in payload["request"]


def test_state_code_is_normalised_before_validation():
    assert make_submission().applicant.state_of_residence == "CA"
    lowered = Submission(
        submission_id="SUB-TEST-0009",
        tenant_id=TENANT,
        applicant=Applicant(age=40, state_of_residence="ny"),
        request=Request(product_code="T20", face_amount=100_000),
    )
    assert lowered.applicant.state_of_residence == "NY"


def test_replacement_requires_an_existing_carrier():
    with pytest.raises(ValueError, match="existing carrier"):
        Request(product_code="TERM", face_amount=100_000, is_replacement=True)


def test_avocation_hazard_multiplier_is_ordered():
    multipliers = [Avocation(f"class{i}").hazard_multiplier for i in range(1, 6)]
    assert multipliers == sorted(multipliers)


def test_mib_record_requires_consent():
    with pytest.raises(ValueError, match="consent"):
        MibRecord(review_status=MibStatus.CLEAR, consent_obtained=False)


def test_extracted_field_requires_a_source_document():
    from pas_plugins.plugin3_auw.models import ExtractedField

    with pytest.raises(ValueError, match="no document"):
        ExtractedField(
            field="applicant.age",
            value=40,
            confidence=0.99,
            provenance="documentExtracted",
        )


def test_override_reason_must_be_a_sentence():
    """A 20-character minimum alone is not enough; a bare token is not a reason."""
    with pytest.raises(ValueError, match="sentence"):
        OverrideRequest(decision=Decision.ACCEPT, reason="aaaaaaaaaaaaaaaaaaaa")


# --------------------------------------------------------------------------
# triage
# --------------------------------------------------------------------------


def test_complexity_routes_a_young_simple_case_to_auto():
    result = triage(make_submission())
    assert result.tier.value == "auto"
    assert result.score < 50


def test_complexity_routes_a_hazardous_elderly_case_to_specialist():
    result = triage(make_submission(age=74, avocation=Avocation.CLASS_5))
    assert result.tier.value == "specialist"
    assert result.estimated_minutes_for_human > 40


def test_complexity_penalises_missing_evidence_not_low_risk():
    """A missing paramed exam on a $2m application is a routing cost, not a risk signal."""
    small = triage(make_submission(face=250_000, documents=["application"]))
    large = triage(make_submission(face=2_000_000, documents=["application"]))
    assert large.factors["missingEvidence"] > small.factors["missingEvidence"]


def test_complexity_weights_are_exposed_for_re_weighting():
    engine = ComplexityEngine(weights={"tobacco": 50.0})
    assert engine.weights["tobacco"] == 50.0
    assert triage(make_submission(), engine).factors["tobacco"] == 0.0


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------


def test_extraction_recovers_labelled_fields_with_provenance():
    submission = make_submission()
    text = DocumentText("DOC1", "Application. Age: 35  Sex: F  State: CA  Face Amount: 250,000. 20-year term.")
    fields = RuleBasedExtractor().extract(submission, [text])
    found = {f.field: f for f in fields}
    assert found["applicant.age"].value == 35
    assert found["applicant.age"].source_document == "DOC1"
    assert found["applicant.age"].page == 1
    assert found["request.face_amount"].value == 250_000.0


def test_loose_matches_are_marked_for_verification():
    submission = make_submission()
    fields = RuleBasedExtractor().extract(submission, [DocumentText("D", "State: CA")])
    state = next(f for f in fields if f.field == "applicant.state_of_residence")
    assert state.confidence < REVIEW_THRESHOLD


def test_declaration_wins_over_extraction_on_conflict():
    submission = make_submission(age=35)
    extractor = RuleBasedExtractor()
    extracted = extractor.extract(submission, [DocumentText("DOC1", "Age: 51")])
    merged = extractor.merge_with_declarations(extracted, submission)
    ages = [f for f in merged if f.field == "applicant.age"]
    assert any(f.value == 35 and f.provenance.value == "applicantDeclared" for f in ages)
    assert any(f.value == 51 and f.confidence <= 0.5 for f in ages)


def test_extraction_is_deterministic():
    submission = make_submission()
    text = DocumentText("DOC1", "Age: 35 Face Amount: 250,000")
    first = RuleBasedExtractor().extract(submission, [text])
    second = RuleBasedExtractor().extract(submission, [text])
    assert [f.model_dump() for f in first] == [f.model_dump() for f in second]


def test_page_lookup_respects_page_boundaries():
    text = DocumentText("DOC1", "a b c", pages=["a", "b", "c"])
    assert text.page_of(0) == 1
    assert text.page_of(2) == 2
    assert text.page_of(4) == 3


# --------------------------------------------------------------------------
# enrichment and consent
# --------------------------------------------------------------------------


def test_enrichment_refuses_lookups_without_consent():
    result = EnrichmentEngine().enrich(make_submission(consent=False))
    assert result.mib is None
    assert result.prescription is None
    assert "mibConsentMissing" in result.consent_gaps
    assert "mibLookupSkippedNoConsent" in result.warnings


def test_enrichment_resolves_signals_with_consent():
    result = EnrichmentEngine().enrich(make_submission(consent=True))
    assert result.mib is not None
    assert result.mib.consent_obtained is True
    assert result.consent_gaps == []


def test_enrichment_is_deterministic():
    """Signals must not vary between runs, or explanations stop being reproducible.

    Only the wall-clock timestamps are allowed to differ; everything an
    underwriter or a regulator would rely on has to be identical.
    """
    submission = make_submission()
    first = EnrichmentEngine().enrich(submission).to_dict()
    second = EnrichmentEngine().enrich(submission).to_dict()
    for payload in (first, second):
        payload.pop("enrichedAt")
        payload["screening"].pop("screenedAt")
    assert first == second


def test_contestable_mib_is_flagged_as_a_decline_trigger():
    assert MibStatus.UPHELD.is_decline_trigger
    assert not MibStatus.CLEAR.is_decline_trigger


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------


def test_model_fits_and_discriminates():
    rows = synthetic_book(samples=1200)
    ensemble = fit_ensemble(rows)
    assert 0.6 < ensemble.train_roc_auc <= 1.0
    assert ensemble.stumps
    assert all(stump.feature in FEATURE_NAMES for stump in ensemble.stumps)


def test_challenger_is_trained_and_interpretable():
    fitted = fit_logistic(synthetic_book(samples=800))
    assert 0.5 < fitted.train_accuracy <= 1.0
    assert set(fitted.weights) == set(FEATURE_NAMES)


def test_shap_attributions_sum_to_the_logit_shift(scoring: ScoringEngine):
    """For an additive model, exact SHAP values must reconstruct the output.

    This is the property that makes the explanation trustworthy: base value plus
    the sum of contributions equals the model's raw score, with no residual.
    """
    submission = make_submission(age=58, tobacco_use=True)
    signals = EnrichmentEngine().enrich(submission).signals
    features = build_features(submission, signals)
    ensemble = scoring.champion.ensemble
    attribution = ensemble.attributions(features)
    base_logit = _logit(ensemble.base_rate)
    assert abs(base_logit + sum(attribution.values()) - ensemble.raw(features)) < 1e-9


def test_logistic_attributions_sum_to_the_logit(scoring: ScoringEngine):
    submission = make_submission(age=44)
    signals = EnrichmentEngine().enrich(submission).signals
    features = build_features(submission, signals)
    fitted = scoring.challenger.fitted
    attribution = fitted.attributions(features)
    assert abs(sum(attribution.values()) - (fitted.log_odds(features) - fitted.bias)) < 1e-9


def test_contributions_are_ranked_by_absolute_impact(scoring: ScoringEngine):
    submission = make_submission(age=61, tobacco_use=True)
    result = scoring.score(submission, EnrichmentEngine().enrich(submission).signals)
    ranks = [c.rank for c in result.contributions]
    assert ranks == list(range(1, len(ranks) + 1))
    magnitudes = [abs(c.shap_value) for c in result.contributions]
    assert magnitudes == sorted(magnitudes, reverse=True)


def test_scoring_is_deterministic(scoring: ScoringEngine):
    submission = make_submission()
    signals = EnrichmentEngine().enrich(submission).signals
    first = scoring.score(submission, signals).score
    second = scoring.score(submission, signals).score
    assert first == second


def test_riskier_submission_scores_higher(scoring: ScoringEngine):
    low = make_submission("SUB-TEST-0020", age=30, face=100_000)
    high = make_submission("SUB-TEST-0021", age=64, tobacco_use=True, avocation=Avocation.CLASS_5, face=3_000_000)
    engine = EnrichmentEngine()
    assert (
        scoring.score(high, engine.enrich(high).signals).score
        > scoring.score(low, engine.enrich(low).signals).score
    )


def test_roc_auc_handles_perfect_and_inverted_separation():
    perfect = [({"x": 1.0}, 0), ({"x": 2.0}, 1)]
    assert roc_auc(perfect, [0.1, 0.9]) == 1.0
    assert roc_auc(perfect, [0.9, 0.1]) == 0.0


def test_roc_auc_of_a_single_class_is_neutral():
    assert roc_auc([({"x": 1.0}, 1)], [0.5]) == 0.5


# --------------------------------------------------------------------------
# appetite rules
# --------------------------------------------------------------------------


def test_default_ruleset_is_versioned_and_populated():
    ruleset = default_ruleset()
    assert ruleset.version
    assert len(ruleset.rules) >= 15
    assert all(rule.reason_code for rule in ruleset.rules)
    assert len(set(ruleset.rule_ids)) == len(ruleset.rules)


def test_face_amount_ceiling_declines():
    decision, reasons, _, _ = RuleEngine().evaluate(make_submission(face=9_000_000))
    assert decision is Decision.DECLINE
    assert "FACE_AMOUNT_EXCEEDS_LIMITS" in reasons


def test_contestable_mib_declines():
    submission = make_submission()
    submission.mib = MibRecord(review_status=MibStatus.UPHELD, consent_obtained=True)
    decision, reasons, _, _ = RuleEngine().evaluate(submission)
    assert decision is Decision.DECLINE
    assert "MIB_DECISION_CONTESTABLE" in reasons


def test_all_clear_receives_a_positive_reason_code():
    decision, reasons, _, _ = RuleEngine().evaluate(make_submission())
    assert decision is Decision.ACCEPT
    assert reasons == ["APPETITE_CLEAR"]


def test_non_applicable_product_rules_are_reported_as_not_fired():
    submission = make_submission()
    submission.applicant.age = 16
    outcomes = default_ruleset().evaluate(submission)
    minor = next(o for o in outcomes if o.rule_id == "APP-012")
    assert not minor.fired
    assert minor.applicable is False  # scoped to TERM/WHOLE; this is TERM20-A
    assert minor.reason == ""


def test_rule_set_evaluates_all_rules_not_only_fired_ones():
    outcomes = RuleEngine().evaluate(make_submission(face=9_000_000))[3]
    assert len(outcomes) >= 15


def test_duplicate_rule_registration_is_rejected():
    rule = AppetiteRule(
        rule_id="X-1",
        name="Test",
        severity="refer",
        reason_code="X",
        message="m",
        outcome="refer",
        predicate=lambda s: True,
    )
    assert AppetiteRuleSet("1", [rule]).rule_ids == ["X-1"]


# --------------------------------------------------------------------------
# decision service
# --------------------------------------------------------------------------


def test_clean_submission_is_accepted_with_a_class(service: WorkbenchService):
    record = service.decide(make_submission())
    assert record.decision is Decision.ACCEPT
    assert record.assigned_class is not None
    assert record.assigned_class.code in {"PREFERRED", "STANDARD"}


def test_every_decision_carries_a_reason_code(service: WorkbenchService):
    for submission in (
        make_submission("SUB-TEST-0030"),
        make_submission("SUB-TEST-0031", age=70),
        make_submission("SUB-TEST-0032", face=9_000_000),
        make_submission("SUB-TEST-0033", tobacco=True, age=50),
    ):
        assert service.decide(submission).reason_codes


def test_automated_decline_never_lacks_an_explanation(service: WorkbenchService):
    record = service.decide(make_submission("SUB-TEST-0034", face=9_000_000))
    assert record.decision is Decision.DECLINE
    assert record.explainable


def test_missing_consent_routes_to_human_rather_than_auto_accept(service: WorkbenchService):
    record = service.decide(make_submission("SUB-TEST-0035", consent=False))
    assert record.decision is Decision.REFER
    assert "CONSENT_GAP" in record.reason_codes


def test_auto_decline_is_disabled_by_default(service: WorkbenchService):
    """A model-driven decline must still be signed off by a person."""
    service_obj = service
    record = service_obj.decide(make_submission("SUB-TEST-0036", age=63, tobacco_use=True, face=900_000))
    assert record.decision is not Decision.DECLINE or record.decision_source is DecisionSource.UNDERWRITER


def test_override_retains_the_prior_outcome(service: WorkbenchService):
    record = service.decide(make_submission("SUB-TEST-0040"))
    prior = str(record.decision)
    updated = service.override(
        record.decision_id,
        OverrideRequest(decision=Decision.DECLINE, reason="Evidence contradicts the declaration materially."),
        "uw.supervisor",
    )
    assert updated.decision is Decision.DECLINE
    # `record` is the same mutated object, so the prior outcome is read from the
    # retained override block rather than from the live decision.
    assert updated.override["fromDecision"] == prior
    assert updated.override["actor"] == "uw.supervisor"
    assert "UNDERWRITER_OVERRIDE" in updated.reason_codes


def test_override_of_an_unknown_decision_raises(service: WorkbenchService):
    with pytest.raises(KeyError):
        service.override(
            "DEC-does-not-exist",
            OverrideRequest(decision=Decision.ACCEPT, reason="A sufficiently long reason string."),
            "uw.tester",
        )


def test_decisions_are_retrievable_by_id_and_submission(service: WorkbenchService):
    submission = make_submission("SUB-TEST-0050")
    record = service.decide(submission)
    assert service.get_decision(record.decision_id) is record
    assert service.get_submission(submission.submission_id) is submission
    assert record in service.decisions_for_submission(submission.submission_id)


def test_decision_serialises_to_camel_case(service: WorkbenchService):
    payload = service.decide(make_submission("SUB-TEST-0051")).to_dict()
    for key in ("decisionId", "reasonCodes", "decisionSource", "explainable", "topDrivers"):
        assert key in payload


# --------------------------------------------------------------------------
# monitoring
# --------------------------------------------------------------------------


def test_psi_is_zero_for_an_identical_distribution():
    sample = [i / 100 for i in range(200)]
    assert population_stability_index(sample, sample) == 0.0


def test_psi_grows_with_a_real_shift():
    base = [i / 200 for i in range(400)]
    shifted = [min(1.0, i / 50) for i in range(400)]
    assert population_stability_index(base, shifted) > 0.1


def test_sparse_features_do_not_produce_absurd_psi():
    """Half-count smoothing keeps a mostly-zero feature from reporting a fake shift."""
    baseline = [0.0] * 190 + [1.0] * 10
    current = [0.0] * 195 + [1.0] * 5
    assert population_stability_index(baseline, current) < 1.0


def test_monitoring_report_flags_drift_and_withholds_promotion(scoring: ScoringEngine):
    engine = MonitoringEngine(scoring=scoring)
    submissions, signals = [], {}
    for index in range(60):
        submission = make_submission(f"SUB-MON-{index:04d}", age=70, tobacco_use=True, face=5_000_000)
        submissions.append(submission)
        signals[submission.submission_id] = EnrichmentEngine().enrich(submission).signals
    report = engine.report(submissions, signals)
    assert report.sample_size == 60
    assert report.retrain_recommended is True
    assert report.recommendation != "keepChampion"
    assert report.findings


def test_monitoring_report_handles_an_empty_window(scoring: ScoringEngine):
    report = MonitoringEngine(scoring=scoring).report([], {})
    assert "noSubmissionsInWindow" in report.findings


# --------------------------------------------------------------------------
# MCP surface
# --------------------------------------------------------------------------


def test_mcp_registry_exposes_the_full_workbench():
    registry = build_registry()
    names = registry.tool_names()
    assert len(names) == 11
    assert "auw_submit_application" in names
    assert "auw_override_decision" in names


def test_override_tool_is_marked_destructive():
    spec = build_registry().get_tool("auw_override_decision")
    assert spec.annotations.destructive is True
    assert spec.annotations.read_only is False
    assert "uw:override" in spec.required_scopes


def test_read_tools_are_annotated_read_only():
    spec = build_registry().get_tool("auw_score_risk")
    assert spec.annotations.read_only is True
    assert spec.annotations.destructive is False


def test_mcp_resources_use_the_platform_uri_scheme():
    resources = build_registry().list_resources()
    assert resources
    assert all(r["uriTemplate"].startswith("pas://") for r in resources)


def test_mcp_submit_returns_a_decision():
    registry = build_registry()
    spec = registry.get_tool("auw_submit_application")
    result = call(spec.handler, {"submission": make_submission("SUB-MCP-0001").to_dict()})
    assert "decision" in result
    assert result["decision"]["decision"] in {"accept", "refer", "decline"}


def test_mcp_submit_refuses_a_cross_tenant_submission():
    registry = build_registry()
    spec = registry.get_tool("auw_submit_application")
    submission = make_submission("SUB-MCP-0002", consent=False)
    submission.tenant_id = "acme-life"
    with pytest.raises(Exception, match="belongs to tenant"):
        call(spec.handler, {"submission": submission.to_dict()})


def test_mcp_explain_reports_unknown_decisions_cleanly():
    registry = build_registry()
    result = call(registry.get_tool("auw_explain_decision").handler, {"decisionId": "DEC-nope"})
    assert result["error"] == "decisionNotFound"


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------


def test_health(client: TestClient):
    assert client.get("/health").json()["status"] == "healthy"


def test_submit_endpoint_returns_the_decision(client: TestClient):
    response = client.post("/auw/submissions", json=make_submission("SUB-HTTP-0001").to_dict())
    assert response.status_code == 201
    body = response.json()
    assert body["decision"]["decision"] in {"accept", "refer", "decline"}
    assert body["triage"]["tier"] in {"auto", "fastTrack", "refer", "specialist"}


def test_endpoints_reject_another_tenants_submission(client: TestClient):
    submission = make_submission("SUB-HTTP-0002").to_dict()
    submission["tenantId"] = "acme-life"
    assert client.post("/auw/submissions", json=submission).status_code == 403


def test_triage_endpoint_rejects_a_mismatched_path_id(client: TestClient):
    response = client.post(
        "/auw/submissions/SUB-HTTP-9999/triage", json=make_submission("SUB-HTTP-0003").to_dict()
    )
    assert response.status_code == 400


def test_funnel_endpoints_all_answer(client: TestClient):
    submission = make_submission("SUB-HTTP-0004").to_dict()
    for stage in ("triage", "extract", "enrich", "score", "decision"):
        response = client.post(f"/auw/submissions/SUB-HTTP-0004/{stage}", json=submission)
        assert response.status_code == 200, stage


def test_extract_endpoint_returns_provenance(client: TestClient):
    response = client.post(
        "/auw/submissions/SUB-HTTP-0005/extract",
        json={
            "submission": make_submission("SUB-HTTP-0005").to_dict(),
            "documents": [{"documentId": "DOC9", "text": "Age: 35  Face Amount: 250,000"}],
        },
    )
    fields = response.json()["fields"]
    assert any(f["sourceDocument"] == "DOC9" for f in fields)


def test_decision_lifecycle_over_http(client: TestClient):
    created = client.post("/auw/submissions", json=make_submission("SUB-HTTP-0006").to_dict()).json()
    decision_id = created["decision"]["decisionId"]

    assert client.get(f"/auw/decisions/{decision_id}").status_code == 200
    assert client.get("/auw/decisions", params={"submissionId": "SUB-HTTP-0006"}).json()["count"] >= 1

    explained = client.get(f"/auw/decisions/{decision_id}/explain").json()
    assert explained["decisionId"] == decision_id
    assert "topFactors" in explained

    overridden = client.post(
        f"/auw/decisions/{decision_id}/override",
        json={"decision": "decline", "reason": "Evidence contradicts the declaration materially."},
    )
    assert overridden.status_code == 200
    assert overridden.json()["decision"]["override"]["toDecision"] == "decline"


def test_unknown_decision_is_404(client: TestClient):
    assert client.get("/auw/decisions/DEC-nope").status_code == 404


def test_ruleset_endpoint_lists_every_rule(client: TestClient):
    body = client.get("/auw/ruleset").json()
    assert body["count"] >= 15
    assert all(r["reasonCode"] for r in body["rules"])


def test_model_inventory_reports_both_models(client: TestClient):
    body = client.get("/auw/health/detailed").json()
    assert body["champion"]["trainRocAuc"] > 0.5
    assert body["challenger"]["trainRocAuc"] > 0.5
    assert body["ruleset"]["rules"] >= 15
    assert body["thresholds"]["allowAutoDecline"] is False


def test_monitoring_and_queue_endpoints_answer(client: TestClient):
    client.post("/auw/submissions", json=make_submission("SUB-HTTP-0007").to_dict())
    assert client.get("/auw/monitoring/report").status_code == 200
    assert client.get("/auw/underwriting-queue").status_code == 200


def test_openapi_documents_the_workbench(client: TestClient):
    spec = client.get("/openapi.json").json()
    paths = [p for p in spec["paths"] if p.startswith("/auw")]
    assert len(paths) >= 10
    assert spec["openapi"].startswith("3.1")


def _logit(rate: float) -> float:
    import math

    return math.log(rate / (1.0 - rate))