"""Workflow orchestration tests.

Workflows are where a mistake costs real money - a retried bind that issues two
policies, or a failed saga that leaves an orphaned quote in the carrier's core.
These tests pin the behaviour that prevents both.
"""

from __future__ import annotations

import pytest

from pas_core.errors import ErrorCode, PasError, ValidationError
from pas_core.tenancy import RequestPrincipal, Tenant, build_context
from pas_core.workflow.engine import (
    LocalWorkflowEngine,
    RetryPolicy,
    StepDefinition,
    WorkflowDefinition,
    WorkflowStatus,
)


@pytest.fixture
def ctx() -> object:
    tenant = Tenant(
        tenant_id="demo-carrier",
        legal_name="Demo Mutual Life",
        pas_vendor="simulated",
        enabled_plugins=frozenset({f"plugin{i}" for i in range(1, 8)}),
    )
    return build_context(
        tenant, RequestPrincipal(subject="tester", tenant_id="demo-carrier", scopes=frozenset({"*"}))
    )


def _definition(*steps: StepDefinition, workflow_id: str = "wf") -> WorkflowDefinition:
    return WorkflowDefinition(workflow_id=workflow_id, name="Test", steps=steps)


async def _ok(value: object = "done") -> object:
    return {"value": value}


async def _fail(message: str = "boom") -> object:
    raise PasError(ErrorCode.PAS_UNAVAILABLE, message)


# ---------------------------------------------------------------------------
# Graph validation
# ---------------------------------------------------------------------------
def test_dependency_cycle_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        _definition(
            StepDefinition(name="a", handler=_ok, depends_on=("b",)),
            StepDefinition(name="b", handler=_ok, depends_on=("a",)),
        )
    assert "cycle" in str(exc.value.detail)


def test_unknown_dependency_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _definition(StepDefinition(name="a", handler=_ok, depends_on=("ghost",)))


def test_duplicate_step_names_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _definition(StepDefinition(name="a", handler=_ok), StepDefinition(name="a", handler=_ok))


def test_step_needs_operation_or_handler() -> None:
    with pytest.raises(ValidationError):
        StepDefinition(name="a")


def test_compensation_must_be_marked() -> None:
    with pytest.raises(ValidationError) as exc:
        _definition(
            StepDefinition(name="a", handler=_ok, compensate_with="undo"),
            StepDefinition(name="undo", handler=_ok),
        )
    assert "compensation_only" in exc.value.message


def test_topological_order_is_deterministic(ctx: object) -> None:
    definition = _definition(
        StepDefinition(name="c", handler=_ok, depends_on=("a", "b")),
        StepDefinition(name="a", handler=_ok),
        StepDefinition(name="b", handler=_ok, depends_on=("a",)),
        StepDefinition(name="undo", handler=_ok, compensation_only=True),
    )
    order = definition.topological_order()
    assert order.index("a") < order.index("b") < order.index("c")
    assert "undo" not in order
    assert set(definition.compensation_steps and {s.name for s in definition.compensation_steps}) == {"undo"}


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_steps_run_in_dependency_order(ctx: object) -> None:
    order: list[str] = []

    def make(name: str, depends: tuple[str, ...] = ()) -> object:
        async def handler(_: object) -> object:
            order.append(name)
            return {"step": name}

        return handler

    engine = LocalWorkflowEngine()
    engine.register(
        _definition(
            StepDefinition(name="a", handler=make("a")),
            StepDefinition(name="b", handler=make("b"), depends_on=("a",)),
            StepDefinition(name="c", handler=make("c"), depends_on=("a", "b")),
            workflow_id="ordered",
        )
    )
    execution = await engine.start(engine.definition("ordered"), ctx, {})
    assert execution.status is WorkflowStatus.SUCCEEDED
    assert order == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_outputs_flow_between_steps(ctx: object) -> None:
    async def first(_: object) -> object:
        return {"quoteId": "QT1"}

    async def second(step_input: object) -> object:
        return {"bound": step_input.values["quoteId"]}

    engine = LocalWorkflowEngine()
    engine.register(
        _definition(
            StepDefinition(name="create", handler=first, output_key="quote"),
            StepDefinition(
                name="bind",
                handler=second,
                depends_on=("create",),
                input_template={"quoteId": "quote.quoteId"},
            ),
            workflow_id="flow",
        )
    )
    execution = await engine.start(engine.definition("flow"), ctx, {})
    assert execution.status is WorkflowStatus.SUCCEEDED
    assert execution.context.outputs["bind"]["bound"] == "QT1"


@pytest.mark.asyncio
async def test_optional_failure_does_not_stop_the_workflow(ctx: object) -> None:
    engine = LocalWorkflowEngine()
    engine.register(
        _definition(
            StepDefinition(name="optional", handler=_fail, optional=True),
            StepDefinition(name="required", handler=_ok, depends_on=("optional",)),
            workflow_id="optional",
        )
    )
    execution = await engine.start(engine.definition("optional"), ctx, {})
    assert execution.status is WorkflowStatus.SUCCEEDED
    assert execution.context.step_runs["optional"].status == "skipped"


@pytest.mark.asyncio
async def test_retry_then_success(ctx: object) -> None:
    attempts = {"n": 0}

    async def flaky(_: object) -> object:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise PasError(ErrorCode.PAS_TIMEOUT, "upstream slow")
        return {"ok": True}

    engine = LocalWorkflowEngine()
    engine.register(
        _definition(
            StepDefinition(
                name="flaky",
                handler=flaky,
                retry=RetryPolicy(max_attempts=3, initial_delay_seconds=0.01),
            ),
            workflow_id="flaky",
        )
    )
    execution = await engine.start(engine.definition("flaky"), ctx, {})
    assert execution.status is WorkflowStatus.SUCCEEDED
    assert attempts["n"] == 3
    assert execution.context.step_runs["flaky"].attempts == 3


@pytest.mark.asyncio
async def test_retry_is_bounded(ctx: object) -> None:
    attempts = {"n": 0}

    async def always_fails(_: object) -> object:
        attempts["n"] += 1
        raise PasError(ErrorCode.PAS_TIMEOUT, "still slow")

    engine = LocalWorkflowEngine()
    engine.register(
        _definition(
            StepDefinition(
                name="fails",
                handler=always_fails,
                retry=RetryPolicy(max_attempts=2, initial_delay_seconds=0.01),
            ),
            workflow_id="bounded",
        )
    )
    execution = await engine.start(engine.definition("bounded"), ctx, {})
    assert execution.status is WorkflowStatus.FAILED
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_non_retryable_error_is_not_retried(ctx: object) -> None:
    attempts = {"n": 0}

    async def invalid(_: object) -> object:
        attempts["n"] += 1
        raise PasError(ErrorCode.VALIDATION_FAILED, "bad input")

    engine = LocalWorkflowEngine()
    engine.register(
        _definition(
            StepDefinition(name="invalid", handler=invalid, retry=RetryPolicy(max_attempts=5)),
            workflow_id="noretry",
        )
    )
    await engine.start(engine.definition("noretry"), ctx, {})
    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_compensation_unwinds_a_partial_saga(ctx: object) -> None:
    compensated: list[str] = []

    async def create_quote(_: object) -> object:
        return {"quoteId": "QT1"}

    async def fail_bind(_: object) -> object:
        raise PasError(ErrorCode.BUSINESS_RULE_VIOLATION, "state conflict", retryable_override=False)

    async def withdraw(_: object) -> object:
        compensated.append("withdrawn")
        return {"ok": True}

    engine = LocalWorkflowEngine()
    engine.register(
        _definition(
            StepDefinition(name="create_quote", handler=create_quote, compensate_with="withdraw_quote"),
            StepDefinition(name="bind", handler=fail_bind, depends_on=("create_quote",)),
            StepDefinition(name="withdraw_quote", handler=withdraw, compensation_only=True),
            workflow_id="saga",
        )
    )
    execution = await engine.start(engine.definition("saga"), ctx, {})
    # A saga that successfully unwound is COMPENSATED, not FAILED: the carrier was
    # left in a consistent state and the trace says so explicitly.
    assert execution.status is WorkflowStatus.COMPENSATED
    assert execution.error["code"] == ErrorCode.BUSINESS_RULE_VIOLATION.value
    assert compensated == ["withdrawn"]
    assert execution.context.step_runs["withdraw_quote"].status == "compensated"


@pytest.mark.asyncio
async def test_timeout_budget_is_enforced(ctx: object) -> None:
    import asyncio

    async def slow(_: object) -> object:
        await asyncio.sleep(0.2)
        return {}

    engine = LocalWorkflowEngine()
    definition = _definition(
        StepDefinition(name="slow", handler=slow, timeout_seconds=0.01), workflow_id="slow"
    )
    engine.register(definition)
    execution = await engine.start(definition, ctx, {})
    assert execution.status is WorkflowStatus.FAILED


@pytest.mark.asyncio
async def test_executions_are_isolated_by_tenant(ctx: object) -> None:
    engine = LocalWorkflowEngine()
    engine.register(_definition(StepDefinition(name="a", handler=_ok), workflow_id="iso"))
    await engine.start(engine.definition("iso"), ctx, {})
    assert len(engine.executions(tenant_id="demo-carrier")) == 1
    assert engine.executions(tenant_id="other") == []


def test_unknown_workflow_lists_available() -> None:
    engine = LocalWorkflowEngine()
    with pytest.raises(PasError) as exc:
        engine.definition("nope")
    assert exc.value.code is ErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# Bundled workflows
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_quote_to_bind_happy_path(ctx: object) -> None:
    from pas_plugins.plugin1_gateway.service import get_workflow_engine  # noqa: PLC0415
    from pas_plugins.plugin1_gateway.workflows import WORKFLOWS_BY_ID  # noqa: PLC0415

    execution = await get_workflow_engine().start(
        WORKFLOWS_BY_ID["quote-to-bind"],
        ctx,
        {
            "productCode": "TERM20-A",
            "faceAmount": 250_000,
            "applicant": {"age": 40, "tobacco": False},
            "stateOfIssue": "NY",
            "effectiveDate": "2026-06-01",
            "beneficiaryTotalPercent": 100,
            "customerAccepted": True,
        },
    )
    assert execution.status is WorkflowStatus.SUCCEEDED, execution.to_dict()
    names = [r.name for r in execution.context.step_runs.values() if r.status == "succeeded"]
    assert names == ["read_product", "check_eligibility", "calculate_premium", "create_quote",
                     "confirm_acceptance", "bind_policy"]
    assert execution.context.outputs["policy"]["policyId"]


@pytest.mark.asyncio
async def test_quote_to_bind_compensates_when_bill_fails(ctx: object) -> None:
    from pas_plugins.plugin1_gateway.service import get_workflow_engine  # noqa: PLC0415
    from pas_plugins.plugin1_gateway.workflows import WORKFLOWS_BY_ID  # noqa: PLC0415

    execution = await get_workflow_engine().start(
        WORKFLOWS_BY_ID["quote-to-bind"],
        ctx,
        {
            "productCode": "TERM20-A",
            "faceAmount": 250_000,
            "applicant": {"age": 40},
            "stateOfIssue": "NY",
            "effectiveDate": "2026-06-01",
            "beneficiaryTotalPercent": 100,
            "customerAccepted": False,  # decline stops the saga
        },
    )
    assert execution.status is WorkflowStatus.COMPENSATED
    assert execution.context.step_runs["withdraw_quote"].status == "compensated"


@pytest.mark.asyncio
async def test_eligibility_gate_blocks_an_out_of_range_request(ctx: object) -> None:
    from pas_plugins.plugin1_gateway.service import get_workflow_engine  # noqa: PLC0415
    from pas_plugins.plugin1_gateway.workflows import WORKFLOWS_BY_ID  # noqa: PLC0415

    execution = await get_workflow_engine().start(
        WORKFLOWS_BY_ID["quote-to-bind"],
        ctx,
        {
            "productCode": "TERM20-A",
            "faceAmount": 10_000,  # below the product minimum
            "applicant": {"age": 40},
            "stateOfIssue": "NY",
            "effectiveDate": "2026-06-01",
        },
    )
    assert execution.status is WorkflowStatus.FAILED
    assert execution.error["code"] == ErrorCode.BUSINESS_RULE_VIOLATION.value
    assert "BELOW_MIN_FACE" in execution.error["errors"]["reasonCodes"]


@pytest.mark.asyncio
async def test_beneficiary_change_rejects_over_allocation(ctx: object) -> None:
    from pas_plugins.plugin1_gateway.service import get_workflow_engine  # noqa: PLC0415
    from pas_plugins.plugin1_gateway.workflows import WORKFLOWS_BY_ID  # noqa: PLC0415

    execution = await get_workflow_engine().start(
        WORKFLOWS_BY_ID["beneficiary-change"],
        ctx,
        {
            "policyId": "SIMPOL000001",
            "allocations": [
                {"partyId": "P1", "sharePercent": 80, "relationship": "spouse"},
                {"partyId": "P2", "sharePercent": 40, "relationship": "child"},
            ],
        },
    )
    assert execution.status is WorkflowStatus.FAILED
    assert execution.error["code"] == ErrorCode.BUSINESS_RULE_VIOLATION.value


@pytest.mark.asyncio
async def test_premium_recalculation_reports_a_delta(ctx: object) -> None:
    from pas_plugins.plugin1_gateway.service import get_workflow_engine  # noqa: PLC0415
    from pas_plugins.plugin1_gateway.workflows import WORKFLOWS_BY_ID  # noqa: PLC0415

    execution = await get_workflow_engine().start(
        WORKFLOWS_BY_ID["premium-recalculation"],
        ctx,
        {
            "change": {"faceAmount": 500_000},
            "currentAnnualisedPremium": 500.0,
            "productCode": "TERM20-A",
            "faceAmount": 500_000,
            "issueAge": 40,
            "paymentMode": "monthly",
            "stateOfIssue": "NY",
        },
    )
    assert execution.status is WorkflowStatus.SUCCEEDED
    delta = execution.context.outputs["delta"]
    assert delta["delta"] > 0
    assert delta["requiresCustomerConsent"] is True


def test_workflow_catalogue_is_complete() -> None:
    from pas_plugins.plugin1_gateway.workflows import WORKFLOWS, summarise  # noqa: PLC0415

    summary = summarise()
    assert summary["count"] == len(WORKFLOWS)
    quote = next(w for w in summary["workflows"] if w["workflowId"] == "quote-to-bind")
    assert quote["hasCompensation"] is True
    for workflow in WORKFLOWS:
        detail = workflow.to_dict()
        assert detail["executionOrder"], workflow.workflow_id
        assert detail["description"], workflow.workflow_id
