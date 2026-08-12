"""Strategy-bound temporary workers for the Trading department.

Bull/Bear employees are intentionally absent. Quant supplies immutable strategy
bundles; Trading validates each bundle and creates exactly one temporary worker
for each accepted strategy version. Workers only execute strategy logic and
paper orders. Deterministic orchestration owns comparison and selection.
"""
from __future__ import annotations
import hashlib
import json

from dataclasses import dataclass, field
from decimal import Decimal
from decimal import InvalidOperation
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping


class StrategyBundleRejected(ValueError):
    """Raised before worker creation when a Quant bundle is not admissible."""


_REQUIRED_FIELDS = (
    "strategy_id",
    "strategy_version",
    "capital_allocation",
    "performance_weights",
)
_REQUIRED_WEIGHTS = ("return", "drawdown", "cost", "execution")

# Fixed LLM registry is intentionally empty; strategy workers are request-scoped.
WORKER_SPECS: tuple[Any, ...] = ()

def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def validate_strategy_bundle(bundle: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a normalized immutable copy or reject before Worker construction."""
    if not isinstance(bundle, Mapping):
        raise StrategyBundleRejected("bundle_must_be_mapping")
    missing = [name for name in _REQUIRED_FIELDS if bundle.get(name) in (None, "", {})]
    if missing:
        raise StrategyBundleRejected(f"missing_required_fields:{','.join(missing)}")

    weights = bundle["performance_weights"]
    if not isinstance(weights, Mapping):
        raise StrategyBundleRejected("performance_weights_must_be_mapping")
    missing_weights = [name for name in _REQUIRED_WEIGHTS if name not in weights]
    if missing_weights:
        raise StrategyBundleRejected(f"missing_performance_weights:{','.join(missing_weights)}")
    try:
        normalized_weights = {name: Decimal(str(weights[name])) for name in _REQUIRED_WEIGHTS}
        allocation = Decimal(str(bundle["capital_allocation"]))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise StrategyBundleRejected("bundle_numeric_value_invalid") from exc
    if any(not value.is_finite() or value < 0 for value in normalized_weights.values()):
        raise StrategyBundleRejected("performance_weights_must_be_finite_non_negative")
    if sum(normalized_weights.values()) <= 0:
        raise StrategyBundleRejected("performance_weights_sum_must_be_positive")
    if not allocation.is_finite() or allocation <= 0:
        raise StrategyBundleRejected("capital_allocation_must_be_finite_positive")

    normalized = dict(bundle)
    normalized["strategy_id"] = str(bundle["strategy_id"])
    normalized["strategy_version"] = str(bundle["strategy_version"])
    normalized["capital_allocation"] = allocation
    normalized["performance_weights"] = normalized_weights
    return _freeze(normalized)


@dataclass(frozen=True)
class StrategyWorkerSpec:
    worker_id: str
    strategy_id: str
    strategy_version: str
    output_contract: str = "trading.temporary-strategy-worker.v1"
    llm: bool = False
    temporary: bool = True


@dataclass(frozen=True)
class TemporaryStrategyWorker:
    spec: StrategyWorkerSpec
    bundle: Mapping[str, Any]
    executor: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]
    audit: list[dict[str, Any]] = field(default_factory=list)

    def run_event(self, event: Mapping[str, Any], *, sequence: int) -> dict[str, Any]:
        frozen_event = _freeze(event)
        output = dict(self.executor(self.bundle, frozen_event))
        if "target_weight" not in output:
            raise StrategyBundleRejected("strategy_output_missing_target_weight")
        return {
            "sequence": sequence,
            "strategy_id": self.spec.strategy_id,
            "strategy_version": self.spec.strategy_version,
            "as_of": frozen_event.get("as_of"),
            "instrument_id": frozen_event.get("instrument_id"),
            "price": frozen_event.get("price"),
            "target_weight": Decimal(str(output["target_weight"])),
        }

    def run(self, market_events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Execute immutable strategy logic; never select, promote, or submit live orders."""
        signals = [
            self.run_event(event, sequence=sequence)
            for sequence, event in enumerate(market_events)
        ]
        self.audit.append({"event": "paper_signals_generated", "count": len(signals)})
        return signals


def create_temporary_worker(
    bundle: Mapping[str, Any],
    *,
    executor: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]],
) -> TemporaryStrategyWorker:
    normalized = validate_strategy_bundle(bundle)
    strategy_id = normalized["strategy_id"]
    version = normalized["strategy_version"]
    spec = StrategyWorkerSpec(
        worker_id=f"alpha-{hashlib.sha256(json.dumps([strategy_id, version]).encode()).hexdigest()[:20]}",
        strategy_id=strategy_id,
        strategy_version=version,
    )
    return TemporaryStrategyWorker(spec=spec, bundle=normalized, executor=executor)


# 다른 네 부서(risk/qa/accounting/ceo)는 러너 메타를 이 세 상수로 노출한다.
# 트레이딩만 없어서 러너 인벤토리를 이름으로 셀 때 이 부서가 `None` 으로 빠졌다
# (2026-08-12 실측). 편제 문서가 "결정론 러너 5개"라고 적는 근거를 코드에서
# 확인할 수 있어야 한다.
RUNNER_ID = "desk-runner"
RUNNER_ROLE = "Trading desk runner — 주문 권한 없는 결정론 경계(LLM 없음)"
RUNNER_TOOLS: tuple[str, ...] = ()


def desk_runner(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve the deterministic desk boundary without any LLM employee."""
    return {
        "worker_id": "desk-runner",
        "llm": False,
        "authoritative": False,
        "risk_gate_required": True,
        "broker_submit_allowed": False,
        "payload_keys": sorted(str(key) for key in payload),
    }


def run_employee_workers(
    payload: Mapping[str, Any], *, llm: Any | None = None
) -> dict[str, Any]:
    """Run only the deterministic desk worker through the generic dispatcher."""
    del llm
    report = desk_runner(payload)
    return {
        "department": "trading",
        "status": "COMPLETED",
        "binding": False,
        "degraded": False,
        "workers": [report],
        "executed": ["desk-runner"],
        "failed": [],
        "not_executed": [],
        "llm_worker_count": 0,
        "runtime": {
            "executor": "deterministic_strategy_worker",
            "topology": "dynamic_parallel_fan_out_fan_in",
            "provider": "none",
            "model": "none",
        },
    }


if __name__ == "__main__":
    bundle = {
        "strategy_id": "alpha-1",
        "strategy_version": "v1",
        "capital_allocation": "100000",
        "performance_weights": {"return": 4, "drawdown": 3, "cost": 2, "execution": 1},
    }
    worker = create_temporary_worker(
        bundle,
        executor=lambda _bundle, event: {"target_weight": event["target_weight"]},
    )
    assert worker.spec.llm is False and worker.spec.temporary is True
    assert len(worker.run([{"as_of": "t1", "price": "100", "target_weight": "0.1"}])) == 1
    try:
        create_temporary_worker({}, executor=lambda _bundle, _event: {})
    except StrategyBundleRejected:
        pass
    else:
        raise AssertionError("invalid bundle created a worker")
    print("temporary strategy worker checks passed")
