"""Deterministic QA Runner.

The Runner is intentionally boring: all collaborators are injected and the
stage trace is fixed.  There is no model, network, event, Redis, or database
integration in this module.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
import signal
import threading

from pydantic import BaseModel, ConfigDict, Field, ValidationError

try:  # The QA directory is also imported directly by its focused test suite.
    from .runtime_contracts import (
        AgentTaskContext,
        ArtifactRef,
        ErrorCode,
        WorkerContext,
        WorkerStatus,
        canonical_payload_hash,
        sha256_hash,
        to_worker_context,
        utc_now,
    )
except ImportError:  # pragma: no cover - direct ``sys.path`` imports
    from runtime_contracts import (  # type: ignore[no-redef]
        AgentTaskContext,
        ArtifactRef,
        ErrorCode,
        WorkerContext,
        WorkerStatus,
        canonical_payload_hash,
        sha256_hash,
        to_worker_context,
        utc_now,
    )


class RunnerStage(StrEnum):
    INPUT_SCHEMA = "Input Schema"
    TOOL_PLANNING = "Tool planning"
    ALLOWLIST = "Allowlist"
    TOOL_EXECUTION = "Tool execution"
    EVIDENCE_NORMALIZATION = "Evidence normalization"
    WORKER_INVOKE = "Worker Invoke"
    OUTPUT_SCHEMA = "Output Schema"
    RETRY_TIMEOUT_REPLAY = "Retry/Timeout/Replay"
    END_OR_ESCALATE = "END/ESCALATE"


RUNNER_STAGE_ORDER: tuple[RunnerStage, ...] = tuple(RunnerStage)


class ToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=128)
    scope: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=128)
    payload: Any
    evidence_refs: list[ArtifactRef] = Field(default_factory=list)
def _call_with_timeout(call: Callable[[], Any], timeout_ms: int) -> Any:
    """Bound injected tool/worker calls on main and service worker threads.

    Injected executors need not be pickleable, so a subprocess is not a safe
    universal boundary.  Use ``SIGALRM`` on the main thread and a daemon
    helper thread elsewhere.  Timed-out helpers are never joined: arbitrary
    injected code cannot be safely terminated, and joining would break the
    timeout bound.
    """
    timeout_seconds = timeout_ms / 1000
    current = threading.current_thread()
    if (
        current is threading.main_thread()
        and hasattr(signal, "SIGALRM")
        and hasattr(signal, "setitimer")
        and hasattr(signal, "ITIMER_REAL")
    ):
        def _alarm(_signum: int, _frame: Any) -> None:
            raise TimeoutError(f"call exceeded timeout ({timeout_ms}ms)")

        previous = signal.signal(signal.SIGALRM, _alarm)
        signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
        try:
            return call()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    result: list[Any] = []
    error: list[BaseException] = []
    finished = threading.Event()

    def _run() -> None:
        try:
            result.append(call())
        except BaseException as exc:  # Re-raise in the caller, including injected errors.
            error.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=_run, daemon=True, name="qa-runtime-timeout")
    worker.start()
    if not finished.wait(timeout_seconds):
        raise TimeoutError(f"call exceeded timeout ({timeout_ms}ms)")
    if error:
        raise error[0]
    return result[0]


class ReplayManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str
    attempts: int = Field(ge=0, le=3)
    input_hash: str
    stage_order: tuple[str, ...]


class RunnerOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    status: WorkerStatus
    error_code: ErrorCode | None = None
    error_detail: str | None = None
    stage_history: list[RunnerStage] = Field(default_factory=list)
    worker_context: WorkerContext | None = None
    tool_calls: list[ToolRequest] = Field(default_factory=list)
    evidence_refs: list[ArtifactRef] = Field(default_factory=list)
    payload_hash: str | None = None
    replay_manifest: ReplayManifest | None = None

    @property
    def passed(self) -> bool:
        """Only a completed, error-free run can be considered successful."""

        return self.status is WorkerStatus.COMPLETED and self.error_code is None


@runtime_checkable
class ToolGateway(Protocol):
    def plan(self, task: AgentTaskContext, input_payload: Mapping[str, Any]) -> Sequence[ToolRequest | Mapping[str, Any]]: ...

    def allow(self, request: ToolRequest, task: AgentTaskContext) -> bool: ...

    def execute(self, request: ToolRequest, task: AgentTaskContext) -> ToolResponse | Mapping[str, Any]: ...


@runtime_checkable
class WorkerExecutor(Protocol):
    def invoke(
        self,
        task: AgentTaskContext,
        evidence_refs: Sequence[ArtifactRef],
        input_payload: Mapping[str, Any],
    ) -> WorkerContext | Mapping[str, Any]: ...


class ToolRegistry:
    """Small dependency-injected gateway useful for production adapters and tests."""

    def __init__(
        self,
        handlers: Mapping[str, Callable[[Mapping[str, Any]], Any]] | None = None,
        *,
        allowed_tools: Sequence[str] = (),
        allowed_scopes: Sequence[str] = (),
    ) -> None:
        self._handlers = dict(handlers or {})
        self._allowed_tools = set(allowed_tools) if allowed_tools else set(self._handlers)
        self._allowed_scopes = set(allowed_scopes)
        self.calls: list[ToolRequest] = []

    def plan(self, task: AgentTaskContext, input_payload: Mapping[str, Any]) -> Sequence[ToolRequest]:
        requested = input_payload.get("tool_calls", ())
        if requested is None:
            return ()
        if not isinstance(requested, Sequence) or isinstance(requested, (str, bytes, bytearray)):
            raise ValueError("tool_calls must be a sequence")
        return [item if isinstance(item, ToolRequest) else ToolRequest.model_validate(item) for item in requested]

    def allow(self, request: ToolRequest, task: AgentTaskContext) -> bool:
        if task.department != "qa-department":
            return False
        return request.tool in self._allowed_tools and (
            not self._allowed_scopes or request.scope in self._allowed_scopes
        )

    def execute(self, request: ToolRequest, task: AgentTaskContext) -> ToolResponse:
        self.calls.append(request)
        handler = self._handlers.get(request.tool)
        if handler is None:
            raise PermissionError(ErrorCode.TOOLCALL_DENIED.value)
        return ToolResponse(tool=request.tool, payload=handler(request.arguments))



def build_qa_task_context(
    payload: Mapping[str, Any],
    *,
    worker: str = "qa-runner",
    case_id: str | None = None,
    task_id: str | None = None,
    trace_id: str | None = None,
    input_refs: Sequence[ArtifactRef | Mapping[str, Any]] | None = None,
    clock: RunnerClock = utc_now,
) -> AgentTaskContext:
    """Build a strict QA task envelope for legacy façade adapters."""

    if not isinstance(payload, Mapping):
        raise TypeError("QA payload must be an object")
    digest = canonical_payload_hash(payload)
    safe_case = case_id or str(payload.get("case_id") or payload.get("verification_id") or f"qa-case-{digest[7:23]}")
    safe_task = task_id or str(payload.get("task_id") or payload.get("verification_id") or f"{safe_case}-task")
    safe_trace = trace_id or str(payload.get("trace_id") or f"qa-trace-{digest[7:23]}")
    raw_refs = input_refs if input_refs is not None else payload.get("input_refs")
    if raw_refs is None:
        refs = [ArtifactRef(type="qa-payload", id=f"{safe_task}-payload", content_hash=digest)]
    else:
        if isinstance(raw_refs, (str, bytes, bytearray)) or not isinstance(raw_refs, Sequence):
            raise ValueError("input_refs must be a sequence")
        refs = [
            item if isinstance(item, ArtifactRef) else ArtifactRef.model_validate(item)
            for item in raw_refs
        ]
        if not refs:
            raise ValueError("input_refs must not be empty")
    now = clock()
    return AgentTaskContext(
        schema_version="agent-task-context.v1",
        case_id=safe_case,
        task_id=safe_task,
        department="qa-department",
        worker=worker,
        route=str(payload.get("route") or "qa-runtime"),
        input_refs=refs,
        trace_id=safe_trace,
        status="RUNNING",
        attempt=1,
        idempotency_key=f"qa:{safe_task}:{digest}",
        created_at=now,
        updated_at=now,
    )

class RunnerClock(Protocol):
    def __call__(self) -> datetime: ...


@dataclass(frozen=True)
class _Failure:
    status: WorkerStatus
    code: ErrorCode
    detail: str


class QARunner:
    """One canonical QA execution owner with a fixed, inspectable stage order."""

    def __init__(
        self,
        *,
        tools: ToolGateway | None,
        executor: WorkerExecutor,
        clock: RunnerClock = utc_now,
        profile_version: str = "qa-profile-unknown",
        model_version: str = "deterministic:qa-runtime-v1",
        adapter_version: str = "none",
        timeout_ms: int = 30000,
        max_retries: int = 0,
        producer_worker: str = "qa-runner",
    ) -> None:
        if timeout_ms < 1:
            raise ValueError("timeout_ms must be positive")
        if not 0 <= max_retries <= 2:
            raise ValueError("max_retries must be between 0 and 2")
        self.tools = tools
        self.executor = executor
        self.clock = clock
        self.profile_version = profile_version
        self.model_version = model_version
        self.adapter_version = adapter_version
        self.timeout_ms = timeout_ms
        self.max_retries = max_retries
        self.producer_worker = producer_worker

    def run(
        self,
        task: AgentTaskContext | Mapping[str, Any],
        input_payload: Mapping[str, Any] | None = None,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> RunnerOutcome:
        """Run the canonical QA stages and return a fail-closed outcome."""

        if input_payload is not None and payload is not None:
            raise TypeError("provide input_payload or payload, not both")
        input_payload = input_payload if input_payload is not None else payload
        payload_hash = canonical_payload_hash(input_payload)
        stages: list[RunnerStage] = [RunnerStage.INPUT_SCHEMA]
        try:
            task_model = task if isinstance(task, AgentTaskContext) else AgentTaskContext.model_validate(task)
        except ValidationError as exc:
            return self._invalid_outcome(stages, ErrorCode.INVALID_INPUT, str(exc), payload_hash)
        if task_model.department != "qa-department":
            return self._failure_outcome(
                task_model,
                stages,
                _Failure(WorkerStatus.REJECTED, ErrorCode.INVALID_INPUT, "QA runner received a non-QA task"),
                attempts=0,
                payload_hash=payload_hash,
            )
        if task_model.status not in {"CREATED", "QUEUED", "RUNNING"}:
            return self._failure_outcome(
                task_model,
                stages,
                _Failure(
                    WorkerStatus.REJECTED,
                    ErrorCode.INVALID_INPUT,
                    f"task status is not runnable: {task_model.status.value}",
                ),
                attempts=0,
                payload_hash=payload_hash,
            )
        if not isinstance(input_payload, Mapping):
            return self._failure_outcome(
                task_model,
                stages,
                _Failure(WorkerStatus.HOLD, ErrorCode.INVALID_INPUT, "input_payload must be an object"),
                attempts=0,
                payload_hash=payload_hash,
            )
        if not input_payload:
            return self._failure_outcome(
                task_model,
                stages,
                _Failure(WorkerStatus.HOLD, ErrorCode.MISSING_INPUT, "required QA input payload is missing"),
                attempts=0,
                payload_hash=payload_hash,
            )

        stages.append(RunnerStage.TOOL_PLANNING)
        try:
            if "tool_calls" in input_payload:
                self._validate_declared_tool_calls(input_payload["tool_calls"])
            requests = self._plan(task_model, input_payload)
        except Exception as exc:
            return self._failure_outcome(
                task_model,
                stages,
                _Failure(WorkerStatus.DEGRADED, ErrorCode.SCHEMA_FAILURE, f"tool plan invalid: {exc}"),
                attempts=0,
                payload_hash=payload_hash,
            )

        stages.append(RunnerStage.ALLOWLIST)
        for request in requests:
            if self.tools is None or not self._allowed(request, task_model):
                return self._failure_outcome(
                    task_model,
                    stages,
                    _Failure(WorkerStatus.REJECTED, ErrorCode.TOOLCALL_DENIED, f"tool denied: {request.tool}"),
                    attempts=0,
                    tool_calls=requests,
                    payload_hash=payload_hash,
                )

        stages.append(RunnerStage.TOOL_EXECUTION)
        responses: list[ToolResponse] = []
        try:
            for request in requests:
                responses.append(self._execute(request, task_model, timeout_ms=self.timeout_ms))
        except TimeoutError as exc:
            return self._failure_outcome(
                task_model,
                stages,
                _Failure(WorkerStatus.ESCALATED, ErrorCode.TIMEOUT, str(exc) or "tool timed out"),
                attempts=0,
                tool_calls=requests,
                payload_hash=payload_hash,
            )
        except MemoryError as exc:
            return self._failure_outcome(
                task_model,
                stages,
                _Failure(WorkerStatus.ESCALATED, ErrorCode.OOM, str(exc) or "tool ran out of memory"),
                attempts=0,
                tool_calls=requests,
                payload_hash=payload_hash,
            )
        except PermissionError as exc:
            return self._failure_outcome(
                task_model,
                stages,
                _Failure(WorkerStatus.REJECTED, ErrorCode.TOOLCALL_DENIED, str(exc) or "tool denied"),
                attempts=0,
                tool_calls=requests,
                payload_hash=payload_hash,
            )
        except Exception as exc:
            code = self._exception_code(exc)
            return self._failure_outcome(
                task_model,
                stages,
                _Failure(self._status_for_code(code), code, str(exc) or "tool failed"),
                attempts=0,
                tool_calls=requests,
                payload_hash=payload_hash,
            )

        stages.append(RunnerStage.EVIDENCE_NORMALIZATION)
        try:
            evidence_refs = self._normalize_evidence(responses)
        except Exception as exc:
            return self._failure_outcome(
                task_model,
                stages,
                _Failure(WorkerStatus.DEGRADED, ErrorCode.EVIDENCE_FAILURE, str(exc) or "evidence invalid"),
                attempts=0,
                tool_calls=requests,
                payload_hash=payload_hash,
            )

        stages.extend(
            (
                RunnerStage.WORKER_INVOKE,
                RunnerStage.OUTPUT_SCHEMA,
                RunnerStage.RETRY_TIMEOUT_REPLAY,
            )
        )
        attempts = 0
        worker_context: WorkerContext | None = None
        failure: _Failure | None = None
        while attempts <= self.max_retries:
            attempts += 1
            started = self.clock()
            try:
                raw = _call_with_timeout(
                    lambda: self._invoke(task_model, evidence_refs, input_payload),
                    self.timeout_ms,
                )
                worker_context = self._coerce_worker_context(
                    raw, task_model, evidence_refs, attempts, payload_hash
                )
                elapsed = self._elapsed_ms(started)
                if elapsed > self.timeout_ms:
                    raise TimeoutError(f"worker exceeded timeout ({elapsed}ms > {self.timeout_ms}ms)")
                failure = None
                break
            except TimeoutError as exc:
                failure = _Failure(WorkerStatus.ESCALATED, ErrorCode.TIMEOUT, str(exc) or "worker timed out")
            except MemoryError as exc:
                failure = _Failure(WorkerStatus.ESCALATED, ErrorCode.OOM, str(exc) or "worker ran out of memory")
            except ValidationError as exc:
                failure = _Failure(WorkerStatus.DEGRADED, ErrorCode.SCHEMA_FAILURE, str(exc))
            except ValueError as exc:
                code = self._exception_code(exc)
                failure = _Failure(
                    WorkerStatus.DEGRADED if code is ErrorCode.SCHEMA_FAILURE else self._status_for_code(code),
                    code,
                    str(exc) or "worker output invalid",
                )
            except Exception as exc:
                code = self._exception_code(exc)
                failure = _Failure(self._status_for_code(code), code, str(exc) or "worker crashed")
        if failure is not None:
            if attempts > self.max_retries:
                failure = _Failure(failure.status, failure.code, f"{failure.detail}; attempts={attempts}")
            stages.append(RunnerStage.END_OR_ESCALATE)
            return self._failure_outcome(
                task_model,
                stages,
                failure,
                attempts=attempts,
                tool_calls=requests,
                evidence_refs=evidence_refs,
                payload_hash=payload_hash,
            )

        assert worker_context is not None
        stages.append(RunnerStage.END_OR_ESCALATE)
        worker_error = self._context_error_code(worker_context)
        return RunnerOutcome(
            status=worker_context.status,
            error_code=worker_error,
            error_detail=None if worker_error is None else "worker returned a non-completed status",
            stage_history=stages,
            worker_context=worker_context,
            tool_calls=requests,
            evidence_refs=evidence_refs,
            replay_manifest=ReplayManifest(
                trace_id=task_model.trace_id,
                attempts=attempts,
                input_hash=payload_hash,
                stage_order=tuple(stage.value for stage in stages),
            ),
            payload_hash=payload_hash,
        )

    def _plan(self, task: AgentTaskContext, payload: Mapping[str, Any]) -> list[ToolRequest]:
        if self.tools is None:
            requested = payload.get("tool_calls", ())
        else:
            requested = self.tools.plan(task, payload)
        return [item if isinstance(item, ToolRequest) else ToolRequest.model_validate(item) for item in requested]

    def _allowed(self, request: ToolRequest, task: AgentTaskContext) -> bool:
        allow = getattr(self.tools, "allow", None)
        return bool(allow and allow(request, task))

    def _validate_declared_tool_calls(self, requested: Any) -> None:
        if requested is None:
            return
        if isinstance(requested, (str, bytes, bytearray)) or not isinstance(requested, Sequence):
            raise ValueError("tool_calls must be a sequence")
        for item in requested:
            ToolRequest.model_validate(item)
    def _execute(
        self, request: ToolRequest, task: AgentTaskContext, *, timeout_ms: int
    ) -> ToolResponse:
        raw = _call_with_timeout(
            lambda: self.tools.execute(request, task), timeout_ms  # type: ignore[union-attr]
        )
        return raw if isinstance(raw, ToolResponse) else ToolResponse.model_validate(raw)

    def _normalize_evidence(self, responses: Sequence[ToolResponse]) -> list[ArtifactRef]:
        refs: list[ArtifactRef] = []
        for index, response in enumerate(responses):
            if response.evidence_refs:
                refs.extend(response.evidence_refs)
            else:
                refs.append(
                    ArtifactRef(
                        type="qa-evidence",
                        id=f"{response.tool}:{index}",
                        content_hash=sha256_hash(response.payload),
                    )
                )
        # Preserve order while deduplicating exact references.
        unique: dict[tuple[str, str], ArtifactRef] = {(ref.type, ref.id): ref for ref in refs}
        return list(unique.values())

    def _invoke(
        self,
        task: AgentTaskContext,
        evidence_refs: Sequence[ArtifactRef],
        payload: Mapping[str, Any],
    ) -> WorkerContext | Mapping[str, Any]:
        invoke = getattr(self.executor, "invoke", None)
        if invoke is None:
            if callable(self.executor):  # type: ignore[call-overload]
                return self.executor(task, evidence_refs, payload)  # type: ignore[misc]
            raise TypeError("executor must implement invoke")
        return invoke(task, evidence_refs, payload)

    def _coerce_worker_context(
        self,
        raw: WorkerContext | Mapping[str, Any],
        task: AgentTaskContext,
        evidence_refs: Sequence[ArtifactRef],
        attempt: int,
        payload_hash: str,
    ) -> WorkerContext:
        if isinstance(raw, WorkerContext):
            self._validate_worker_context(raw, task, payload_hash)
            return raw
        if not isinstance(raw, Mapping):
            raise ValueError("worker output must be an object")
        data = dict(raw)
        if "context_id" in data:
            context = WorkerContext.model_validate(data)
            self._validate_worker_context(context, task, payload_hash)
            return context

        allowed_legacy = {
            "status",
            "verdict",
            "advisory",
            "summary",
            "producer_worker",
            "profile_version",
            "model_version",
            "adapter_version",
            "reason_codes",
            "output_refs",
            "input_hash",
            "output_hash",
            "calculation_version",
            "context_id",
            "replay_manifest_ref",
            "decision",
            "binding",
            "authoritative",
            "findings",
            "claim_checks",
            "error_code",
        }
        unknown = sorted(set(data) - allowed_legacy)
        if unknown:
            raise ValueError(f"worker output has unknown fields: {', '.join(unknown)}")
        provided_hash = data.pop("input_hash", None)
        if provided_hash is not None and provided_hash != payload_hash:
            raise ValueError("worker input_hash does not match the canonical payload hash")
        decision = data.pop("decision", None)
        status = data.pop("status", data.pop("verdict", "COMPLETED"))
        error_code = data.pop("error_code", None)
        if error_code is None:
            try:
                error_code = ErrorCode(str(status))
                status = self._status_for_code(error_code)
            except ValueError:
                pass
        if decision is not None and str(status).upper() in {"PASS", "COMPLETED"}:
            # Legacy adapters often carry the deterministic verdict separately
            # from transport status. Never let FAIL/WARN become COMPLETED.
            status = decision
        advisory = data.pop("advisory", None) or {
            "summary": str(data.pop("summary", "QA worker completed"))
        }
        refs = data.pop("output_refs", evidence_refs)
        producer = str(data.pop("producer_worker", self.producer_worker))
        if producer != self.producer_worker:
            raise ValueError("worker producer does not match the canonical runner")
        context = to_worker_context(
            task,
            producer_worker=self.producer_worker,
            profile_version=str(data.pop("profile_version", self.profile_version)),
            model_version=str(data.pop("model_version", self.model_version)),
            adapter_version=str(data.pop("adapter_version", self.adapter_version)),
            status=status,
            advisory=advisory,
            decision=decision,
            reason_codes=data.pop("reason_codes", ()),
            error_code=error_code,
            output_refs=refs,
            input_hash=payload_hash,
            output_hash=data.pop("output_hash", sha256_hash(data)),
            calculation_version=data.pop("calculation_version", "qa-runtime-v1"),
            timeout_ms=self.timeout_ms,
            attempt=attempt,
            context_id=data.pop("context_id", None),
            replay_manifest_ref=data.pop("replay_manifest_ref", None),
            clock=self.clock,
        )
        self._validate_worker_context(context, task, payload_hash)
        return context

    def _validate_worker_context(
        self,
        context: WorkerContext,
        task: AgentTaskContext,
        payload_hash: str,
    ) -> None:
        if context.schema_version != "qa.worker-context.v1":
            raise ValueError("worker schema does not match qa.worker-context.v1")
        if context.department != task.department or context.department != "qa-department":
            raise ValueError("worker department does not map to the QA task")
        if context.input_contract != "qa.department-input.v1":
            raise ValueError("worker input contract does not map to QA")
        if context.producer_worker != self.producer_worker:
            raise ValueError("worker producer does not match the canonical runner")
        if (
            context.case_id,
            context.task_id,
            context.trace_id,
            context.consumer_worker,
        ) != (task.case_id, task.task_id, task.trace_id, task.worker):
            raise ValueError("worker context identity does not map to the input task")
        if context.input_refs != task.input_refs:
            raise ValueError("worker input_refs do not map to the input task")
        if context.input_hash != payload_hash:
            raise ValueError("worker input_hash does not match the canonical payload hash")

    @staticmethod
    def _context_error_code(context: WorkerContext) -> ErrorCode | None:
        if context.status is WorkerStatus.COMPLETED:
            return None
        for reason in context.reason_codes:
            try:
                return ErrorCode(reason)
            except ValueError:
                continue
        return ErrorCode.SCHEMA_FAILURE

    @staticmethod
    def _status_for_code(code: ErrorCode) -> WorkerStatus:
        if code is ErrorCode.TOOLCALL_DENIED:
            return WorkerStatus.REJECTED
        if code in {ErrorCode.TIMEOUT, ErrorCode.OOM, ErrorCode.CRASHED}:
            return WorkerStatus.ESCALATED
        return WorkerStatus.DEGRADED
    @staticmethod
    def _exception_code(exc: Exception) -> ErrorCode:
        text = str(exc).upper()
        for code in ErrorCode:
            if code.value in text:
                return code
        if isinstance(exc, (TypeError, ValueError, ValidationError)):
            return ErrorCode.SCHEMA_FAILURE
        return ErrorCode.CRASHED

    def _elapsed_ms(self, started: datetime) -> int:
        ended = self.clock()
        if isinstance(started, (int, float)) and isinstance(ended, (int, float)):
            return max(0, int((ended - started) * 1000))
        if not isinstance(started, datetime) or not isinstance(ended, datetime):
            return 0
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=timezone.utc)
        return max(0, int((ended - started).total_seconds() * 1000))

    def _invalid_outcome(
        self,
        stages: list[RunnerStage],
        code: ErrorCode,
        detail: str,
        payload_hash: str,
    ) -> RunnerOutcome:
        stages.append(RunnerStage.END_OR_ESCALATE)
        return RunnerOutcome(
            status=WorkerStatus.ESCALATED,
            error_code=code,
            error_detail=detail,
            stage_history=stages,
            payload_hash=payload_hash,
        )

    def _failure_outcome(
        self,
        task: AgentTaskContext,
        stages: list[RunnerStage],
        failure: _Failure,
        *,
        attempts: int,
        tool_calls: Sequence[ToolRequest] = (),
        evidence_refs: Sequence[ArtifactRef] = (),
        payload_hash: str,
    ) -> RunnerOutcome:
        if not stages or stages[-1] is not RunnerStage.END_OR_ESCALATE:
            stages = [*stages, RunnerStage.RETRY_TIMEOUT_REPLAY, RunnerStage.END_OR_ESCALATE]
        context = to_worker_context(
            task,
            producer_worker=self.producer_worker,
            profile_version=self.profile_version,
            model_version=self.model_version,
            adapter_version=self.adapter_version,
            status=failure.status,
            advisory={"summary": failure.detail},
            reason_codes=[failure.code.value],
            output_refs=evidence_refs,
            input_hash=payload_hash,
            output_hash=sha256_hash({"error_code": failure.code.value, "detail": failure.detail}),
            calculation_version="qa-runtime-v1",
            timeout_ms=self.timeout_ms,
            attempt=max(1, min(3, task.attempt + max(0, attempts - 1))),
            clock=self.clock,
        )
        return RunnerOutcome(
            status=failure.status,
            error_code=failure.code,
            error_detail=failure.detail,
            stage_history=stages,
            worker_context=context,
            tool_calls=list(tool_calls),
            evidence_refs=list(evidence_refs),
            replay_manifest=ReplayManifest(
                trace_id=task.trace_id,
                attempts=attempts,
                input_hash=payload_hash,
                stage_order=tuple(stage.value for stage in stages),
            ),
            payload_hash=payload_hash,
        )


# Explicit public spelling used by some service code.
Runner = QARunner

__all__ = [
    "QARunner",
    "RUNNER_STAGE_ORDER",
    "ReplayManifest",
    "Runner",
    "RunnerOutcome",
    "RunnerStage",
    "ToolGateway",
    "ToolRegistry",
    "ToolRequest",
    "build_qa_task_context",
    "ToolResponse",
    "WorkerExecutor",
]
