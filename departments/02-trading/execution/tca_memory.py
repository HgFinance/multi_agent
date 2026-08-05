#!/usr/bin/env python3
"""TCA 환류 — 집행 기억. 과거 실현 슬리피지로 집행 파라미터 조정안을 만든다.

소유: 도현 (트레이딩본부)
근거: docs/HEDGE_FUND_MASTER_PLAN.md 19.8 (Execution Desk), 12(TCA)
      contracts/philosophies.yaml momentum "TCA 결과를 다음 리밸런싱 파라미터로 환류한다"
      CLAUDE.local.md "만들지 않는 것" — 한도 변경은 리스크본부, 전략 승격은 퀀트본부

**제안까지만 한다.** 이 모듈에는 philosophies.yaml 을 쓰는 경로가 없다(읽기만 한다).
반환값은 조정 **제안**이고 반영은 다른 본부의 승인을 거친다:

  참여율·슬리피지 예산 한도 변경 -> 리스크본부
  전략 승격·리밸런싱 규칙 변경   -> 퀀트/백테스트본부
  집행 파라미터 실제 적용        -> 위 승인 뒤 우리가 philosophies.yaml 을 고친다

경계에서 지키는 것 넷:

  1. **Paper 와 Live 를 섞지 않는다.** Paper 슬리피지는 시뮬레이션값이다. 그걸로 실거래
     파라미터를 제안하면 없는 근거를 만드는 것이다 - broker_adapter 별로 분리하고
     제안에 어느 adapter 에서 배운 것인지 항상 붙인다.
  2. **표본이 적으면 제안하지 않는다.** min_samples 미만은 insufficient_evidence 다
     (skills/agentic-rag 의 grounded=false 처리와 같은 원칙).
  3. **완화 방향 제안은 따로 표시한다.** 비용이 예상보다 쌌다는 관측은 "더 크게/빠르게
     집행하자"로 이어지는데, 이건 거래 확대 방향이다(개발 원칙 9번). 제안은 하되
     `direction: loosen` 으로 구분해 승인자가 조인다/푼다를 헷갈리지 않게 한다.
  4. **분할 수 제안은 브로커 한도를 통과해야 한다.** slices 를 늘리라는 제안이 정정·취소
     초당 3건을 넘기면 그 제안 자체가 불가능하다 - broker_rules 로 검증한 뒤에만 낸다.

**LLM 이 없다.** 중앙값·분위수·비교는 전부 산수이고, 회계·집행 수치를 LLM 문장에서
뽑지 않는다(CLAUDE.local.md 원칙 5).

자체 점검: python departments/02-trading/execution/tca_memory.py
  DATABASE_URL 이 있으면 실 DB 왕복(읽기 전용)까지 본다.
"""
from __future__ import annotations

import os
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "contracts"))

import yaml

from broker_rules import ExecutionPlanDraft, check_plan_feasible

PHILOSOPHIES_PATH = _HERE.parent / "contracts" / "philosophies.yaml"

# Paper 는 시뮬레이션이다. 이 목록 밖의 adapter 만 실집행 근거로 본다.
SIMULATED_ADAPTERS = frozenset({"paper", "sim", "backtest"})


class TcaMemoryError(Exception):
    """집행 기억을 만들 수 없는 경우. 빈 제안을 지어내지 않는다."""


@dataclass(frozen=True)
class ExecutionRecord:
    """과거 집행 한 건. execution.tca_results + orders 조인 결과 한 줄이다."""

    order_id: str
    philosophy: str
    side: str
    adapter: str
    notional: Decimal
    slippage_bps: Decimal
    slices: int
    participation_rate: Decimal | None
    calculated_at: datetime


@dataclass(frozen=True)
class Settings:
    min_samples: int
    lookback_days: int
    notional_buckets_krw: tuple[Decimal, ...]
    tighten_ratio: Decimal
    loosen_ratio: Decimal
    participation_step: Decimal
    slices_step: int


def load_settings(path: Path = PHILOSOPHIES_PATH) -> Settings:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    block = doc.get("tca_memory")
    if not block:
        raise TcaMemoryError(f"{path} 에 tca_memory 블록이 없습니다. 튜닝값은 코드에 두지 않습니다")
    return Settings(
        min_samples=int(block["min_samples"]),
        lookback_days=int(block["lookback_days"]),
        notional_buckets_krw=tuple(Decimal(str(v)) for v in block["notional_buckets_krw"]),
        tighten_ratio=Decimal(str(block["tighten_ratio"])),
        loosen_ratio=Decimal(str(block["loosen_ratio"])),
        participation_step=Decimal(str(block["participation_step"])),
        slices_step=int(block["slices_step"]),
    )


def load_execution_presets(path: Path = PHILOSOPHIES_PATH) -> dict[str, dict[str, Any]]:
    """철학별 현재 집행 프리셋. 제안의 '현재값'이 여기서 온다."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {key: dict(block["execution"]) for key, block in doc["philosophies"].items()}


def notional_bucket(notional: Decimal, settings: Settings) -> str:
    edges = settings.notional_buckets_krw
    names = ("small", "mid", "large")
    for edge, name in zip(edges, names):
        if notional < edge:
            return name
    return names[len(edges)] if len(edges) < len(names) else names[-1]


# ── 검색: 과거 유사 집행 ────────────────────────────────────────────────────
def recall(records: Iterable[ExecutionRecord], *, philosophy: str, side: str,
           notional: Decimal, adapter: str, settings: Settings,
           now: datetime | None = None) -> list[ExecutionRecord]:
    """지금 내려는 주문과 **유사한** 과거 집행을 꺼낸다.

    유사 = 같은 철학 · 같은 방향 · 같은 규모 구간 · 같은 broker adapter · lookback 안.
    adapter 를 유사 조건에서 빼면 Paper 체결이 실거래 근거로 섞인다.
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=settings.lookback_days)
    bucket = notional_bucket(notional, settings)
    return [r for r in records
            if r.philosophy == philosophy and r.side == side and r.adapter == adapter
            and notional_bucket(r.notional, settings) == bucket
            and r.calculated_at >= cutoff]


def _quantiles(values: Sequence[Decimal]) -> dict[str, Decimal]:
    ordered = sorted(values)
    median = Decimal(str(statistics.median(ordered)))
    # p75 는 보간 없이 상위 인덱스를 쓴다 - 표본이 적을 때 보간값이 관측되지 않은 수를 만든다.
    p75 = ordered[min(len(ordered) - 1, (len(ordered) * 3) // 4)]
    return {"median_bps": median, "p75_bps": p75,
            "worst_bps": ordered[-1], "best_bps": ordered[0]}


# ── 제안 생성 (결정론) ─────────────────────────────────────────────────────
def propose_adjustments(records: Iterable[ExecutionRecord], *,
                        presets: Mapping[str, Mapping[str, Any]] | None = None,
                        settings: Settings | None = None,
                        adapter: str, now: datetime | None = None) -> dict[str, Any]:
    """철학별 집행 파라미터 조정안. **적용하지 않는다 - 제안만 만든다.**"""
    settings = settings or load_settings()
    presets = presets if presets is not None else load_execution_presets()
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=settings.lookback_days)

    rows = [r for r in records if r.adapter == adapter and r.calculated_at >= cutoff]
    proposals: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for philosophy, preset in sorted(presets.items()):
        group = [r for r in rows if r.philosophy == philosophy]
        if len(group) < settings.min_samples:
            skipped.append({"philosophy": philosophy, "samples": len(group),
                            "reason": "insufficient_evidence",
                            "detail": f"표본 {len(group)}건 < 최소 {settings.min_samples}건"})
            continue

        stats = _quantiles([r.slippage_bps for r in group])
        budget = Decimal(str(preset["slippage_budget_bps"]))
        median = stats["median_bps"]
        current_participation = Decimal(str(preset["max_participation_rate"]))
        current_slices = int(preset["slices"])

        if median > budget * settings.tighten_ratio:
            direction, changes = "tighten", {}
            # 조이는 방향: 참여율을 낮추고 분할을 늘린다. 예산 자체를 올리지 않는다 -
            # 예산을 실측에 맞춰 올리면 비싼 집행이 사후에 정당해진다.
            new_participation = max(current_participation - settings.participation_step,
                                    settings.participation_step)
            if new_participation < current_participation:
                changes["max_participation_rate"] = new_participation
            new_slices = current_slices + settings.slices_step
            # 분할 제안은 브로커 한도를 통과해야 실행 가능하다
            window = Decimal(str(preset.get("cancel_after_min", 30)))
            feasible = check_plan_feasible(ExecutionPlanDraft(
                slices=new_slices, window_minutes=float(window), replaces_per_slice=1))
            if feasible["feasible"]:
                changes["slices"] = new_slices
            else:
                skipped.append({"philosophy": philosophy, "reason": "broker_rate_limit",
                                "detail": f"slices {new_slices} 제안이 한도에 걸린다: "
                                          f"{feasible['violations'][0]['detail']}",
                                "rule_ids": feasible["violations"][0]["rule_ids"]})
        elif median < budget * settings.loosen_ratio:
            direction = "loosen"
            changes = {"max_participation_rate": current_participation + settings.participation_step}
            if current_slices - settings.slices_step >= 1:
                changes["slices"] = current_slices - settings.slices_step
        else:
            skipped.append({"philosophy": philosophy, "samples": len(group),
                            "reason": "within_budget",
                            "detail": f"중앙값 {median}bps 가 예산 {budget}bps 밴드 안이다"})
            continue

        if not changes:
            skipped.append({"philosophy": philosophy, "samples": len(group),
                            "reason": "no_room", "detail": "더 조정할 여지가 없다"})
            continue

        proposals.append({
            "philosophy": philosophy,
            "direction": direction,
            "current": {"slippage_budget_bps": budget,
                        "max_participation_rate": current_participation,
                        "slices": current_slices},
            "proposed": changes,
            "evidence": {"samples": len(group), "adapter": adapter,
                         "lookback_days": settings.lookback_days,
                         "window_from": cutoff.isoformat(), "window_to": now.isoformat(),
                         "simulated": adapter in SIMULATED_ADAPTERS,
                         **{k: str(v) for k, v in stats.items()}},
            "rationale": (f"실현 슬리피지 중앙값 {stats['median_bps']}bps 대 "
                          f"예산 {budget}bps ({len(group)}건, adapter={adapter})"),
        })

    return {
        "proposals": proposals,
        "skipped": skipped,
        "adapter": adapter,
        # 이 근거가 시뮬레이션인지 실집행인지가 제안 신뢰도를 가른다
        "evidence_is_simulated": adapter in SIMULATED_ADAPTERS,
        # 계약으로 박는다 - 이 반환값을 받아 바로 적용하는 소비자가 없어야 한다
        "authoritative": False,
        "applies_automatically": False,
        "approval_required": {
            "participation_and_budget_limits": "risk-department",
            "strategy_promotion_and_rebalance_rules": "quant-backtest-department",
            "execution_preset_edit": "trading-department (위 승인 이후)",
        },
    }


# ── DB 조회 (읽기 전용) ────────────────────────────────────────────────────
_TCA_SQL = """
select o.order_id::text            as order_id,
       oi.strategy_version_id::text as strategy_version_id,
       oi.side                      as side,
       o.broker_adapter             as adapter,
       coalesce(o.filled_quantity * o.average_fill_price, 0) as notional,
       t.slippage_bps               as slippage_bps,
       t.calculated_at              as calculated_at
  from execution.tca_results t
  join execution.orders o        on o.order_id = t.order_id
  join execution.order_intents oi on oi.order_intent_id = o.order_intent_id
 where t.slippage_bps is not null
   and t.calculated_at >= %(cutoff)s
 order by t.calculated_at desc
 limit %(limit)s
"""


def fetch_records(dsn: str | None = None, *,
                  philosophy_by_strategy: Mapping[str, str] | None = None,
                  settings: Settings | None = None, now: datetime | None = None,
                  limit: int = 5000) -> tuple[list[ExecutionRecord], list[dict[str, Any]]]:
    """execution.tca_results 에서 과거 집행을 읽는다. **읽기만 한다.**

    `philosophy` 컬럼은 DB 에 없다(스키마는 팀 소유라 우리가 늘리지 않는다). 호출자가
    strategy_version_id -> philosophy 매핑을 주면 그 행만 쓰고, 못 매핑한 행은 버리지 않고
    `unmapped` 로 돌려준다 - 조용히 사라지면 표본이 왜 적은지 알 수 없다.
    """
    settings = settings or load_settings()
    dsn = dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        raise TcaMemoryError("DATABASE_URL 이 없습니다. 집행 기억을 읽을 수 없습니다")
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except ImportError as exc:  # pragma: no cover - 드라이버 부재 환경
        raise TcaMemoryError("psycopg2-binary 가 필요합니다") from exc

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=settings.lookback_days)
    mapping = philosophy_by_strategy or {}
    records: list[ExecutionRecord] = []
    unmapped: list[dict[str, Any]] = []
    with psycopg2.connect(dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(_TCA_SQL, {"cutoff": cutoff, "limit": limit})
        for row in cur.fetchall():
            philosophy = mapping.get(row["strategy_version_id"])
            if philosophy is None:
                unmapped.append({"order_id": row["order_id"],
                                 "strategy_version_id": row["strategy_version_id"]})
                continue
            records.append(ExecutionRecord(
                order_id=row["order_id"], philosophy=philosophy, side=row["side"],
                adapter=row["adapter"], notional=Decimal(str(row["notional"])),
                slippage_bps=Decimal(str(row["slippage_bps"])),
                # slices / participation 은 execution_plans 소관이라 TCA 행에 없다.
                # 없는 값을 0 으로 채우지 않는다 - 제안은 프리셋의 현재값을 기준으로 만든다.
                slices=0, participation_rate=None,
                calculated_at=row["calculated_at"]))
    return records, unmapped


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    D = Decimal
    now = datetime(2026, 8, 5, 6, 0, tzinfo=timezone.utc)
    settings = load_settings()
    presets = load_execution_presets()

    def record(**over) -> ExecutionRecord:
        kw = {"order_id": "o1", "philosophy": "momentum", "side": "BUY", "adapter": "ls-live",
              "notional": D("20000000"), "slippage_bps": D("10"), "slices": 5,
              "participation_rate": D("0.03"), "calculated_at": now - timedelta(days=1)}
        kw.update(over)
        return ExecutionRecord(**kw)

    def many(n: int, **over) -> list[ExecutionRecord]:
        return [record(order_id=f"o{i}", **over) for i in range(n)]

    def raises(fn, why):
        try:
            fn()
        except TcaMemoryError:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    # 1. 튜닝값은 코드가 아니라 philosophies.yaml 에서 온다
    assert settings.min_samples == 20 and settings.lookback_days == 90
    assert settings.tighten_ratio == D("1.2") and settings.loosen_ratio == D("0.5")
    assert set(presets) == {"trend_following", "value", "momentum", "mean_reversion"}
    print("  튜닝값 YAML 적재           OK")

    # 2. 유사 집행 검색 - 철학·방향·규모·adapter·기간이 전부 맞아야 유사다
    pool = (many(3) + many(2, side="SELL") + many(2, philosophy="value")
            + many(2, adapter="paper") + many(2, notional=D("500000000"))
            + many(2, calculated_at=now - timedelta(days=200)))
    hits = recall(pool, philosophy="momentum", side="BUY", notional=D("20000000"),
                  adapter="ls-live", settings=settings, now=now)
    assert len(hits) == 3, len(hits)
    # **Paper 는 실거래 근거로 섞이지 않는다** - 이 파일의 존재 이유 중 하나
    assert all(h.adapter == "ls-live" for h in hits)
    assert len(recall(pool, philosophy="momentum", side="BUY", notional=D("20000000"),
                      adapter="paper", settings=settings, now=now)) == 2
    assert notional_bucket(D("5000000"), settings) == "small"
    assert notional_bucket(D("20000000"), settings) == "mid"
    assert notional_bucket(D("500000000"), settings) == "large"
    print("  유사 집행 검색             OK")

    # 3. 표본이 적으면 제안하지 않는다 (근거 부족을 통과로 바꾸지 않는다)
    thin = propose_adjustments(many(5, slippage_bps=D("90")), presets=presets,
                               settings=settings, adapter="ls-live", now=now)
    assert thin["proposals"] == [], thin["proposals"]
    reasons = {s["reason"] for s in thin["skipped"]}
    assert reasons == {"insufficient_evidence"}, reasons
    print("  표본 부족 -> 제안 없음     OK")

    # 4. **예산 초과는 조이는 방향으로 제안한다** (예산을 올려주지 않는다)
    #    momentum 예산 30bps, 실현 중앙값 60bps -> 1.2배 초과
    over = propose_adjustments(many(25, slippage_bps=D("60")), presets=presets,
                               settings=settings, adapter="ls-live", now=now)
    assert len(over["proposals"]) == 1, over["proposals"]
    p = over["proposals"][0]
    assert p["philosophy"] == "momentum" and p["direction"] == "tighten"
    assert p["proposed"]["max_participation_rate"] < p["current"]["max_participation_rate"]
    assert p["proposed"]["slices"] > p["current"]["slices"]
    assert "slippage_budget_bps" not in p["proposed"], "예산을 올려 비싼 집행을 정당화했다"
    assert p["evidence"]["samples"] == 25 and p["evidence"]["adapter"] == "ls-live"
    assert p["evidence"]["median_bps"] == "60"
    print("  예산 초과 -> 집행 조임     OK")

    # 5. 비용이 예상보다 싼 경우는 완화 제안이지만 방향이 표시된다 (거래 확대 방향)
    cheap = propose_adjustments(many(25, slippage_bps=D("5")), presets=presets,
                                settings=settings, adapter="ls-live", now=now)
    loose = [x for x in cheap["proposals"] if x["philosophy"] == "momentum"][0]
    assert loose["direction"] == "loosen"
    assert loose["proposed"]["max_participation_rate"] > loose["current"]["max_participation_rate"]
    # 밴드 안이면 아무 제안도 하지 않는다
    band = propose_adjustments(many(25, slippage_bps=D("30")), presets=presets,
                               settings=settings, adapter="ls-live", now=now)
    assert band["proposals"] == []
    assert {s["reason"] for s in band["skipped"]} == {"insufficient_evidence", "within_budget"}
    print("  완화 방향 구분 표시        OK")

    # 6. **분할 제안이 브로커 한도를 넘으면 그 제안은 나가지 않는다** (broker_rules 연동)
    tight_preset = {"momentum": {**presets["momentum"], "slices": 5400,
                                 "cancel_after_min": 30}}
    blocked = propose_adjustments(many(25, slippage_bps=D("60")), presets=tight_preset,
                                  settings=settings, adapter="ls-live", now=now)
    limited = [s for s in blocked["skipped"] if s["reason"] == "broker_rate_limit"]
    assert limited, blocked["skipped"]
    assert limited[0]["rule_ids"], "한도 위반에 근거 rule_id 가 없다"
    # 참여율 조정은 한도와 무관하므로 살아남는다 - 제안 전체가 사라지지 않는다
    assert blocked["proposals"] and "slices" not in blocked["proposals"][0]["proposed"]
    print("  분할 제안 한도 검증        OK")

    # 7. Paper 근거는 시뮬레이션으로 표시된다
    sim = propose_adjustments(many(25, slippage_bps=D("60"), adapter="paper"),
                              presets=presets, settings=settings, adapter="paper", now=now)
    assert sim["evidence_is_simulated"] is True
    assert sim["proposals"][0]["evidence"]["simulated"] is True
    assert over["evidence_is_simulated"] is False
    print("  Paper/Live 근거 구분       OK")

    # 8. 권한 경계 - 이 모듈은 적용하지 않는다
    assert over["authoritative"] is False and over["applies_automatically"] is False
    assert over["approval_required"]["participation_and_budget_limits"] == "risk-department"
    assert (over["approval_required"]["strategy_promotion_and_rebalance_rules"]
            == "quant-backtest-department")
    # 제안을 만들어도 philosophies.yaml 은 그대로다 - 적용은 승인 뒤 사람이 한다
    before = PHILOSOPHIES_PATH.read_bytes()
    propose_adjustments(many(25, slippage_bps=D("60")), presets=presets,
                        settings=settings, adapter="ls-live", now=now)
    assert PHILOSOPHIES_PATH.read_bytes() == before, "제안 생성이 프리셋을 고쳤다"
    print("  제안 전용 (프리셋 불변)    OK")

    # 9. 실 DB 왕복 (읽기 전용). DATABASE_URL 이 없으면 건너뛴다
    if os.environ.get("DATABASE_URL"):
        try:
            rows, unmapped = fetch_records(now=now)
            print(f"  실 DB 조회                 OK (행 {len(rows)}, 미매핑 {len(unmapped)})")
        except TcaMemoryError as exc:
            print(f"  실 DB 조회                 skip - {exc}")
    else:
        raises(lambda: fetch_records(dsn=""), "DATABASE_URL 없이 조회")
        print("  실 DB 조회                 skip - DATABASE_URL 없음")

    print("ok - TCA 집행 기억 8개 영역 점검 통과 (제안만, 적용은 리스크·퀀트 승인)")
