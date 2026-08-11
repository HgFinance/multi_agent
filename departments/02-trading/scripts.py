#!/usr/bin/env python3
"""Parallel Alpha Strategy paper evaluation and deterministic selection.

Trading consumes Quant-owned strategy bundles. One temporary worker is created
per valid bundle, all workers observe the same materialized market stream, and
paper activity is attributed inside one shared account. No worker can select a
winner, promote itself, bypass Risk, or submit a live broker order.
"""
from __future__ import annotations

import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Iterable, Mapping

_BASE = Path(__file__).resolve().parent
for _p in (_BASE, _BASE / "contracts"):   # contracts/: intent_builder(F11)
    if str(_p) not in sys.path:
        sys.path.append(str(_p))

from employee_workers import (  # noqa: E402
    StrategyBundleRejected,
    TemporaryStrategyWorker,
    create_temporary_worker,
    validate_strategy_bundle,
)

D = Decimal
PIPELINE_VERSION = "trading-alpha-paper-v1"


@dataclass
class AttributedLedger:
    strategy_id: str
    strategy_version: str
    allocation: Decimal
    cash: Decimal
    position: Decimal = D("0")
    average_price: Decimal = D("0")
    realized_pnl: Decimal = D("0")
    costs: Decimal = D("0")
    trades: int = 0
    equity_curve: list[Decimal] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)


class WorkerLifecycleRegistry:
    """Authoritative in-run lifecycle state with validated transitions."""

    def __init__(self) -> None:
        self._states: dict[str, str] = {}
        self._lock = Lock()

    def register_temporary(self, worker_id: str) -> None:
        with self._lock:
            if worker_id in self._states:
                raise ValueError(f"worker_already_registered:{worker_id}")
            self._states[worker_id] = "TEMPORARY"

    def finalize_selection(self, winner_id: str) -> list[dict[str, str]]:
        events: list[dict[str, str]] = []
        with self._lock:
            if self._states.get(winner_id) != "TEMPORARY":
                raise ValueError("winner_not_temporary")
            for worker_id in sorted(self._states):
                prior = self._states[worker_id]
                target = "SELECTED_PENDING_IAM" if worker_id == winner_id else "TERMINATED"
                if prior != "TEMPORARY":
                    raise ValueError(f"invalid_lifecycle_transition:{prior}:{target}")
                self._states[worker_id] = target
                events.append({"worker_id": worker_id, "from": prior, "to": target})
        return events

    def resolve_iam(self, winner_id: str, *, granted: bool) -> dict[str, str]:
        with self._lock:
            prior = self._states.get(winner_id)
            if prior != "SELECTED_PENDING_IAM":
                raise ValueError(f"invalid_iam_transition:{prior}")
            target = "PROMOTED" if granted else "IAM_DENIED"
            self._states[winner_id] = target
            return {"worker_id": winner_id, "from": prior, "to": target}

    def terminate_all(self) -> list[dict[str, str]]:
        events: list[dict[str, str]] = []
        with self._lock:
            for worker_id in sorted(self._states):
                prior = self._states[worker_id]
                if prior == "TEMPORARY":
                    self._states[worker_id] = "TERMINATED"
                    events.append({"worker_id": worker_id, "from": prior, "to": "TERMINATED"})
        return events

    def state(self, worker_id: str) -> str:
        with self._lock:
            return self._states[worker_id]

class SharedPaperAccount:
    """One account with deterministic per-strategy attribution subledgers."""

    def __init__(self, initial_cash: Decimal, *, fee_bps: Decimal = D("1"), run_id: str = "") -> None:
        if initial_cash <= 0:
            raise ValueError("initial_cash_must_be_positive")
        self.initial_cash = initial_cash
        self.fee_bps = fee_bps
        self.run_id = run_id
        self._ledgers: dict[str, AttributedLedger] = {}
        self._lock = Lock()

    def register(self, worker: TemporaryStrategyWorker) -> None:
        allocation = D(str(worker.bundle["capital_allocation"]))
        with self._lock:
            if worker.spec.worker_id in self._ledgers:
                raise ValueError(f"duplicate_worker:{worker.spec.worker_id}")
            allocated = sum((ledger.allocation for ledger in self._ledgers.values()), D("0"))
            if allocated + allocation > self.initial_cash:
                raise ValueError("capital_allocations_exceed_shared_account")
            self._ledgers[worker.spec.worker_id] = AttributedLedger(
                strategy_id=worker.spec.strategy_id,
                strategy_version=worker.spec.strategy_version,
                allocation=allocation,
                cash=allocation,
            )

    def apply_signal(self, worker_id: str, signal: Mapping[str, Any]) -> None:
        price = D(str(signal["price"]))
        target_weight = D(str(signal["target_weight"]))
        if price <= 0 or target_weight < 0 or target_weight > 1:
            raise ValueError("invalid_paper_signal")
        with self._lock:
            ledger = self._ledgers[worker_id]
            equity = ledger.cash + ledger.position * price
            target_quantity = (equity * target_weight / price).quantize(D("0.00000001"))
            delta = target_quantity - ledger.position
            notional = abs(delta) * price
            fee = notional * self.fee_bps / D("10000")
            if delta > 0 and notional + fee > ledger.cash:
                raise ValueError("paper_insufficient_attributed_cash")
            ledger.cash -= delta * price + fee
            ledger.position = target_quantity
            ledger.costs += fee
            if delta:
                ledger.trades += 1
                ledger.fills.append({
                    "worker_id": worker_id,
                    "run_id": self.run_id,
                    "strategy_id": ledger.strategy_id,
                    "strategy_version": ledger.strategy_version,
                    "instrument_id": signal.get("instrument_id"),
                    "sequence": signal["sequence"],
                    "as_of": signal.get("as_of"),
                    "quantity": str(delta),
                    "price": str(price),
                    "fee": str(fee),
                })
            ledger.equity_curve.append(ledger.cash + ledger.position * price)

    def report(self, worker_id: str, *, failure: str | None = None) -> dict[str, Any]:
        with self._lock:
            ledger = self._ledgers[worker_id]
            curve = [ledger.allocation, *ledger.equity_curve]
            ending = curve[-1]
            peak = curve[0]
            max_drawdown = D("0")
            for equity in curve:
                peak = max(peak, equity)
                if peak:
                    max_drawdown = max(max_drawdown, (peak - equity) / peak)
            return {
                "strategy_id": ledger.strategy_id,
                "strategy_version": ledger.strategy_version,
                "capital_allocation": str(ledger.allocation),
                "fills": list(ledger.fills),
                "position": str(ledger.position),
                "pnl": str(ending - ledger.allocation),
                "return": str((ending - ledger.allocation) / ledger.allocation),
                "max_drawdown": str(max_drawdown),
                "trading_cost": str(ledger.costs),
                "trade_count": ledger.trades,
                "failure_reason": failure,
                "stopped_reason": failure,
            }



def _score_report(
    report: Mapping[str, Any],
    *,
    weights: Mapping[str, Decimal],
    risk: Mapping[str, Any],
) -> tuple[Decimal | None, list[str]]:
    required = ("max_drawdown_limit", "minimum_trades", "execution_feasibility", "approved")
    missing = [name for name in required if name not in risk]
    if missing:
        return None, [f"missing_risk_thresholds:{','.join(missing)}"]
    try:
        drawdown_limit = D(str(risk["max_drawdown_limit"]))
        minimum_trades = int(risk["minimum_trades"])
        return_value = D(str(report["return"]))
        drawdown = D(str(report["max_drawdown"]))
        costs = D(str(report["trading_cost"])) / D(str(report["capital_allocation"]))
        execution_feasibility = D(str(risk["execution_feasibility"]))
    except (ArithmeticError, TypeError, ValueError):
        return None, ["invalid_risk_or_report_numeric_value"]
    if not all(value.is_finite() for value in (
        drawdown_limit, execution_feasibility, return_value, drawdown, costs,
    )):
        return None, ["non_finite_risk_or_report_value"]
    if minimum_trades < 0 or drawdown_limit < 0:
        return None, ["invalid_risk_threshold_range"]
    if execution_feasibility < 0 or execution_feasibility > 1:
        return None, ["invalid_execution_feasibility_range"]

    blockers: list[str] = []
    if risk["approved"] is not True:
        blockers.append("risk_rejected")
    if drawdown > drawdown_limit:
        blockers.append("max_drawdown_exceeded")
    if int(report["trade_count"]) < minimum_trades:
        blockers.append("insufficient_trade_sample")
    if report.get("failure_reason"):
        blockers.append("paper_run_failed")
    if blockers:
        return None, blockers

    total_weight = sum(weights.values(), D("0"))
    normalized = {key: value / total_weight for key, value in weights.items()}
    execution = execution_feasibility
    score = (
        normalized["return"] * return_value
        - normalized["drawdown"] * drawdown
        - normalized["cost"] * costs
        + normalized["execution"] * min(execution, D("1"))
    )
    if not score.is_finite():
        return None, ["non_finite_score"]
    return score, []


def run_alpha_strategy_selection(
    strategy_bundles: Iterable[Mapping[str, Any]],
    market_stream: Iterable[Mapping[str, Any]],
    *,
    strategy_executor: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]],
    risk_metrics_provider: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    iam_permission_provider: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    strategy_executor_version: str,
    initial_cash: Decimal = D("1000000"),
    max_workers: int | None = None,
) -> dict[str, Any]:
    """Run validated strategies concurrently and select exactly one or reject all."""
    if not strategy_executor_version:
        raise ValueError("strategy_executor_version_required")
    bundles = list(strategy_bundles)
    trace_hasher = hashlib.sha256(
        json.dumps({
            "bundles": bundles,
            "strategy_executor_version": strategy_executor_version,
        }, default=str, sort_keys=True).encode()
    )
    run_id = trace_hasher.hexdigest()[:24]
    trace_id = run_id
    audit: list[dict[str, Any]] = []
    workers: list[TemporaryStrategyWorker] = []
    rejected: list[dict[str, str]] = []

    seen_versions: set[tuple[str, str]] = set()
    planned_allocation = D("0")
    for bundle in bundles:
        strategy_id = str(bundle.get("strategy_id", "unknown")) if isinstance(bundle, Mapping) else "unknown"
        try:
            normalized = validate_strategy_bundle(bundle)
            identity = (normalized["strategy_id"], normalized["strategy_version"])
            if identity in seen_versions:
                raise StrategyBundleRejected("duplicate_strategy_version")
            if planned_allocation + normalized["capital_allocation"] > D(str(initial_cash)):
                rejected.append({"strategy_id": strategy_id, "reason": "account_capacity_rejected"})
                audit.append({"event": "strategy_account_capacity_rejected",
                              "strategy_id": strategy_id})
                continue
            seen_versions.add(identity)
            planned_allocation += normalized["capital_allocation"]
            worker = create_temporary_worker(normalized, executor=strategy_executor)
            workers.append(worker)
            audit.append({"event": "temporary_worker_created", "worker_id": worker.spec.worker_id})
        except StrategyBundleRejected as exc:
            rejected.append({"strategy_id": strategy_id, "reason": str(exc)})
            audit.append({"event": "strategy_bundle_rejected", "reason": str(exc)})
        except Exception as exc:
            rejected.append({"strategy_id": strategy_id,
                             "reason": f"internal_worker_creation_failed:{type(exc).__name__}"})
            audit.append({"event": "internal_worker_creation_failed",
                          "strategy_id": strategy_id, "error_type": type(exc).__name__})

    if not workers:
        return {
            "pipeline_version": PIPELINE_VERSION,
            "trace_id": trace_id,
            "run_id": trace_id,
            "status": "REJECT",
            "selected_strategy": None,
            "reports": [],
            "rejected": rejected,
            "audit": audit,
        }

    account = SharedPaperAccount(initial_cash, run_id=run_id)
    lifecycle = WorkerLifecycleRegistry()
    registered: list[TemporaryStrategyWorker] = []
    for worker in workers:
        try:
            account.register(worker)
            lifecycle.register_temporary(worker.spec.worker_id)
            registered.append(worker)
        except Exception as exc:
            rejected.append({
                "strategy_id": worker.spec.strategy_id,
                "reason": f"account_registration_failed:{type(exc).__name__}",
            })
            audit.append({
                "event": "account_registration_failed",
                "worker_id": worker.spec.worker_id,
                "error_type": type(exc).__name__,
            })
    workers = registered
    if not workers:
        return {"pipeline_version": PIPELINE_VERSION, "trace_id": trace_id,
                "run_id": trace_id, "status": "REJECT", "selected_strategy": None,
                "reports": [], "rejected": rejected, "audit": audit}

    failures: dict[str, str] = {}
    signals_by_worker: dict[str, list[dict[str, Any]]] = {
        worker.spec.worker_id: [] for worker in workers
    }
    active = {worker.spec.worker_id: worker for worker in workers}
    with ThreadPoolExecutor(max_workers=max_workers or len(workers)) as pool:
        for sequence, raw_event in enumerate(market_stream):
            event = dict(raw_event)
            trace_hasher.update(json.dumps(event, default=str, sort_keys=True).encode())
            futures = {
                pool.submit(worker.run_event, event, sequence=sequence): worker
                for worker in active.values()
            }
            signals: dict[str, Mapping[str, Any]] = {}
            for future in as_completed(futures):
                worker = futures[future]
                try:
                    signal = dict(future.result())
                    signals[worker.spec.worker_id] = signal
                    signals_by_worker[worker.spec.worker_id].append(signal)
                except Exception as exc:
                    failures[worker.spec.worker_id] = f"{type(exc).__name__}:{exc}"
            for worker_id in sorted(signals):
                try:
                    account.apply_signal(worker_id, signals[worker_id])
                except Exception as exc:
                    failures[worker_id] = f"{type(exc).__name__}:{exc}"
            for worker_id in failures:
                active.pop(worker_id, None)
            if not active:
                break
    trace_id = trace_hasher.hexdigest()[:24]
    reports_by_id = {
        worker.spec.worker_id: account.report(
            worker.spec.worker_id, failure=failures.get(worker.spec.worker_id)
        )
        for worker in workers
    }
    for worker_id, report in reports_by_id.items():
        report["signals"] = list(signals_by_worker[worker_id])
        report["worker_id"] = worker_id
    for report in reports_by_id.values():
        report["run_id"] = trace_id
        report["trace_id"] = trace_id
        report["strategy_executor_version"] = strategy_executor_version
        for signal in report["signals"]:
            signal["trace_id"] = trace_id
            signal["run_id"] = trace_id
            signal["strategy_executor_version"] = strategy_executor_version
        for fill in report["fills"]:
            fill["trace_id"] = trace_id
            fill["run_id"] = trace_id
            fill["strategy_executor_version"] = strategy_executor_version

    candidates: list[tuple[Decimal, str, TemporaryStrategyWorker, dict[str, Any]]] = []
    for worker in workers:
        report = reports_by_id[worker.spec.worker_id]
        try:
            risk = dict(risk_metrics_provider(report))
            score, blockers = _score_report(
                report,
                weights=worker.bundle["performance_weights"],
                risk=risk,
            )
        except Exception as exc:
            risk = {}
            score = None
            blockers = [f"risk_metrics_provider_failed:{type(exc).__name__}"]
        report["risk_metrics"] = risk
        report["selection_score"] = str(score) if score is not None else None
        report["selection_blockers"] = blockers
        if score is not None:
            candidates.append((score, worker.spec.worker_id, worker, report))

    selected = None
    if candidates:
        # Stable worker id tie-break makes replay deterministic.
        _, _, winner, winner_report = max(candidates, key=lambda item: (item[0], item[1]))
        lifecycle_events = lifecycle.finalize_selection(winner.spec.worker_id)
        decision_id = hashlib.sha256(json.dumps(
            {
                "trace_id": trace_id,
                "strategy_executor_version": strategy_executor_version,
                "reports": reports_by_id,
            },
            default=str,
            sort_keys=True,
        ).encode()).hexdigest()[:24]
        selected = {
            "strategy_id": winner.spec.strategy_id,
            "strategy_version": winner.spec.strategy_version,
            "worker_id": winner.spec.worker_id,
            "score": winner_report["selection_score"],
            "decision_id": decision_id,
            "promotion_state": "SELECTED_PENDING_IAM",
            "live_order_submission_allowed": False,
            "risk_gate_required": True,
        }
        audit.append({"event": "strategy_selected", **selected})
        for event in lifecycle_events:
            audit.append({"event": "worker_lifecycle_transition", **event})
        try:
            iam_result = dict(iam_permission_provider(selected))
            scopes = iam_result.get("scopes")
            allowed_scopes = {"paper.execute", "strategy.signal.generate"}
            iam_granted = (
                iam_result.get("granted") is True
                and bool(iam_result.get("permission_id"))
                and iam_result.get("worker_id") == winner.spec.worker_id
                and iam_result.get("strategy_id") == winner.spec.strategy_id
                and iam_result.get("strategy_version") == winner.spec.strategy_version
                and isinstance(scopes, list)
                and bool(scopes)
                and set(scopes) <= allowed_scopes
            )
        except Exception as exc:
            iam_result = {"granted": False, "reason": f"{type(exc).__name__}:{exc}"}
            iam_granted = False
        iam_event = lifecycle.resolve_iam(winner.spec.worker_id, granted=iam_granted)
        selected["iam_result"] = iam_result
        selected["promotion_state"] = iam_event["to"]
        audit.append({"event": "worker_lifecycle_transition", **iam_event})
    else:
        for event in lifecycle.terminate_all():
            audit.append({"event": "worker_lifecycle_transition", **event})

    for worker in workers:
        reports_by_id[worker.spec.worker_id]["worker_lifecycle_state"] = lifecycle.state(
            worker.spec.worker_id
        )
    ordered_reports = [reports_by_id[worker.spec.worker_id] for worker in workers]
    return {
        "pipeline_version": PIPELINE_VERSION,
        "trace_id": trace_id,
        "run_id": trace_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "SELECTED" if selected and selected["promotion_state"] == "PROMOTED" else "REJECT",
        "selected_strategy": selected,
        "reports": ordered_reports,
        "rejected": rejected,
        "audit": audit,
    }


def promoted_strategy(selection: Mapping[str, Any]) -> tuple[str, str] | None:
    """주문 후보를 만들어도 되는 전략 하나. 아니면 None.

    선발 결과의 조건 하나라도 어긋나면 None이다 - 승격되지 않은 전략의 시그널이
    주문 후보가 되는 경로를 만들지 않는다. `live_order_submission_allowed`가
    False가 **아닌** 결과도 통과시키지 않는다. 그 값이 뒤집혔다는 것은 선발 계약이
    깨졌다는 뜻이고, 계약이 깨진 결과를 주문 쪽으로 흘리는 것이 제일 나쁘다.
    """
    selected = selection.get("selected_strategy")
    if selection.get("status") != "SELECTED" or not isinstance(selected, Mapping):
        return None
    if selected.get("promotion_state") != "PROMOTED":
        return None
    if selected.get("live_order_submission_allowed") is not False:
        return None
    return str(selected["strategy_id"]), str(selected["strategy_version"])


def propose_intents_from_selection(
    selection: Mapping[str, Any],
    signals: Iterable[Any],
    *,
    nav: Decimal,
    positions: Mapping[Any, Decimal],
    snapshots: Mapping[Any, Any],
    trade_case_id: Any,
    now: datetime,
    presets: Mapping[str, Any] | None = None,
    env: str = "paper",
) -> dict[str, Any]:
    """승격된 전략의 Signal만 OrderIntent 후보로 바꾼다. **제출하지 않는다.**

    선발(`run_alpha_strategy_selection`)과 F11(`intent_builder.build_order_intent`)
    사이의 배선이다. 선발은 "어느 전략이 일해도 되는가"까지만 답하고, 수량·지정가는
    F11이 정한다. 여기서 하는 판단은 하나뿐이다 - **이 시그널이 승격된 그 전략에서
    온 것인가.** 아니면 만들지 않는다.

    거른 시그널은 조용히 사라지지 않고 `rejected`에 사유와 함께 남는다. "만들 이유가
    없다"(이미 목표 도달)와 "만들면 안 된다"(정책 위반)를 사유 문자열로 구분한다.

    nav와 positions는 회계본부 Projection에서 온다. 여기서 계산하지 않는다.
    """
    # 함수 안에서 import - 선발 파이프라인만 쓰는 호출자에게 pydantic/yaml 의존을
    # 얹지 않는다.
    from intent_builder import IntentBuildError, build_order_intent, load_presets

    promoted = promoted_strategy(selection)
    if promoted is None:
        return {"promoted": None, "intents": [],
                "rejected": [{"signal_id": None, "reason": "no_promoted_strategy"}]}

    presets = presets if presets is not None else load_presets()
    intents: list[Any] = []
    rejected: list[dict[str, Any]] = []
    for signal in signals:
        signal_id = str(getattr(signal, "signal_id", ""))
        if (str(signal.strategy_id), signal.strategy_version) != promoted:
            rejected.append({"signal_id": signal_id, "reason": "strategy_not_promoted"})
            continue
        snapshot = snapshots.get(signal.instrument_id)
        if snapshot is None:
            rejected.append({"signal_id": signal_id, "reason": "missing_market_snapshot"})
            continue
        preset = presets.get(signal.philosophy)
        if preset is None:
            rejected.append({"signal_id": signal_id,
                             "reason": f"unknown_philosophy:{signal.philosophy}"})
            continue
        try:
            intent = build_order_intent(
                signal,
                snapshot=snapshot,
                nav=nav,
                current_quantity=positions.get(signal.instrument_id, D("0")),
                preset=preset,
                trade_case_id=trade_case_id,
                now=now,
                env=env,
            )
        except IntentBuildError as exc:
            rejected.append({"signal_id": signal_id, "reason": f"intent_build_rejected:{exc}"})
            continue
        if intent is None:
            rejected.append({"signal_id": signal_id, "reason": "already_at_target"})
            continue
        intents.append(intent)

    return {
        "promoted": {"strategy_id": promoted[0], "strategy_version": promoted[1]},
        "intents": intents,
        "rejected": rejected,
    }


def _self_test() -> None:
    bundles = [
        {"strategy_id": "a", "strategy_version": "v1", "capital_allocation": "400000",
         "performance_weights": {"return": 4, "drawdown": 3, "cost": 2, "execution": 1},
         "target_weight": "0.5"},
        {"strategy_id": "b", "strategy_version": "v1", "capital_allocation": "400000",
         "performance_weights": {"return": 4, "drawdown": 3, "cost": 2, "execution": 1},
         "target_weight": "0.2"},
    ]
    events = [{"as_of": "t1", "price": "100"}, {"as_of": "t2", "price": "110"}]
    out = run_alpha_strategy_selection(
        bundles,
        events,
        strategy_executor=lambda bundle, _event: {"target_weight": bundle["target_weight"]},
        iam_permission_provider=lambda selection: {
            "granted": True,
            "permission_id": "iam-self-test",
            "worker_id": selection["worker_id"],
            "strategy_id": selection["strategy_id"],
            "strategy_version": selection["strategy_version"],
            "scopes": ["paper.execute"],
        },
        strategy_executor_version="self-test-v1",
        risk_metrics_provider=lambda _report: {
            "approved": True, "max_drawdown_limit": "0.2", "minimum_trades": 1,
            "execution_feasibility": "1",
        },
    )
    assert out["status"] == "SELECTED" and out["selected_strategy"]["strategy_id"] == "a"
    assert len(out["reports"]) == 2
    assert all(report["trade_count"] >= 1 for report in out["reports"])
    assert out["selected_strategy"]["live_order_submission_allowed"] is False
    assert out["selected_strategy"]["promotion_state"] == "PROMOTED"

    _self_test_intent_proposal()


def _self_test_intent_proposal() -> None:
    """선발 -> OrderIntent 후보 배선. 승격된 전략 하나만 통과한다."""
    from datetime import timedelta
    from uuid import uuid4

    from contracts import MarketSnapshot, Side, StrategySignal

    now = datetime.now(timezone.utc)
    strategy, other = uuid4(), uuid4()
    fund, book, instrument = uuid4(), uuid4(), uuid4()

    def selection(**over) -> dict[str, Any]:
        selected = {"strategy_id": str(strategy), "strategy_version": "v1",
                    "promotion_state": "PROMOTED", "live_order_submission_allowed": False}
        selected.update(over)
        return {"status": "SELECTED", "selected_strategy": selected}

    def signal(strategy_id=None, **over) -> StrategySignal:
        kw = {"strategy_id": strategy_id or strategy, "strategy_version": "v1",
              "fund_id": fund, "book_id": book, "instrument_id": instrument,
              "philosophy": "momentum", "target_weight": D("0.02"), "stage": "paper",
              "as_of": now, "valid_until": now + timedelta(hours=1), "trace_id": "t1"}
        kw.update(over)
        return StrategySignal(**kw)

    snapshots = {instrument: MarketSnapshot(market_snapshot_id="s1", as_of=now,
                                            bid=D("69900"), ask=D("70100"))}

    def propose(sel, sigs, positions=None):
        return propose_intents_from_selection(
            sel, sigs, nav=D("1000000000"), positions=positions or {},
            snapshots=snapshots, trade_case_id=uuid4(), now=now)

    # 1. 승격된 전략의 시그널은 주문 후보가 된다 (제출은 아니다)
    out = propose(selection(), [signal()])
    assert len(out["intents"]) == 1, out["rejected"]
    assert out["intents"][0].side is Side.BUY and out["intents"][0].quantity == D("285")

    # 2. 승격 전 상태는 하나도 통과하지 못한다 - 선발만으로 주문을 만들지 않는다
    for state in ("SELECTED_PENDING_IAM", "IAM_DENIED", "TERMINATED"):
        blocked = propose(selection(promotion_state=state), [signal()])
        assert blocked["intents"] == [] and blocked["promoted"] is None, state
    assert propose({"status": "REJECT", "selected_strategy": None}, [signal()])["intents"] == []
    # 계약이 뒤집힌 결과(제출 허용)도 통과시키지 않는다
    assert propose(selection(live_order_submission_allowed=True), [signal()])["intents"] == []

    # 3. 승격되지 않은 다른 전략의 시그널은 섞여 들어와도 걸러진다
    mixed = propose(selection(), [signal(), signal(strategy_id=other)])
    assert len(mixed["intents"]) == 1
    assert [r["reason"] for r in mixed["rejected"]] == ["strategy_not_promoted"]

    # 4. 거른 이유는 남는다 - 조용히 사라지지 않는다
    at_target = propose(selection(), [signal()], positions={instrument: D("285")})
    assert [r["reason"] for r in at_target["rejected"]] == ["already_at_target"]
    no_snap = propose_intents_from_selection(
        selection(), [signal()], nav=D("1000000000"), positions={}, snapshots={},
        trade_case_id=uuid4(), now=now)
    assert [r["reason"] for r in no_snap["rejected"]] == ["missing_market_snapshot"]
    unknown = propose(selection(), [signal(philosophy="astrology")])
    assert unknown["rejected"][0]["reason"].startswith("unknown_philosophy"), unknown
    # 승격된 전략이어도 시그널 자체가 주문 대상이 아니면 F11이 막는다(shadow 단계).
    # 만료 시그널은 여기까지 오지도 못한다 - StrategySignal 계약이 먼저 거부한다.
    shadow = propose(selection(), [signal(stage="shadow")])
    assert shadow["rejected"][0]["reason"].startswith("intent_build_rejected"), shadow


if __name__ == "__main__":
    _self_test()
    print("parallel alpha paper selection checks passed")
