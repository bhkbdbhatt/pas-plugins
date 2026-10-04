"""Workflow orchestration for composing atomic operations into business processes.

Why a workflow engine at all when the atomic operations are just HTTP calls?
Because a real carrier process is not a straight line:

``createQuote -> validateEligibility -> calculatePremium -> bindPolicy``

has retries, compensation, human approval gates, long-running waits for
underwriting, and it must survive a pod restart mid-issue.  Those are exactly the
properties a durable workflow engine provides and a chain of HTTP calls does not.

Two engines are provided behind one interface:

* :class:`LocalWorkflowEngine` - deterministic, in-process, used by tests, the
  demo stack and single-tenant deployments.
* :class:`TemporalWorkflowEngine` - adapter for Temporal (or AWS Step Functions
  via the same interface) in production.

Workflows are declared as data (:class:`WorkflowDefinition`) so the orchestration
catalogue is inspectable, diffable and publishable as an MCP resource.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pas_core.errors import ErrorCode, PasError, ValidationError
from pas_core.observability import ACTIVE_WORKFLOWS, WORKFLOW_STEPS, GLOBAL_METRICS
from pas_core.tenancy import TenantContext, current_tenant_id

StepHandler = Callable[["WorkflowContext"], Awaitable[Any]]


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    COMPENSATED = "compensated"
    WAITING = "waiting"


class WorkflowStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    COMPENSATED = "compensated"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential backoff.

    Retries are opt-in per step and always bounded.  An unbounded retry against a
    legacy PAS is how a rate calculation turns into an outage.
    """

    max_attempts: int = 3
    initial_delay_seconds: float = 0.2
    backoff_multiplier: float = 2.0
    max_delay_seconds: float = 30.0
    retry_on: tuple[str, ...] = ()

    def delay_for(self, attempt: int) -> float:
        delay = self.initial_delay_seconds * (self.backoff_multiplier ** max(0, attempt - 1))
        return min(delay, self.max_delay_seconds)

    def should_retry(self, attempt: int, error: BaseException) -> bool:
        if attempt >= self.max_attempts:
            return False
        if not self.retry_on:
            return isinstance(error, PasError) and error.retryable
        return any(marker in str(error) for marker in self.retry_on)


@dataclass(frozen=True, slots=True)
class StepDefinition:
    """One unit of work in a workflow.

    ``operation_id`` references an atomic operation in the gateway catalogue;
    ``handler`` provides the logic for steps that are pure computation.  Exactly
    one of the two must be present.

    ``compensate_with`` names a step to run if a *later* step fails, turning the
    process into a saga: a bind that succeeds and then fails on a downstream
    document step will withdraw its quote rather than leave it dangling.
    """

    name: str
    operation_id: str | None = None
    handler: StepHandler | None = field(default=None, repr=False)
    depends_on: tuple[str, ...] = ()
    retry: RetryPolicy | None = None
    compensate_with: str | None = None
    optional: bool = False
    compensation_only: bool = False
    timeout_seconds: float = 60.0
    approval_required: bool = False
    input_template: dict[str, Any] = field(default_factory=dict)
    output_key: str | None = None

    def __post_init__(self) -> None:
        if self.operation_id is None and self.handler is None:
            msg = f"step '{self.name}' needs either an operation_id or a handler"
            raise ValidationError(msg, step=self.name)
        if self.compensation_only and self.compensate_with:
            msg = (
                f"step '{self.name}' cannot itself declare a compensating step; "
                "compensation chains would be impossible to reason about"
            )
            raise ValidationError(msg, step=self.name)


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """A business process composed of atomic operations."""

    workflow_id: str
    name: str
    steps: tuple[StepDefinition, ...]
    version: int = 1
    description: str = ""
    tags: tuple[str, ...] = ()
    timeout_seconds: float = 900.0
    slo_ms: int = 3000

    def __post_init__(self) -> None:
        if not self.steps:
            msg = f"workflow '{self.workflow_id}' has no steps"
            raise ValidationError(msg, workflowId=self.workflow_id)
        names = [s.name for s in self.steps]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            msg = f"workflow '{self.workflow_id}' has duplicate step names: {sorted(duplicates)}"
            raise ValidationError(msg, duplicates=sorted(duplicates))
        for step in self.steps:
            for dependency in step.depends_on:
                if dependency not in names:
                    msg = (
                        f"step '{step.name}' depends on unknown step '{dependency}' "
                        f"in workflow '{self.workflow_id}'"
                    )
                    raise ValidationError(msg, step=step.name, dependency=dependency)
            if step.compensate_with and step.compensate_with not in names:
                msg = (
                    f"step '{step.name}' names an unknown compensating step "
                    f"'{step.compensate_with}'"
                )
                raise ValidationError(msg, step=step.name, compensation=step.compensate_with)
            if step.compensate_with and not self.step(step.compensate_with).compensation_only:
                msg = (
                    f"step '{step.name}' names '{step.compensate_with}' as its compensation, but "
                    "that step is not marked compensation_only"
                )
                raise ValidationError(msg, step=step.name, compensation=step.compensate_with)
        self._assert_acyclic()

    @property
    def forward_steps(self) -> tuple[StepDefinition, ...]:
        """Steps that run during normal (forward) execution."""
        return tuple(s for s in self.steps if not s.compensation_only)

    @property
    def compensation_steps(self) -> tuple[StepDefinition, ...]:
        """Steps that only run while unwinding a partially completed saga."""
        return tuple(s for s in self.steps if s.compensation_only)

    def _assert_acyclic(self) -> None:
        graph = {s.name: set(s.depends_on) for s in self.forward_steps}
        state: dict[str, int] = {}

        def visit(node: str, trail: tuple[str, ...]) -> None:
            if state.get(node) == 2:
                return
            if state.get(node) == 1:
                cycle = " -> ".join([*trail, node])
                msg = f"workflow '{self.workflow_id}' has a dependency cycle: {cycle}"
                raise ValidationError(msg, cycle=cycle)
            state[node] = 1
            for dependency in graph[node]:
                visit(dependency, (*trail, node))
            state[node] = 2

        for step in self.forward_steps:
            visit(step.name, ())

    def topological_order(self) -> list[str]:
        """Deterministic forward execution order (Kahn's algorithm, name-sorted ties).

        Compensation-only steps are excluded: they run during unwinding, not on
        the happy path.
        """
        forward = self.forward_steps
        indegree = {s.name: len(set(s.depends_on)) for s in forward}
        dependents: dict[str, list[str]] = {s.name: [] for s in forward}
        for step in forward:
            for dependency in step.depends_on:
                dependents[dependency].append(step.name)
        ready = sorted(name for name, degree in indegree.items() if degree == 0)
        order: list[str] = []
        while ready:
            current = ready.pop(0)
            order.append(current)
            for dependent in sorted(dependents[current]):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)
                    ready.sort()
        return order

    def step(self, name: str) -> StepDefinition:
        for candidate in self.steps:
            if candidate.name == name:
                return candidate
        raise ValidationError(f"workflow '{self.workflow_id}' has no step '{name}'", step=name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflowId": self.workflow_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "tags": list(self.tags),
            "sloMs": self.slo_ms,
            "timeoutSeconds": self.timeout_seconds,
            "executionOrder": self.topological_order(),
            "steps": [
                {
                    "name": s.name,
                    "operationId": s.operation_id,
                    "dependsOn": list(s.depends_on),
                    "optional": s.optional,
                    "compensationOnly": s.compensation_only,
                    "approvalRequired": s.approval_required,
                    "compensateWith": s.compensate_with,
                    "timeoutSeconds": s.timeout_seconds,
                    "retry": {
                        "maxAttempts": s.retry.max_attempts,
                        "initialDelaySeconds": s.retry.initial_delay_seconds,
                        "retryOn": list(s.retry.retry_on),
                    } if s.retry else None,
                }
                for s in self.steps
            ],
        }


@dataclass(slots=True)
class StepRun:
    """Execution record for one step."""

    name: str
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    output: Any = None
    error: dict[str, Any] | None = None
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": str(self.status),
            "attempts": self.attempts,
            "startedAt": self.started_at.isoformat() if self.started_at else None,
            "completedAt": self.completed_at.isoformat() if self.completed_at else None,
            "durationMs": round(self.duration_ms, 3),
            "output": self.output,
            "error": self.error,
        }


@dataclass(slots=True)
class WorkflowContext:
    """Mutable state shared across the steps of one execution."""

    workflow_id: str
    execution_id: str
    tenant_id: str
    input: dict[str, Any]
    outputs: dict[str, Any] = field(default_factory=dict)
    step_runs: dict[str, StepRun] = field(default_factory=dict)
    correlation_id: str = ""
    variables: dict[str, Any] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def resolve(self, template: str | None) -> Any:  # noqa: ANN401
        """Resolve a dotted path against inputs then accumulated outputs."""
        from pas_core.rules.engine import resolve_path  # noqa: PLC0415

        if not template:
            return None
        return resolve_path({**self.input, **self.outputs, **self.variables}, template)

    def put(self, key: str, value: Any) -> None:  # noqa: ANN401
        self.outputs[key] = value

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflowId": self.workflow_id,
            "executionId": self.execution_id,
            "tenantId": self.tenant_id,
            "correlationId": self.correlation_id,
            "startedAt": self.started_at.isoformat(),
            "input": self.input,
            "outputs": self.outputs,
            "steps": {name: run.to_dict() for name, run in self.step_runs.items()},
        }


@dataclass(slots=True)
class WorkflowExecution:
    """A single run of a workflow."""

    execution_id: str
    workflow_id: str
    workflow_version: int
    tenant_id: str
    status: WorkflowStatus = WorkflowStatus.PENDING
    context: WorkflowContext | None = None
    error: dict[str, Any] | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    approval: dict[str, Any] | None = None

    @property
    def duration_ms(self) -> float:
        end = self.completed_at or datetime.now(UTC)
        return (end - self.started_at).total_seconds() * 1000

    def to_dict(self) -> dict[str, Any]:
        return {
            "executionId": self.execution_id,
            "workflowId": self.workflow_id,
            "workflowVersion": self.workflow_version,
            "tenantId": self.tenant_id,
            "status": str(self.status),
            "startedAt": self.started_at.isoformat(),
            "completedAt": self.completed_at.isoformat() if self.completed_at else None,
            "durationMs": round(self.duration_ms, 3),
            "error": self.error,
            "approval": self.approval,
            "context": self.context.to_dict() if self.context else None,
        }


class WorkflowEngine:
    """Interface implemented by the local and Temporal engines."""

    async def start(
        self, definition: WorkflowDefinition, ctx: TenantContext, payload: dict[str, Any]
    ) -> WorkflowExecution: ...

    async def get(self, execution_id: str) -> WorkflowExecution | None: ...


class LocalWorkflowEngine(WorkflowEngine):
    """Deterministic in-process engine.

    Executes steps in topological order, honours retries, compensation and
    optional steps, and records a full execution trace.  Determinism matters:
    contract tests assert on the exact step order and outcome.
    """

    def __init__(self, *, operation_invoker: Callable[..., Awaitable[Any]] | None = None) -> None:
        self._definitions: dict[str, WorkflowDefinition] = {}
        self._executions: dict[str, WorkflowExecution] = {}
        self._invoker = operation_invoker
        self._lock = asyncio.Lock()

    def register(self, definition: WorkflowDefinition) -> WorkflowDefinition:
        self._definitions[definition.workflow_id] = definition
        return definition

    def register_all(self, definitions: Iterable[WorkflowDefinition]) -> None:
        for definition in definitions:
            self.register(definition)

    def definition(self, workflow_id: str) -> WorkflowDefinition:
        try:
            return self._definitions[workflow_id]
        except KeyError as exc:
            raise PasError(
                ErrorCode.NOT_FOUND,
                f"Unknown workflow '{workflow_id}'",
                {"workflowId": workflow_id, "available": sorted(self._definitions)},
            ) from exc

    def catalogue(self) -> list[dict[str, Any]]:
        return [d.to_dict() for d in sorted(self._definitions.values(), key=lambda x: x.workflow_id)]

    async def start(
        self, definition: WorkflowDefinition, ctx: TenantContext, payload: dict[str, Any]
    ) -> WorkflowExecution:
        execution_id = uuid.uuid4().hex
        workflow_ctx = WorkflowContext(
            workflow_id=definition.workflow_id,
            execution_id=execution_id,
            tenant_id=ctx.tenant_id,
            input=dict(payload),
            correlation_id=ctx.correlation_id,
        )
        for step in definition.steps:
            workflow_ctx.step_runs[step.name] = StepRun(name=step.name)
        execution = WorkflowExecution(
            execution_id=execution_id,
            workflow_id=definition.workflow_id,
            workflow_version=definition.version,
            tenant_id=ctx.tenant_id,
            status=WorkflowStatus.RUNNING,
            context=workflow_ctx,
        )
        async with self._lock:
            self._executions[execution_id] = execution

        if ACTIVE_WORKFLOWS is not None:
            ACTIVE_WORKFLOWS.labels(plugin=ctx.effective_pas_vendor).inc()
        GLOBAL_METRICS.increment("workflow_starts_total", workflow=definition.workflow_id)
        try:
            await self._run(definition, ctx, execution, workflow_ctx)
        finally:
            execution.completed_at = datetime.now(UTC)
            if ACTIVE_WORKFLOWS is not None:
                ACTIVE_WORKFLOWS.labels(plugin=ctx.effective_pas_vendor).dec()
        GLOBAL_METRICS.observe(
            "workflow_duration_ms", execution.duration_ms, workflow=definition.workflow_id
        )
        return execution

    async def _run(
        self,
        definition: WorkflowDefinition,
        ctx: TenantContext,
        execution: WorkflowExecution,
        workflow_ctx: WorkflowContext,
    ) -> None:
        completed: list[str] = []
        deadline = datetime.now(UTC) + timedelta(seconds=definition.timeout_seconds)
        try:
            for step_name in definition.topological_order():
                step = definition.step(step_name)
                if datetime.now(UTC) > deadline:
                    raise PasError(
                        ErrorCode.INTERNAL,
                        f"workflow '{definition.workflow_id}' exceeded its "
                        f"{definition.timeout_seconds}s budget",
                        {"workflowId": definition.workflow_id},
                    )
                run = workflow_ctx.step_runs[step_name]
                run.started_at = datetime.now(UTC)
                run.status = StepStatus.RUNNING
                try:
                    output = await self._execute_step(step, ctx, workflow_ctx)
                    run.output = output
                    run.status = StepStatus.SUCCEEDED
                    completed.append(step_name)
                    key = step.output_key or step_name
                    workflow_ctx.outputs[key] = output
                except PasError as exc:
                    run.error = exc.to_problem()
                    run.status = StepStatus.FAILED
                    if step.optional:
                        run.status = StepStatus.SKIPPED
                        completed.append(step_name)
                        continue
                    await self._compensate(definition, ctx, workflow_ctx, completed, exc)
                    if execution.status is not WorkflowStatus.COMPENSATED:
                        execution.status = WorkflowStatus.FAILED
                    execution.error = exc.to_problem()
                    return
                finally:
                    run.completed_at = datetime.now(UTC)
                    run.duration_ms = (
                        (run.completed_at - run.started_at).total_seconds() * 1000  # type: ignore[operator]
                        if run.started_at
                        else 0.0
                    )
                    if WORKFLOW_STEPS is not None:
                        WORKFLOW_STEPS.labels(
                            plugin=ctx.effective_pas_vendor,
                            workflow=definition.workflow_id,
                            step=step_name,
                            outcome=str(run.status),
                        ).observe(run.duration_ms / 1000)
        except PasError as exc:
            execution.status = WorkflowStatus.FAILED
            execution.error = exc.to_problem()

        if execution.status is WorkflowStatus.RUNNING:
            execution.status = WorkflowStatus.SUCCEEDED
        GLOBAL_METRICS.increment(
            "workflow_completions_total",
            workflow=definition.workflow_id,
            outcome=str(execution.status),
        )

    async def _execute_step(
        self, step: StepDefinition, ctx: TenantContext, workflow_ctx: WorkflowContext
    ) -> Any:  # noqa: ANN401
        inputs = {key: workflow_ctx.resolve(path) for key, path in step.input_template.items()}
        policy = step.retry or RetryPolicy(max_attempts=1)
        last_error: BaseException | None = None

        for attempt in range(1, policy.max_attempts + 1):
            run = workflow_ctx.step_runs[step.name]
            run.attempts = attempt
            try:
                if step.handler is not None:
                    result = await asyncio.wait_for(
                        step.handler(_StepInput(inputs, workflow_ctx, ctx)), timeout=step.timeout_seconds
                    )
                elif self._invoker is not None and step.operation_id:
                    result = await asyncio.wait_for(
                        self._invoker(step.operation_id, inputs, ctx), timeout=step.timeout_seconds
                    )
                else:
                    raise PasError(
                        ErrorCode.INTERNAL,
                        f"step '{step.name}' has no handler bound and no operation invoker",
                        {"step": step.name, "operationId": step.operation_id},
                    )
                return result
            except TimeoutError as timeout:
                # A step that overruns its budget is a catalogue failure, not a
                # bare builtin exception: the workflow must fail cleanly with a
                # trace rather than escaping as an unhandled error.
                last_error = PasError(
                    ErrorCode.PAS_TIMEOUT,
                    f"step '{step.name}' exceeded its {step.timeout_seconds}s budget",
                    {
                        "step": step.name,
                        "timeoutSeconds": step.timeout_seconds,
                        "attempt": attempt,
                    },
                )
                if policy.should_retry(attempt, last_error):
                    await asyncio.sleep(policy.delay_for(attempt))
                    continue
                raise last_error from timeout
            except PasError as error:
                last_error = error
                if policy.should_retry(attempt, error):
                    await asyncio.sleep(policy.delay_for(attempt))
                    continue
                raise
            except RuntimeError as error:
                last_error = PasError(
                    ErrorCode.INTERNAL,
                    f"step '{step.name}' failed: {error}",
                    {"step": step.name, "attempt": attempt},
                )
                if policy.should_retry(attempt, last_error):
                    await asyncio.sleep(policy.delay_for(attempt))
                    continue
                raise last_error from error
        raise PasError(
            ErrorCode.INTERNAL,
            f"step '{step.name}' exhausted {policy.max_attempts} attempt(s): {last_error}",
            {"step": step.name, "attempts": policy.max_attempts},
        )

    async def _compensate(
        self,
        definition: WorkflowDefinition,
        ctx: TenantContext,
        workflow_ctx: WorkflowContext,
        completed: list[str],
        cause: BaseException,
    ) -> None:
        """Run compensating steps in reverse order (saga pattern).

        Compensation status is set on the execution *before* the steps run, so a
        compensation that itself fails leaves the execution honestly marked as
        COMPENSATED-with-failures rather than silently reverting to FAILED and
        hiding the fact that a carrier record was partially written.
        """
        compensations = [
            (definition.step(name), definition.step(name).compensate_with)
            for name in reversed(completed)
            if definition.step(name).compensate_with
        ]
        if not compensations:
            return
        execution = self._executions.get(workflow_ctx.execution_id)
        if execution is not None:
            execution.status = WorkflowStatus.COMPENSATED
        failures: list[str] = []
        for step, compensation_name in compensations:
            compensation = definition.step(compensation_name)
            run = workflow_ctx.step_runs.setdefault(compensation_name, StepRun(name=compensation_name))
            run.started_at = datetime.now(UTC)
            run.status = StepStatus.RUNNING
            try:
                await self._execute_step(compensation, ctx, workflow_ctx)
                run.status = StepStatus.COMPENSATED
            except PasError as exc:  # compensation failure is recorded, not raised
                run.status = StepStatus.FAILED
                run.error = exc.to_problem()
                failures.append(compensation_name)
            finally:
                run.completed_at = datetime.now(UTC)
        if failures and execution is not None:
            existing = execution.error or {}
            execution.error = {
                **(existing.get("compensationFailure") and existing or {}),
                "compensationFailure": {
                    "failedSteps": failures,
                    "message": (
                        f"compensation failed for {', '.join(failures)}; a carrier record "
                        "may require manual reconciliation"
                    ),
                },
            }
        GLOBAL_METRICS.increment(
            "workflow_compensations_total",
            workflow=definition.workflow_id,
            cause=type(cause).__name__,
        )

    async def get(self, execution_id: str) -> WorkflowExecution | None:
        return self._executions.get(execution_id)

    def executions(self, *, tenant_id: str | None = None) -> list[WorkflowExecution]:
        return [
            e for e in self._executions.values()
            if tenant_id is None or e.tenant_id == tenant_id
        ]

    def stats(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        for execution in self._executions.values():
            key = str(execution.status)
            by_status[key] = by_status.get(key, 0) + 1
        return {
            "workflowCount": len(self._definitions),
            "executionCount": len(self._executions),
            "byStatus": by_status,
        }


@dataclass(slots=True)
class _StepInput:
    """What a step handler receives."""

    values: dict[str, Any]
    context: WorkflowContext
    tenant: TenantContext

    def __getitem__(self, key: str) -> Any:  # noqa: ANN401
        return self.values.get(key)

    def get(self, key: str, default: Any = None) -> Any:  # noqa: ANN401
        return self.values.get(key, default)


class TemporalWorkflowEngine(WorkflowEngine):
    """Adapter for Temporal (open source) or AWS Step Functions.

    Temporal is the default production choice: it gives durable execution,
    exactly-once step semantics, timers for approval gates and a replayable
    history, which is what a carrier's issue pipeline needs.  The adapter is
    intentionally thin - all the semantics live in :class:`LocalWorkflowEngine`,
    so the two engines cannot drift.

    When the ``temporalio`` package is not installed the adapter raises a
    catalogue error naming the missing dependency rather than silently degrading.
    """

    def __init__(
        self,
        *,
        namespace: str = "pas-plugins",
        task_queue: str = "pas-workflows",
        address: str = "localhost:7233",
        workflow_registry: dict[str, WorkflowDefinition] | None = None,
    ) -> None:
        self.namespace = namespace
        self.task_queue = task_queue
        self.address = address
        self._client: Any = None
        self._definitions = dict(workflow_registry or {})

    def register(self, definition: WorkflowDefinition) -> WorkflowDefinition:
        self._definitions[definition.workflow_id] = definition
        return definition

    @property
    def available(self) -> bool:
        try:  # pragma: no cover - optional dependency
            import temporalio  # noqa: F401, PLC0415

            return True
        except ImportError:
            return False

    async def connect(self) -> Any:  # noqa: ANN401
        if self._client is not None:
            return self._client
        try:  # pragma: no cover - optional dependency guard
            from temporalio.client import Client  # noqa: PLC0415
        except ImportError as exc:
            raise PasError(
                ErrorCode.INTERNAL,
                "temporalio is not installed; run `pip install temporalio` to use the "
                "Temporal workflow engine, or set the engine to 'local'.",
                {"dependency": "temporalio"},
            ) from exc
        self._client = await Client.connect(  # pragma: no cover
            self.address, namespace=self.namespace
        )
        return self._client

    async def start(
        self, definition: WorkflowDefinition, ctx: TenantContext, payload: dict[str, Any]
    ) -> WorkflowExecution:
        """Start a Temporal workflow and return its tracking handle."""
        if definition.workflow_id not in self._definitions:
            self.register(definition)
        client = await self.connect()
        execution = WorkflowExecution(  # pragma: no cover
            execution_id=uuid.uuid4().hex,
            workflow_id=definition.workflow_id,
            workflow_version=definition.version,
            tenant_id=ctx.tenant_id,
            status=WorkflowStatus.RUNNING,
            context=WorkflowContext(
                workflow_id=definition.workflow_id,
                execution_id="",
                tenant_id=ctx.tenant_id,
                input=dict(payload),
                correlation_id=ctx.correlation_id,
            ),
        )
        await client.start_workflow(  # pragma: no cover
            name=definition.workflow_id,
            args=[dict(payload), ctx.tenant_id, ctx.correlation_id],
            task_queue=self.task_queue,
            id=execution.execution_id,
        )
        return execution

    async def get(self, execution_id: str) -> WorkflowExecution | None:  # pragma: no cover
        client = await self.connect()
        handle = client.get_workflow_handle(execution_id)
        description = await handle.describe()
        status_map = {
            "RUNNING": WorkflowStatus.RUNNING,
            "COMPLETED": WorkflowStatus.SUCCEEDED,
            "FAILED": WorkflowStatus.FAILED,
            "CANCELED": WorkflowStatus.CANCELLED,
            "TERMINATED": WorkflowStatus.FAILED,
            "TIMED_OUT": WorkflowStatus.TIMED_OUT,
        }
        return WorkflowExecution(
            execution_id=execution_id,
            workflow_id=getattr(description, "workflow_type", "unknown"),
            workflow_version=1,
            tenant_id=getattr(description, "task_queue", self.task_queue),
            status=status_map.get(str(getattr(description, "status", "")).upper(), WorkflowStatus.RUNNING),
        )


def build_engine(kind: str, **kwargs: Any) -> WorkflowEngine:  # noqa: ANN401
    """Select the workflow engine by name, as configured by ``P1_WORKFLOW_ENGINE``."""
    if kind == "temporal":
        return TemporalWorkflowEngine(**kwargs)
    return LocalWorkflowEngine(**kwargs)


def describe_workflows(workflows: Sequence[WorkflowDefinition]) -> dict[str, Any]:
    """MCP resource projection for the orchestration catalogue."""
    return {
        "workflows": [w.to_dict() for w in workflows],
        "engineNotes": (
            "Steps reference atomic operations by id. Compensation steps make the "
            "process a saga: a failure after a successful write is unwound rather "
            "than left dangling in the carrier's core system."
        ),
    }


def tenant_of(execution: WorkflowExecution) -> str:
    return execution.tenant_id or current_tenant_id()
