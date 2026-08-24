"""QA-only deterministic Shadow/Mock evaluation service.

The Eval Runner has a deliberately closed dependency surface.  Candidate
execution, tools, memory, clock, and audit persistence are injected; this
module does not import a model client, HTTP client, event bus, HR/workforce
module, or the QA decision engine.
"""
from __future__ import annotations

import copy
import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4
import signal
import threading

def _call_with_timeout(call: Callable[[], Any], timeout_ms: int) -> Any:
    """Bound an injected call on both the main thread and service threads.

    Injected runners are not required to be pickleable, so a subprocess is not
    a safe universal isolation boundary.  Main-thread calls use ``SIGALRM``;
    calls made by service worker threads use a daemon thread and a bounded
    wait.  A timed-out daemon is deliberately not joined: Python cannot safely
    terminate an arbitrary injected callable, and joining would defeat the
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
            raise TimeoutError(f"candidate exceeded timeout ({timeout_ms}ms)")

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

    worker = threading.Thread(target=_run, daemon=True, name="qa-eval-timeout")
    worker.start()
    if not finished.wait(timeout_seconds):
        raise TimeoutError(f"candidate exceeded timeout ({timeout_ms}ms)")
    if error:
        raise error[0]
    return result[0]

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

try:
    from .runtime_contracts import sha256_hash, utc_now
except ImportError:  # pragma: no cover - direct ``sys.path`` imports
    from runtime_contracts import sha256_hash, utc_now  # type: ignore[no-redef]


class EvalMetric(StrEnum):
    ACCURACY = "accuracy"
    HALLUCINATION_SCORE = "hallucination_score"
    TOOL_COMPLIANCE = "tool_compliance"
    CITATION_PRECISION = "citation_precision"
    LATENCY_MS = "latency_ms"
    RISK_COMPLIANCE = "risk_compliance"


METRICS: tuple[EvalMetric, ...] = tuple(EvalMetric)
EvaluationMetric = EvalMetric
METRIC_VERSION = "qa-eval-metrics-v1"


class EvalErrorCode(StrEnum):
    TIMEOUT = "TIMEOUT"
    TOOLCALL_DENIED = "TOOLCALL_DENIED"
    OOM = "OOM"
    CRASHED = "CRASHED"
    SCHEMA_FAILURE = "SCHEMA_FAILURE"
    MISSING_INPUT = "MISSING_INPUT"
    EVAL_SET_MISMATCH = "EVAL_SET_MISMATCH"
    UNSUPPORTED_ENVIRONMENT = "UNSUPPORTED_ENVIRONMENT"
    CANDIDATE_FAILURE = "CANDIDATE_FAILURE"


class EvalSetMismatchError(ValueError):
    """Candidate/champion comparisons require the exact same set identity."""

    error_code = EvalErrorCode.EVAL_SET_MISMATCH.value


class AppendOnlyViolation(RuntimeError):
    """Raised when an eval run/result is updated or deleted."""


class EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class EvalCase(EvalModel):
    case_key: str = Field(min_length=1, max_length=256)
    input_payload: dict[str, Any] = Field(default_factory=dict)
    expected: Any = None
    expected_decision: str | None = None
    expected_output: Any = None
    expected_citations: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    expected_risk_compliance: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EvalCase":
        data = dict(value)
        if "input" in data and "input_payload" not in data:
            data["input_payload"] = data.pop("input")
        if "label" in data and "expected" not in data:
            data["expected"] = data.pop("label")
        return cls.model_validate(data)

class CandidateCase(EvalModel):
    """Candidate-visible case view; ground-truth fields stay scorer-private."""

    case_key: str
    input_payload: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_eval_case(cls, case: EvalCase) -> "CandidateCase":
        safe_metadata = {
            key: copy.deepcopy(value)
            for key, value in case.metadata.items()
            if not key.startswith("expected_") and key not in {"answer", "label", "ground_truth"}
        }
        return cls(
            case_key=case.case_key,
            input_payload=copy.deepcopy(case.input_payload),
            allowed_tools=list(case.allowed_tools),
            metadata=safe_metadata,
        )

class EvalSet(EvalModel):
    eval_set_id: str = Field(min_length=1, max_length=128)
    role_code: str = Field(min_length=1, max_length=128)
    version: int = Field(gt=0)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    cases: list[EvalCase] = Field(min_length=1)

    @classmethod
    def compute_content_hash(cls, value: "EvalSet | Mapping[str, Any]") -> str:
        if isinstance(value, cls):
            normalized = value.model_dump(mode="json", exclude={"content_hash"})
        else:
            data = dict(value)
            cases = [
                (
                    item
                    if isinstance(item, EvalCase)
                    else EvalCase.from_mapping(item)
                ).model_dump(mode="json")
                for item in data.get("cases", [])
            ]
            normalized = {
                "eval_set_id": data.get("eval_set_id"),
                "role_code": data.get("role_code"),
                "version": data.get("version"),
                "cases": cases,
            }
        return sha256_hash(normalized)

    @property
    def canonical_content_hash(self) -> str:
        """Hash the normalized set, excluding the caller-supplied hash assertion."""
        return self.compute_content_hash(self)
    @model_validator(mode="after")
    def canonical_hash_matches(self) -> "EvalSet":
        if self.content_hash != self.canonical_content_hash:
            raise ValueError("content_hash does not match canonical eval-set content")
        return self

    @property
    def identity(self) -> tuple[str, int, str]:
        return (self.eval_set_id, self.version, self.canonical_content_hash)


@dataclass(frozen=True)
class CandidateSpec:
    """Audit-owned identity plus an injected candidate executor."""

    candidate_id: str
    profile_version: str
    model_version: str
    adapter_version: str
    runner: Any


EvalCandidate = CandidateSpec


class CandidateOutput(EvalModel):
    """Small, model-independent candidate result envelope."""

    status: str | None = None
    output: Any = None
    decision: str | None = None
    correct: bool | None = None
    claims: list[Mapping[str, Any] | str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    tool_calls: list[str | Mapping[str, Any]] = Field(default_factory=list)
    risk_compliant: bool | None = None
    hallucination_score: float | None = Field(default=None, ge=0, le=1)
    tool_compliance: float | None = Field(default=None, ge=0, le=1)
    citation_precision: float | None = Field(default=None, ge=0, le=1)
    latency_ms: float | None = Field(default=None, ge=0)
    error_code: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class EvalResult(EvalModel):
    eval_result_id: str
    eval_run_id: str
    case_key: str
    metric: EvalMetric
    score: float | None = None
    passed: bool
    evidence: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    created_at: datetime

    @staticmethod
    def score_is_valid(metric: EvalMetric, score: float | None) -> bool:
        if score is None:
            return False
        if metric is EvalMetric.LATENCY_MS:
            return score >= 0
        return 0 <= score <= 1
    @model_validator(mode="after")
    def bounded_score(self) -> "EvalResult":
        if self.score is not None and not self.score_is_valid(self.metric, self.score):
            raise ValueError(f"score out of range for {self.metric.value}")
        return self


class EvalRun(EvalModel):
    eval_run_id: str
    eval_set_id: str
    eval_set_version: int
    eval_set_hash: str
    candidate_id: str
    candidate_profile_version: str
    champion_ref: dict[str, Any] | None = None
    config: dict[str, Any]
    status: str
    trace_id: str
    environment: str
    mock_tool_manifest: dict[str, Any]
    model_version: str
    adapter_version: str
    evidence_hash: str
    started_at: datetime
    ended_at: datetime | None = None
    created_at: datetime


class ChampionComparison(EvalModel):
    status: str
    error_code: str | None = None
    candidate_run_id: str
    champion_run_id: str | None = None
    metrics: dict[str, dict[str, float | None]] = Field(default_factory=dict)
class EvaluationReport(EvalModel):
    candidate_run: EvalRun
    comparison: ChampionComparison | None = None
    results: list[EvalResult] = Field(default_factory=list)


@runtime_checkable
class CandidateRunner(Protocol):
    def run(self, case: CandidateCase, *, tools: "MockToolRegistry", memory: "ShadowMemory") -> Any: ...


@runtime_checkable
class EvalAuditRepository(Protocol):
    def ensure_eval_set(self, eval_set: EvalSet) -> None: ...

    def append_run(self, run: EvalRun) -> None: ...
    def results_for_run(self, run_id: str) -> Sequence[EvalResult]: ...
    def append_result(self, result: EvalResult) -> None: ...
    def transition_run(
        self, run_id: str, status: str, *, ended_at: datetime | None = None
    ) -> EvalRun: ...
    def run_for_id(self, run_id: str) -> EvalRun | None: ...
    def get_run(self, run_id: str) -> EvalRun | None: ...
    def append_comparison(self, comparison: "ChampionComparison") -> None: ...

    def comparison_for_run(self, run_id: str) -> "ChampionComparison | None": ...


class ShadowMemory:
    """Per-run, per-candidate namespace; never shared with production memory."""

    def __init__(self, namespace: str) -> None:
        self.namespace = namespace
        self._values: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def put(self, key: str, value: Any) -> None:
        self._values[key] = copy.deepcopy(value)
    def fork(self) -> "ShadowMemory":
        """Return an isolated snapshot for one case invocation."""
        child = ShadowMemory(self.namespace)
        child._values = copy.deepcopy(self._values)
        return child

    def merge_from(self, child: "ShadowMemory") -> None:
        """Commit a completed case snapshot without sharing mutable state."""
        self._values = copy.deepcopy(child._values)


class MockToolRegistry:
    """Only deterministic fixtures are callable by a Shadow candidate."""

    def __init__(
        self,
        fixtures: Mapping[str, Any] | None = None,
        *,
        manifest_version: str = "mock-tools-v1",
        allowed_tools: Sequence[str] | None = None,
    ) -> None:
        self.fixtures = dict(fixtures or {})
        self.manifest_version = manifest_version
        self.allowed_tools = set(allowed_tools) if allowed_tools is not None else set(self.fixtures)
        self.calls: list[dict[str, Any]] = []

    @property
    def manifest(self) -> dict[str, Any]:
        return {
            "version": self.manifest_version,
            "tools": sorted(self.fixtures),
            "allowed_tools": sorted(self.allowed_tools & self.fixtures.keys()),
            "fixture_hash": sha256_hash(self.fixtures),
        }

    def call(self, tool: str, arguments: Mapping[str, Any] | None = None) -> Any:
        if tool not in self.allowed_tools or tool not in self.fixtures:
            raise PermissionError(EvalErrorCode.TOOLCALL_DENIED.value)
        self.calls.append({"tool": tool, "arguments": dict(arguments or {})})
        fixture = self.fixtures[tool]
        return fixture(dict(arguments or {})) if callable(fixture) else copy.deepcopy(fixture)

    # Candidate-friendly alias.
    execute = call


class InMemoryEvalAuditRepository:
    """Append-only repository for focused tests and local Shadow runs."""

    def __init__(self) -> None:
        self.eval_sets: dict[str, EvalSet] = {}
        self.runs: list[EvalRun] = []
        self.results: list[EvalResult] = []
        self.comparisons: dict[str, ChampionComparison] = {}

    def ensure_eval_set(self, eval_set: EvalSet) -> None:
        existing = self.eval_sets.get(eval_set.eval_set_id)
        if existing is not None and existing.identity != eval_set.identity:
            raise AppendOnlyViolation("eval set identity conflict")
        for existing in self.eval_sets.values():
            if existing.identity == eval_set.identity:
                continue
            if (
                existing.role_code == eval_set.role_code
                and existing.version == eval_set.version
            ) or (
                existing.role_code == eval_set.role_code
                and existing.content_hash == eval_set.content_hash
            ):
                raise AppendOnlyViolation("eval set identity conflict")
        self.eval_sets[eval_set.eval_set_id] = eval_set.model_copy(deep=True)

    def append_run(self, run: EvalRun) -> None:
        existing = next((item for item in self.runs if item.eval_run_id == run.eval_run_id), None)
        if existing is not None:
            if existing.model_dump(mode="json") == run.model_dump(mode="json"):
                return  # idempotent replay of the same append
            raise AppendOnlyViolation("eval run id conflict")
        self.runs.append(run.model_copy(deep=True))

    def append_result(self, result: EvalResult) -> None:
        for item in self.results:
            if item.eval_result_id == result.eval_result_id:
                if item.model_dump() == result.model_dump():
                    return  # idempotent replay of the same append
                raise AppendOnlyViolation("eval result id conflict")
            if (item.eval_run_id, item.case_key, item.metric) == (result.eval_run_id, result.case_key, result.metric):
                raise AppendOnlyViolation("duplicate eval result key")
        self.results.append(result.model_copy(deep=True))

    def transition_run(self, run_id: str, status: str, *, ended_at: datetime | None = None) -> EvalRun:
        run = next((item for item in self.runs if item.eval_run_id == run_id), None)
        if run is None:
            raise KeyError(run_id)
        allowed = {
            "QUEUED": {"RUNNING", "CANCELLED"},
            "RUNNING": {"COMPLETED", "FAILED", "CANCELLED"},
            "COMPLETED": set(),
            "FAILED": set(),
            "CANCELLED": set(),
        }
        if status not in allowed.get(run.status, set()):
            raise AppendOnlyViolation(f"invalid eval run transition {run.status}->{status}")
        if status == "RUNNING" and (run.started_at is None or ended_at is not None):
            raise AppendOnlyViolation("RUNNING eval run requires started_at and no ended_at")
        if status in {"COMPLETED", "FAILED", "CANCELLED"} and ended_at is None:
            raise AppendOnlyViolation("terminal eval run transition requires ended_at")
        if ended_at is not None and ended_at < run.started_at:
            raise AppendOnlyViolation("eval run ended_at cannot precede started_at")
        run.status = status
        if ended_at is not None:
            run.ended_at = ended_at
        return run.model_copy(deep=True)
    def results_for_run(self, run_id: str) -> list[EvalResult]:
        return [
            item.model_copy(deep=True)
            for item in self.results
            if item.eval_run_id == run_id
        ]

    def run_for_id(self, run_id: str) -> EvalRun | None:
        run = next((item for item in self.runs if item.eval_run_id == run_id), None)
        return None if run is None else run.model_copy(deep=True)
    get_run = run_for_id
    def append_comparison(self, comparison: ChampionComparison) -> None:
        existing = self.comparisons.get(comparison.candidate_run_id)
        if existing is not None:
            if existing.model_dump(mode="json") == comparison.model_dump(mode="json"):
                return
            raise AppendOnlyViolation("eval comparison id conflict")
        self.comparisons[comparison.candidate_run_id] = comparison.model_copy(deep=True)

    def comparison_for_run(self, run_id: str) -> ChampionComparison | None:
        comparison = self.comparisons.get(run_id)
        return None if comparison is None else comparison.model_copy(deep=True)


    def update_result(self, *_: Any, **__: Any) -> None:
        raise AppendOnlyViolation("eval results are append-only")

    def delete_result(self, *_: Any, **__: Any) -> None:
        raise AppendOnlyViolation("eval results are append-only")

    def delete_run(self, run_id: str) -> None:
        # Parent deletion cannot cascade into results.
        if any(item.eval_run_id == run_id for item in self.results):
            raise AppendOnlyViolation("cannot delete an eval run with results")
        raise AppendOnlyViolation("eval runs are append-only")


@dataclass(frozen=True)
class _CaseEvaluation:
    output: CandidateOutput | None
    error_code: str | None
    error_detail: str | None
    latency_ms: float
    tool_calls: tuple[dict[str, Any], ...] = ()


class EvalRunner:
    """Run only injected QA candidates against deterministic Mock tools."""

    def __init__(
        self,
        *,
        candidate_runner: Any | None = None,
        repository: EvalAuditRepository | None = None,
        clock: Callable[[], datetime] = utc_now,
        environment: str = "SHADOW",
        mock_tools: MockToolRegistry | None = None,
        metric_thresholds: Mapping[str, float] | None = None,
        timeout_ms: int = 120000,
    ) -> None:
        if environment.upper() not in {"SHADOW", "MOCK"}:
            raise ValueError(EvalErrorCode.UNSUPPORTED_ENVIRONMENT.value)
        if timeout_ms < 1:
            raise ValueError("timeout_ms must be positive")
        self.candidate_runner = candidate_runner
        self.repository = repository or InMemoryEvalAuditRepository()
        self.clock = clock
        self.environment = environment.upper()
        self.timeout_ms = timeout_ms
        self.mock_tools = mock_tools or MockToolRegistry()
        self.metric_thresholds = {
            EvalMetric.ACCURACY.value: 0.8,
            EvalMetric.HALLUCINATION_SCORE.value: 0.8,
            EvalMetric.TOOL_COMPLIANCE.value: 1.0,
            EvalMetric.CITATION_PRECISION.value: 0.8,
            EvalMetric.RISK_COMPLIANCE.value: 1.0,
            EvalMetric.LATENCY_MS.value: 1000.0,
            **dict(metric_thresholds or {}),
        }
    def _new_tools(self) -> MockToolRegistry:
        """Create a fresh fixture registry for one candidate run."""
        return MockToolRegistry(
            copy.deepcopy(self.mock_tools.fixtures),
            manifest_version=self.mock_tools.manifest_version,
            allowed_tools=set(self.mock_tools.allowed_tools),
        )

    def run(
        self,
        eval_set: EvalSet | Mapping[str, Any],
        candidate: CandidateSpec | Any | None = None,
        *,
        champion: CandidateSpec | Any | None = None,
        champion_eval_set: EvalSet | Mapping[str, Any] | None = None,
        trace_id: str | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> EvalRun:
        try:
            eval_model = eval_set if isinstance(eval_set, EvalSet) else EvalSet.model_validate(eval_set)
            eval_model = eval_model.model_copy(deep=True)
        except ValidationError as exc:
            raise ValueError(f"{EvalErrorCode.SCHEMA_FAILURE.value}: {exc}") from exc
        candidate_spec = self._candidate_spec(candidate or self.candidate_runner, role="candidate")
        if champion is not None and champion_eval_set is not None:
            other_set = champion_eval_set if isinstance(champion_eval_set, EvalSet) else EvalSet.model_validate(champion_eval_set)
            if eval_model.identity != other_set.identity:
                raise EvalSetMismatchError(EvalErrorCode.EVAL_SET_MISMATCH.value)
        if candidate_spec is None:
            raise ValueError(f"{EvalErrorCode.CANDIDATE_FAILURE.value}: candidate runner is required")

        run_id = str(uuid4())
        now = self.clock()
        run_tools = self._new_tools()
        run = EvalRun(
            eval_run_id=run_id,
            eval_set_id=eval_model.eval_set_id,
            eval_set_version=eval_model.version,
            eval_set_hash=eval_model.canonical_content_hash,
            candidate_id=candidate_spec.candidate_id,
            candidate_profile_version=candidate_spec.profile_version,
            champion_ref=self._champion_ref(champion),
            config={
                **dict(config or {}),
                "metric_version": METRIC_VERSION,
                "environment": self.environment,
                "timeout_ms": self.timeout_ms,
                "metric_thresholds": dict(self.metric_thresholds),
                "mock_memory_namespace": f"qa-eval:{run_id}:{candidate_spec.candidate_id}",
            },
            status="QUEUED",
            trace_id=trace_id or str(uuid4()),
            environment=self.environment,
            mock_tool_manifest=run_tools.manifest,
            model_version=candidate_spec.model_version,
            adapter_version=candidate_spec.adapter_version,
            evidence_hash=sha256_hash({"eval_set": eval_model.canonical_content_hash, "cases": [c.case_key for c in eval_model.cases]}),
            started_at=now,
            created_at=now,
        )
        ensure_eval_set = getattr(self.repository, "ensure_eval_set", None)
        if ensure_eval_set is not None:
            ensure_eval_set(eval_model)
        self.repository.append_run(run)
        self.repository.transition_run(run_id, "RUNNING")

        run_failed = False
        memory = ShadowMemory(f"qa-eval:{run_id}:{candidate_spec.candidate_id}")
        final_run = run.model_copy(deep=True, update={"status": "RUNNING"})
        try:
            for source_case in eval_model.cases:
                # A timed-out injected runner can continue in its daemon thread;
                # isolate both its input and memory snapshot from later cases.
                case = source_case.model_copy(deep=True)
                case_tools = self._new_tools()
                case_memory = memory.fork()
                evaluation = self._run_case(candidate_spec.runner, case, case_memory, case_tools)
                if evaluation.error_code is None:
                    memory.merge_from(case_memory)
                for result in self._metric_results(run, eval_model, case, evaluation):
                    self.repository.append_result(result)
                if evaluation.error_code is not None:
                    run_failed = True
        except Exception:
            # The repository receives an explicit FAILED terminal status even
            # when a repository/candidate integration raises unexpectedly.
            run_failed = True
            raise
        finally:
            ended_at = self.clock()
            self.repository.transition_run(
                run_id,
                "FAILED" if run_failed else "COMPLETED",
                ended_at=ended_at,
            )
            final_run = run.model_copy(
                update={
                    "status": "FAILED" if run_failed else "COMPLETED",
                    "ended_at": ended_at,
                }
            )

        return final_run
    def evaluate(
        self,
        eval_set: EvalSet | Mapping[str, Any],
        candidate: CandidateSpec | Any | None = None,
        *,
        champion: CandidateSpec | Any | None = None,
        champion_eval_set: EvalSet | Mapping[str, Any] | None = None,
        trace_id: str | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> EvaluationReport:
        run = self.run(
            eval_set,
            candidate,
            champion=champion,
            champion_eval_set=champion_eval_set,
            trace_id=trace_id,
            config=config,
        )
        comparison = self.compare_champion(
            run,
            eval_set,
            champion,
            champion_eval_set=champion_eval_set,
        )
        append_comparison = getattr(self.repository, "append_comparison", None)
        if append_comparison is not None:
            append_comparison(comparison)
        return EvaluationReport(
            candidate_run=run,
            comparison=comparison,
            results=self._rows_for_run(run.eval_run_id),
        )

    def compare_champion(
        self,
        candidate_run: EvalRun,
        eval_set: EvalSet | Mapping[str, Any],
        champion: CandidateSpec | Any | None,
        *,
        champion_eval_set: EvalSet | Mapping[str, Any] | None = None,
    ) -> ChampionComparison:
        """Compare only an identical eval-set id/version/content hash."""
        current = eval_set if isinstance(eval_set, EvalSet) else EvalSet.model_validate(eval_set)
        champion_set = (
            current
            if champion_eval_set is None
            else champion_eval_set
            if isinstance(champion_eval_set, EvalSet)
            else EvalSet.model_validate(champion_eval_set)
        )
        if not self._run_matches_eval_set(candidate_run, current):
            return ChampionComparison(
                status="NOT_EXECUTED",
                error_code=EvalErrorCode.EVAL_SET_MISMATCH.value,
                candidate_run_id=candidate_run.eval_run_id,
            )
        if str(candidate_run.status).upper() != "COMPLETED":
            # A failed/partial candidate has no trustworthy metrics.  Never
            # execute or persist a champion comparison for it.
            return ChampionComparison(
                status="NOT_EXECUTED",
                error_code=EvalErrorCode.CANDIDATE_FAILURE.value,
                candidate_run_id=candidate_run.eval_run_id,
            )
        if champion is None:
            return ChampionComparison(
                status="NOT_EXECUTED",
                candidate_run_id=candidate_run.eval_run_id,
            )
        if current.identity != champion_set.identity:
            return ChampionComparison(
                status="NOT_EXECUTED",
                error_code=EvalErrorCode.EVAL_SET_MISMATCH.value,
                candidate_run_id=candidate_run.eval_run_id,
            )
        champion_spec = self._candidate_spec(champion, role="champion")
        if champion_spec is None:
            return ChampionComparison(status="NOT_EXECUTED", candidate_run_id=candidate_run.eval_run_id)
        champion_run = self.run(champion_set, champion_spec)
        if not self._run_matches_eval_set(champion_run, champion_set):
            return ChampionComparison(
                status="NOT_EXECUTED",
                error_code=EvalErrorCode.EVAL_SET_MISMATCH.value,
                candidate_run_id=candidate_run.eval_run_id,
                champion_run_id=champion_run.eval_run_id,
            )
        if str(champion_run.status).upper() != "COMPLETED":
            return ChampionComparison(
                status="NOT_EXECUTED",
                error_code=EvalErrorCode.CANDIDATE_FAILURE.value,
                candidate_run_id=candidate_run.eval_run_id,
                champion_run_id=champion_run.eval_run_id,
            )
        candidate_rows = self._rows_for_run(candidate_run.eval_run_id)
        champion_rows = self._rows_for_run(champion_run.eval_run_id)
        metrics: dict[str, dict[str, float | None]] = {}
        for metric in METRICS:
            c = [row.score for row in candidate_rows if row.metric is metric and row.score is not None]
            h = [row.score for row in champion_rows if row.metric is metric and row.score is not None]
            metrics[metric.value] = {
                "candidate": round(sum(c) / len(c), 6) if c else None,
                "champion": round(sum(h) / len(h), 6) if h else None,
            }
        return ChampionComparison(
            status="COMPARED",
            candidate_run_id=candidate_run.eval_run_id,
            champion_run_id=champion_run.eval_run_id,
            metrics=metrics,
        )

    def _run_case(
        self,
        runner: Any,
        case: EvalCase,
        memory: ShadowMemory,
        tools: MockToolRegistry,
    ) -> _CaseEvaluation:
        started = self.clock()
        runner_case = CandidateCase.from_eval_case(case)
        def observed_calls() -> tuple[dict[str, Any], ...]:
            return tuple(copy.deepcopy(tools.calls))
        try:
            raw = _call_with_timeout(
                lambda: self._invoke_runner(runner, runner_case, tools, memory),
                self.timeout_ms,
            )
            elapsed = self._elapsed_ms(started)
            if elapsed > self.timeout_ms:
                raise TimeoutError(f"candidate exceeded timeout ({elapsed}ms > {self.timeout_ms}ms)")
            output = raw if isinstance(raw, CandidateOutput) else CandidateOutput.model_validate(raw)
            candidate_status = str(output.status or "COMPLETED").upper()
            if (
                candidate_status in {"COMPLETED", "PASS", "PASSED"}
                and output.output is None
                and output.decision is None
                and output.correct is None
                and not output.claims
                and not output.citations
            ):
                return _CaseEvaluation(
                    None,
                    EvalErrorCode.SCHEMA_FAILURE.value,
                    "completed candidate output is empty",
                    round(float(elapsed), 3),
                    observed_calls(),
                )
            error_code = output.error_code
            known_statuses = {
                "COMPLETED",
                "PASS",
                "PASSED",
                "FAILED",
                "FAIL",
                "ERROR",
                "CRASHED",
                "TIMEOUT",
                "OOM",
                "SCHEMA_FAILURE",
                "TOOLCALL_DENIED",
                "DEGRADED",
                "REJECTED",
                "ESCALATED",
                "HOLD",
            }
            if candidate_status not in known_statuses:
                return _CaseEvaluation(
                    None,
                    EvalErrorCode.SCHEMA_FAILURE.value,
                    f"unknown candidate status: {candidate_status or '<empty>'}",
                    round(float(elapsed), 3),
                    observed_calls(),
                )
            if candidate_status in {
                "FAILED",
                "FAIL",
                "ERROR",
                "CRASHED",
                "TIMEOUT",
                "OOM",
                "SCHEMA_FAILURE",
                "TOOLCALL_DENIED",
                "DEGRADED",
                "REJECTED",
                "ESCALATED",
                "HOLD",
            }:
                if error_code not in {code.value for code in EvalErrorCode}:
                    error_code = (
                        EvalErrorCode.TIMEOUT.value
                        if candidate_status == "TIMEOUT"
                        else (
                            candidate_status
                            if candidate_status in {code.value for code in EvalErrorCode}
                            else EvalErrorCode.CANDIDATE_FAILURE.value
                        )
                    )
            return _CaseEvaluation(
                output=output,
                error_code=error_code,
                error_detail=None,
                # Latency is runner-owned; candidate-provided values are ignored.
                latency_ms=round(float(elapsed), 3),
                tool_calls=observed_calls(),
            )
        except TimeoutError as exc:
            return _CaseEvaluation(
                None,
                EvalErrorCode.TIMEOUT.value,
                str(exc),
                min(float(self._elapsed_ms(started)), float(self.timeout_ms)),
                observed_calls(),
            )
        except MemoryError as exc:
            elapsed = self._elapsed_ms(started)
            if elapsed > self.timeout_ms:
                return _CaseEvaluation(None, EvalErrorCode.TIMEOUT.value, "candidate exceeded timeout", float(self.timeout_ms), observed_calls())
            return _CaseEvaluation(None, EvalErrorCode.OOM.value, str(exc), float(elapsed), observed_calls())
        except PermissionError as exc:
            elapsed = self._elapsed_ms(started)
            if elapsed > self.timeout_ms:
                return _CaseEvaluation(None, EvalErrorCode.TIMEOUT.value, "candidate exceeded timeout", float(self.timeout_ms), observed_calls())
            return _CaseEvaluation(None, EvalErrorCode.TOOLCALL_DENIED.value, str(exc), float(elapsed), observed_calls())
        except ValidationError as exc:
            elapsed = self._elapsed_ms(started)
            if elapsed > self.timeout_ms:
                return _CaseEvaluation(None, EvalErrorCode.TIMEOUT.value, "candidate exceeded timeout", float(self.timeout_ms), observed_calls())
            return _CaseEvaluation(None, EvalErrorCode.SCHEMA_FAILURE.value, str(exc), float(elapsed), observed_calls())
        except Exception as exc:
            elapsed = self._elapsed_ms(started)
            if elapsed > self.timeout_ms:
                return _CaseEvaluation(None, EvalErrorCode.TIMEOUT.value, "candidate exceeded timeout", float(self.timeout_ms), observed_calls())
            code = self._exception_code(exc)
            return _CaseEvaluation(None, code, str(exc) or "candidate failed", float(elapsed), observed_calls())
    @staticmethod
    def _exception_code(exc: Exception) -> str:
        message = str(exc).upper()
        for code in EvalErrorCode:
            if code.value in message:
                return code.value
        return EvalErrorCode.CRASHED.value

    def _metric_results(self, run: EvalRun, eval_set: EvalSet, case: EvalCase, evaluation: _CaseEvaluation) -> list[EvalResult]:
        now = self.clock()
        values: dict[EvalMetric, float | None]
        evidence: dict[str, Any] = {
            "metric_version": METRIC_VERSION,
            "trace_id": run.trace_id,
            "candidate_profile_version": run.candidate_profile_version,
            "environment": run.environment,
            "mock_tool_manifest": run.mock_tool_manifest,
            "model_version": run.model_version,
            "adapter_version": run.adapter_version,
            "eval_set_id": eval_set.eval_set_id,
            "eval_set_version": eval_set.version,
            "eval_set_hash": eval_set.canonical_content_hash,
        }
        if evaluation.error_code is not None or evaluation.output is None:
            values = {metric: None for metric in METRICS}
            evidence["error_detail"] = evaluation.error_detail
        else:
            output = evaluation.output
            values = {
                EvalMetric.ACCURACY: self._accuracy(case, output),
                EvalMetric.HALLUCINATION_SCORE: self._hallucination_score(output),
                EvalMetric.TOOL_COMPLIANCE: self._tool_compliance(case, output, evaluation.tool_calls),
                EvalMetric.CITATION_PRECISION: self._citation_precision(case, output),
                EvalMetric.LATENCY_MS: evaluation.latency_ms,
                EvalMetric.RISK_COMPLIANCE: self._risk_compliance(case, output),
            }
            evidence.update(
                {
                    key: value
                    for key, value in output.evidence.items()
                    if key not in {
                        "metric_version",
                        "trace_id",
                        "candidate_profile_version",
                        "environment",
                        "mock_tool_manifest",
                        "model_version",
                        "adapter_version",
                        "eval_set_id",
                        "eval_set_version",
                        "eval_set_hash",
                        "output_hash",
                        "case_input_hash",
                        "latency_ms",
                        "error_code",
                        "status",
                    }
                }
            )
            evidence["output_hash"] = sha256_hash(output)
        rows: list[EvalResult] = []
        for metric in METRICS:
            score = values[metric]
            passed = evaluation.error_code is None and self._metric_passes(metric, score)
            rows.append(
                EvalResult(
                    eval_result_id=str(uuid4()),
                    eval_run_id=run.eval_run_id,
                    case_key=case.case_key,
                    metric=metric,
                    score=None if score is None else round(float(score), 6),
                    passed=passed,
                    evidence={**evidence, "case_input_hash": sha256_hash(case.input_payload)},
                    error_code=evaluation.error_code,
                    created_at=now,
                )
            )
        return rows

    def _accuracy(self, case: EvalCase, output: CandidateOutput) -> float:
        expected = case.expected_output if case.expected_output is not None else case.expected
        if case.expected_decision is not None:
            return 1.0 if output.decision == case.expected_decision else 0.0
        return 1.0 if output.output == expected else 0.0

    def _hallucination_score(self, output: CandidateOutput) -> float:
        if not output.claims:
            return 1.0
        unsupported = 0
        for claim in output.claims:
            if isinstance(claim, Mapping) and (claim.get("supported") is False or claim.get("unsupported") is True):
                unsupported += 1
            elif isinstance(claim, str) and claim.startswith("UNSUPPORTED:"):
                unsupported += 1
        return max(0.0, round(1 - unsupported / len(output.claims), 6))

    def _tool_compliance(
        self,
        case: EvalCase,
        output: CandidateOutput,
        observed_calls: tuple[dict[str, Any], ...],
    ) -> float:
        calls = observed_calls
        if not calls:
            return 1.0 if not output.tool_calls else 0.0
        allowed = set(case.allowed_tools)
        if not allowed:
            return 0.0
        compliant = 0
        for call in calls:
            compliant += str(call.get("tool", "")) in allowed
        return round(compliant / len(calls), 6)

    def _citation_precision(self, case: EvalCase, output: CandidateOutput) -> float:
        citations = set(output.citations)
        expected = set(case.expected_citations)
        if not citations:
            return 1.0 if not expected else 0.0
        return round(len(citations & expected) / len(citations), 6) if expected else 0.0

    def _risk_compliance(self, case: EvalCase, output: CandidateOutput) -> float:
        if output.risk_compliant is None:
            return 0.0
        return 1.0 if output.risk_compliant == case.expected_risk_compliance else 0.0

    def _metric_passes(self, metric: EvalMetric, score: float | None) -> bool:
        if score is None:
            return False
        threshold = float(self.metric_thresholds[metric.value])
        return score <= threshold if metric is EvalMetric.LATENCY_MS else score >= threshold

    def _invoke_runner(self, runner: Any, case: CandidateCase, tools: MockToolRegistry, memory: ShadowMemory) -> Any:
        method = getattr(runner, "run", runner if callable(runner) else None)
        if method is None:
            raise TypeError("candidate runner must be callable or implement run")
        # Avoid retrying a candidate after a TypeError (which could duplicate a
        # side effect).  Select its supported signature before calling it.
        try:
            signature = inspect.signature(method)
            names = set(signature.parameters)
        except (TypeError, ValueError):
            names = {"case", "tools", "memory"}
        kwargs: dict[str, Any] = {}
        if "tools" in names:
            kwargs["tools"] = tools
        elif "tool_registry" in names:
            kwargs["tool_registry"] = tools
        if "memory" in names:
            kwargs["memory"] = memory
        elif "memory_namespace" in names:
            kwargs["memory_namespace"] = memory.namespace
        return method(case, **kwargs)

    @staticmethod
    def _candidate_spec(value: Any, *, role: str) -> CandidateSpec | None:
        if value is None:
            return None
        if isinstance(value, CandidateSpec):
            return CandidateSpec(
                candidate_id=EvalRunner._scoped_candidate_id(value.candidate_id, role=role),
                profile_version=value.profile_version,
                model_version=value.model_version,
                adapter_version=value.adapter_version,
                runner=value.runner,
            )
        if isinstance(value, Mapping):
            data = dict(value)
            runner = data.pop("runner", None)
            if runner is not None:
                return CandidateSpec(
                    candidate_id=EvalRunner._scoped_candidate_id(
                        str(data.get("candidate_id", uuid4().hex)), role=role
                    ),
                    profile_version=str(data.get("profile_version", "unknown")),
                    model_version=str(data.get("model_version", "unknown")),
                    adapter_version=str(data.get("adapter_version", "none")),
                    runner=runner,
                )
        return CandidateSpec(
            candidate_id=EvalRunner._scoped_candidate_id(
                str(getattr(value, "candidate_id", uuid4().hex)), role=role
            ),
            profile_version=str(getattr(value, "profile_version", "unknown")),
            model_version=str(getattr(value, "model_version", "deterministic:unknown")),
            adapter_version=str(getattr(value, "adapter_version", "none")),
            runner=value,
        )

    @staticmethod
    def _scoped_candidate_id(candidate_id: str, *, role: str) -> str:
        """Keep persisted candidate identities inside the QA namespace."""
        value = str(candidate_id)
        if value.startswith("qa:"):
            return value
        return f"qa:{role}:{value}"

    @staticmethod
    def _run_matches_eval_set(run: EvalRun, eval_set: EvalSet) -> bool:
        """Ensure persisted run identity is the set being compared."""
        return (
            str(run.eval_set_id) == str(eval_set.eval_set_id)
            and int(run.eval_set_version) == int(eval_set.version)
            and str(run.eval_set_hash) == str(eval_set.canonical_content_hash)
        )
    @staticmethod
    def _champion_ref(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        spec = EvalRunner._candidate_spec(value, role="champion")
        if spec is None:
            return None
        return {
            "candidate_id": spec.candidate_id,
            "profile_version": spec.profile_version,
            "model_version": spec.model_version,
            "adapter_version": spec.adapter_version,
        }

    def _rows_for_run(self, run_id: str) -> list[EvalResult]:
        getter = getattr(self.repository, "results_for_run", None)
        if getter is not None:
            return list(getter(run_id))
        return [item for item in getattr(self.repository, "results", ()) if item.eval_run_id == run_id]

    def _elapsed_ms(self, started: datetime) -> int:
        ended = self.clock()
        if isinstance(started, (int, float)) and isinstance(ended, (int, float)):
            return max(0, int((ended - started) * 1000))
        if not isinstance(started, datetime) or not isinstance(ended, datetime):
            return 0
        return max(0, int((ended - started).total_seconds() * 1000))


__all__ = [
    "AppendOnlyViolation",
    "CandidateOutput",
    "CandidateRunner",
    "CandidateSpec",
    "EvalAuditRepository",
    "EvalCase",
    "EvalCandidate",
    "EvaluationReport",
    "EvaluationMetric",
    "EvalErrorCode",
    "EvalMetric",
    "EvalResult",
    "EvalRun",
    "EvalRunner",
    "EvalSet",
    "EvalSetMismatchError",
    "InMemoryEvalAuditRepository",
    "METRIC_VERSION",
    "METRICS",
    "MockToolRegistry",
    "ChampionComparison",
    "ShadowMemory",
]
