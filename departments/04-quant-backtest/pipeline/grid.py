#!/usr/bin/env python3
"""**행동 격자** - 하나의 구조가 네 가지 역할을 한다.

    탐색   빈 칸 = 아직 안 가 본 곳 → 어디를 파야 하나
    통계   찬 칸 수 = **유효 시도 수** (상관된 변형을 1회로 센다)
    폭     찬 칸 수 = Breadth (`IR ≈ IC × √B`)
    검정   칸 하나 = FDR 의 검정 단위 하나

▶ 왜 필요한가 (2026-08-12 실측)
  ① **기만적 지형** - 거리 순으로만 배분하면 국소 최적에 갇힌다.
     `momentum` 이 v2 에서 최고(Sharpe 1.28)였다가 v3 에서 -0.36 이 됐고,
     v2 에서 **기각됐던** `low_volatility` 가 v3 에서 관문 6/9 로 최고가 됐다.
     거리 순으로만 갔으면 low_volatility 는 영원히 안 돌았다 - 예산이 남아
     우연히 돈 것이다. MAP-Elites 가 말하는 **디딤돌**이 정확히 이것이다.

  ② **유효 N** - DSR 은 "N개 중 최고" 를 보정한다. 그런데 지금 우리는
     계열 **안**에서는 카드를 그대로 세고(lookback 만 다른 5개 = 5회, 실은
     ~90% 상관) 계열 **간**에는 아예 안 센다. 두 방향으로 다 틀렸다.
     López de Prado 본인이 말한다 - N 은 백테스트 횟수가 아니라
     **"충분히 다르고 충분히 무상관인 시도의 수"** 이고 클러스터링으로 센다.
     격자가 그 클러스터링이다.

▶ 왜 구성(위험관리)이 축인가 - 오늘 IC 실측이 가르쳐 줬다
     breakout   IC +0.0372 (t +2.36, 적중 60.2%)  ← 신호는 살아 있다
                백테스트 초과 -168.77%p           ← 구성이 죽였다
  같은 신호도 구성이 다르면 **다른 칸**이다. 축에 안 넣으면 "신호가 나쁘다"
  와 "구성이 나쁘다" 가 한 칸에서 섞인다.

▶ 격자는 **원장에서 매번 다시 만든다.** 상태로 들고 있지 않는다 -
  들고 있으면 원장과 갈리고, 오늘 하루 그 형태의 결함을 열두 번 고쳤다.

사용:
    quant-py grid.py            # 격자 현황
    quant-py grid.py --check
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stock_universe import governed_stock_evidence_sql  # noqa: E402

GRID_VERSION = "quant-grid-v1"
_GOVERNED_GRID_EVIDENCE = governed_stock_evidence_sql(
    experiment_alias="e", dataset_alias="manifest", hypothesis_alias="h")

# ── 축 ───────────────────────────────────────────────────────────────────────
# ▶ 축은 **행동 서술자**다 - "무엇을 다르게 하는가" 이지 "얼마나 잘하는가"
#   가 아니다. 성적을 축에 넣으면 격자가 순위표가 되고 디딤돌이 사라진다.
HORIZON_BUCKETS = (
    ("단기", 1, 10),        # 1~10일 - 미시구조·단기반전
    ("중기", 11, 60),       # 11~60일 - 모멘텀 형성창의 주 영역
    ("장기", 61, 250),      # 61~250일 - 저변동성·가치
)

# 구성(위험관리) 축. 오늘 연 손잡이가 그대로 축이 된다.
CONSTRUCTIONS = ("무위험관리", "낙폭정지", "변동성타게팅", "둘다")


def horizon_bucket(days) -> str:
    """보유 지평 -> 버킷 이름. 못 읽으면 `?` - 지어내지 않는다."""
    try:
        d = int(days)
    except (TypeError, ValueError):
        return "?"
    for name, lo, hi in HORIZON_BUCKETS:
        if lo <= d <= hi:
            return name
    return "장기" if d > 250 else "?"


def construction_of(config: dict) -> str:
    """config -> 구성 축. **손잡이를 안 켜면 `무위험관리` 다.**"""
    c = config or {}
    stop = c.get("max_drawdown_stop") is not None
    vol = c.get("vol_target_annual") is not None
    if stop and vol:
        return "둘다"
    if stop:
        return "낙폭정지"
    if vol:
        return "변동성타게팅"
    return "무위험관리"


def cell_of(edge_type: str, universe_key: str, config: dict) -> tuple:
    """실험 하나 -> 칸 좌표. **같은 칸 = 상관된 시도 = 유효 1회.**"""
    return (
        str(edge_type or "?").strip().lower() or "?",
        str(universe_key or "?").strip().lower() or "?",
        horizon_bucket((config or {}).get("lookback_days")),
        construction_of(config),
    )


@dataclass
class Cell:
    """칸 하나. **그 칸의 최고 하나만 남기고 시도 수는 따로 센다.**"""

    key: tuple
    trials: int = 0                 # 이 칸에서 돈 횟수 (상관된 시도)
    best_experiment: str = ""
    best_dsr: float | None = None
    best_ic_t: float | None = None
    best_excess: float | None = None

    @property
    def alive(self) -> bool:
        """**신호가 살아 있는 칸.** Breadth 는 이것만 센다.

        IC 가 있으면 IC 를 우선한다 - 포트폴리오 구성과 신호 예측력을 분리하기
        위해서다. 단 IC 는 집중도·데이터 결함 검사를 대신하지 않으므로 별도
        관문과 함께 본다. IC 가 없으면 초과수익으로 보되, 그건 약한 근거다.
        """
        if self.best_ic_t is not None:
            return self.best_ic_t > 0
        return self.best_excess is not None and self.best_excess > 0

    def as_dict(self) -> dict:
        d = asdict(self)
        d["key"] = list(self.key)
        d["alive"] = self.alive
        return d


@dataclass
class Grid:
    cells: dict = field(default_factory=dict)

    @property
    def occupied(self) -> int:
        return len(self.cells)

    @property
    def effective_n(self) -> int:
        """**유효 시도 수 = 찬 칸 수.** 실험 행 수가 아니다.

        같은 칸의 변형들은 상관되므로 독립 시도로 세면 스스로 문턱을 올린다.
        DSR 이 이 수를 써야 한다 - 지금은 계열 안의 카드 수를 쓴다.
        """
        return len(self.cells)

    @property
    def breadth(self) -> int:
        """**독립 베팅 수.** `IR ≈ IC × √B` 의 B."""
        return sum(1 for c in self.cells.values() if c.alive)

    @property
    def total_trials(self) -> int:
        return sum(c.trials for c in self.cells.values())

    def as_dict(self) -> dict:
        return {"occupied": self.occupied, "effective_n": self.effective_n,
                "breadth": self.breadth, "total_trials": self.total_trials,
                "cells": [c.as_dict() for c in self.cells.values()]}


def axis_values(cells) -> dict:
    """지금까지 실제로 나타난 축 값들. **어휘를 지어내지 않는다.**"""
    out = {"edge": set(), "universe": set(), "horizon": set(), "construction": set()}
    for k in cells:
        for name, v in zip(("edge", "universe", "horizon", "construction"), k):
            out[name].add(v)
    return {k: sorted(v) for k, v in out.items()}


def empty_cells(grid: Grid, *, edges=None, universes=None) -> list:
    """아직 안 가 본 칸. **탐색 압력의 원천이다.**

    축 값은 통제 어휘에서 온다 - 격자가 어휘 밖을 제안하면 접수에서 막힌다.
    """
    if edges is None:
        try:
            from experiment_orchestrator import STRATEGY_CATALOG
            edges = sorted(STRATEGY_CATALOG)
        except Exception:  # noqa: BLE001
            edges = sorted({k[0] for k in grid.cells})
    if universes is None:
        try:
            from trial_family import UNIVERSE_VOCAB
            universes = sorted(UNIVERSE_VOCAB)
        except Exception:  # noqa: BLE001
            universes = sorted({k[1] for k in grid.cells})
    out = []
    for e in edges:
        for u in universes:
            for h, _lo, _hi in HORIZON_BUCKETS:
                for c in CONSTRUCTIONS:
                    k = (e, u, h, c)
                    if k not in grid.cells:
                        out.append(k)
    return out


# ── 원장에서 만든다 ──────────────────────────────────────────────────────────

_SQL = """
with trial_pressure as (
  select e.experiment_id, e.config, h.expected_edge
    from quant.experiments e
    join quant.hypotheses h on h.hypothesis_id = e.hypothesis_id
   where e.status = 'COMPLETED'
), eligible_performance as (
  select e.experiment_id,
         max(case when metric.metric='deflated_sharpe'
                       and metric.split='TEST' then metric.value end) dsr,
         max(case when metric.metric='excess_return_pct'
                       and metric.split='TEST' then metric.value end) ex,
         max(case when metric.metric='signal_ic_t' then metric.value end) ic_t
    from quant.experiments e
    join quant.hypotheses h on h.hypothesis_id = e.hypothesis_id
    join quant.dataset_manifests manifest on manifest.dataset_id = e.dataset_id
    left join quant.experiment_metrics metric
      on metric.experiment_id = e.experiment_id
   where e.status = 'COMPLETED'
     and """ + _GOVERNED_GRID_EVIDENCE + """
   group by e.experiment_id
)
select trial.experiment_id::text, trial.config,
       coalesce(trial.expected_edge->>'type', ''),
       coalesce(trial.expected_edge->>'universe_key', ''),
       performance.dsr, performance.ex, performance.ic_t,
       performance.experiment_id is not null as eligible_evidence
  from trial_pressure trial
  left join eligible_performance performance
    on performance.experiment_id = trial.experiment_id
"""


def build(conn) -> Grid:
    """원장 -> 격자. **매번 다시 만든다** - 상태로 들고 있으면 원장과 갈린다."""
    g = Grid()
    with conn.cursor() as cur:
        cur.execute(_SQL)
        rows = cur.fetchall()
    for eid, cfg, edge, uni, dsr, ex, ic_t, eligible_evidence in rows:
        cfg = cfg or {}
        # 유니버스가 가설에 없으면 config 에서, 그것도 없으면 `?`
        key = cell_of(edge or cfg.get("edge_type"),
                      uni or cfg.get("universe_key"), cfg)
        c = g.cells.get(key)
        if c is None:
            c = Cell(key=key)
            g.cells[key] = c
        c.trials += 1
        if not eligible_evidence:
            continue
        # **칸의 최고 하나만 남긴다.** 우선순위: IC t > DSR (IC 가 안 속는다)
        better = (
            (ic_t is not None and (c.best_ic_t is None or float(ic_t) > c.best_ic_t))
            or (ic_t is None and c.best_ic_t is None
                and dsr is not None
                and (c.best_dsr is None or float(dsr) > c.best_dsr)))
        if better or not c.best_experiment:
            c.best_experiment = eid
            if dsr is not None:
                c.best_dsr = float(dsr)
            if ex is not None:
                c.best_excess = float(ex)
            if ic_t is not None:
                c.best_ic_t = float(ic_t)
    return g


def render(grid: Grid, *, top_empty: int = 6) -> str:
    if not grid.cells:
        return "격자가 비어 있다. (지어내지 않았다 - 완주한 실험이 없다)"
    lines = [
        f"찬 칸 {grid.occupied} · 총 시도 {grid.total_trials} "
        f"· **유효 N {grid.effective_n}** · 폭(살아있는 칸) {grid.breadth}",
        "",
        "%-22s %-13s %-6s %-12s %5s %8s %8s" % (
            "엣지", "유니버스", "지평", "구성", "시도", "DSR", "IC t"),
        "-" * 82,
    ]
    for k, c in sorted(grid.cells.items(),
                       key=lambda kv: -(kv[1].best_ic_t if kv[1].best_ic_t
                                        is not None else -99)):
        f = lambda v, w=8: ("%*.3f" % (w, v)) if v is not None else "%*s" % (w, "-")
        lines.append("%-22s %-13s %-6s %-12s %5d %s %s%s"
                     % (k[0][:22], k[1][:13], k[2], k[3], c.trials,
                        f(c.best_dsr), f(c.best_ic_t),
                        "  살아있음" if c.alive else ""))
    empt = empty_cells(grid)
    lines += ["", f"빈 칸 {len(empt)}개 - **탐색 압력**. 앞의 {top_empty}개:"]
    for k in empt[:top_empty]:
        lines.append("  " + " · ".join(k))
    lines += [
        "",
        "▶ **유효 N 이 총 시도보다 작다.** 같은 칸의 변형은 상관되므로 독립",
        "  시도로 세면 스스로 문턱을 올린다(DSR 이 그만큼 깎인다).",
        "▶ 빈 칸은 신규성이다. 그중 **무엇이 흥미로운가**는 네가 판단한다 -",
        "  모든 새로운 것이 흥미롭지는 않다.",
    ]
    return "\n".join(lines)


# ── 자체 점검 ────────────────────────────────────────────────────────────────

def _check_correlated_variants_are_one_trial():
    """**같은 칸의 변형은 유효 1회다.** (2026-08-12 - 유효 N 의 핵심)

    lookback 20/25/30 은 서로 ~90% 상관인데 지금은 3회로 센다. 그러면
    DSR 기준선이 3회분 올라가 스스로 문턱을 높인다.
    """
    g = Grid()
    for lb in (20, 25, 30):
        k = cell_of("momentum", "krx_all", {"lookback_days": lb})
        g.cells.setdefault(k, Cell(key=k)).trials += 1
    assert g.total_trials == 3, g.total_trials
    assert g.effective_n == 1, f"상관된 변형을 {g.effective_n}회로 셌다"

    # 지평 버킷이 다르면 다른 칸이다 - 5일과 120일은 다른 베팅이다
    k2 = cell_of("momentum", "krx_all", {"lookback_days": 5})
    g.cells.setdefault(k2, Cell(key=k2)).trials += 1
    assert g.effective_n == 2, g.effective_n
    print("  상관 변형 = 유효 1회      OK")


def _check_construction_is_an_axis():
    """**구성이 다르면 다른 칸이다.** (2026-08-12 IC 실측이 가르쳐 준 것)

    breakout 은 IC +0.0372 로 신호가 살아 있는데 백테스트는 -168.77%p 였다.
    구성을 축에 안 넣으면 "신호가 나쁘다" 와 "구성이 나쁘다" 가 섞인다.
    """
    base = {"lookback_days": 20}
    a = cell_of("breakout", "krx_all", base)
    b = cell_of("breakout", "krx_all", dict(base, max_drawdown_stop=-0.28))
    c = cell_of("breakout", "krx_all", dict(base, vol_target_annual=0.15))
    d = cell_of("breakout", "krx_all", dict(base, max_drawdown_stop=-0.28,
                                            vol_target_annual=0.15))
    assert len({a, b, c, d}) == 4, "구성이 달라도 같은 칸으로 봤다"
    assert a[3] == "무위험관리" and d[3] == "둘다", (a[3], d[3])
    print("  구성이 축이다             OK")


def _check_breadth_counts_only_live_signals():
    """**폭은 살아 있는 칸만 센다.** 진 칸을 폭에 넣으면 IR 예측이 거짓이 된다."""
    g = Grid()
    live = Cell(key=("a", "krx_all", "중기", "무위험관리"), trials=1,
                best_ic_t=2.4, best_excess=-168.77)
    dead = Cell(key=("b", "krx_all", "중기", "무위험관리"), trials=1,
                best_ic_t=-7.09, best_excess=855.92)
    g.cells = {live.key: live, dead.key: dead}
    assert g.effective_n == 2 and g.breadth == 1, (g.effective_n, g.breadth)
    # **IC 가 있으면 IC 를 믿는다** - 오늘 백테스트가 데이터 결함에 속았다.
    #   초과 +855.92%p 인데 IC t -7.09 인 칸은 죽은 칸이다.
    assert not dead.alive, "데이터 결함이 만든 초과수익을 살아있다고 봤다"
    assert live.alive, "IC 가 양수인데 초과수익만 보고 죽었다고 봤다"
    print("  폭은 살아있는 칸만        OK")


def _check_empty_cells_come_from_vocabulary():
    """빈 칸은 **통제 어휘**에서 나온다. 어휘 밖을 제안하면 접수에서 막힌다."""
    g = Grid()
    k = cell_of("momentum", "krx_all", {"lookback_days": 20})
    g.cells[k] = Cell(key=k, trials=1)
    empt = empty_cells(g, edges=["momentum", "breakout"],
                       universes=["krx_all", "low_turnover"])
    # 2 엣지 × 2 유니버스 × 3 지평 × 4 구성 = 48, 그중 하나가 찼다
    assert len(empt) == 47, len(empt)
    assert k not in empt, "찬 칸을 빈 칸으로 냈다"
    assert all(len(x) == 4 for x in empt)
    print("  빈 칸 = 어휘 조합         OK")


def _check_unmeasured_is_not_dead():
    """**못 잰 칸을 죽은 칸으로 세지 않는다.** 미측정과 실패는 다르다."""
    c = Cell(key=("x", "krx_all", "중기", "무위험관리"), trials=1)
    assert not c.alive          # 재료가 없으면 살아있다고 못 한다
    assert c.best_ic_t is None and c.best_dsr is None
    d = c.as_dict()
    assert d["best_ic_t"] is None, "미측정을 값으로 채웠다"
    assert horizon_bucket(None) == "?" and horizon_bucket("이상") == "?"
    assert render(Grid()) == "격자가 비어 있다. (지어내지 않았다 - 완주한 실험이 없다)"
    print("  미측정 != 죽음            OK")


def _selfcheck() -> int:
    print(f"{GRID_VERSION} 자체 점검 (DB 없음)")
    _check_correlated_variants_are_one_trial()
    _check_construction_is_an_axis()
    _check_breadth_counts_only_live_signals()
    _check_empty_cells_come_from_vocabulary()
    _check_unmeasured_is_not_dead()
    print("행동 격자 5개 영역 통과.")
    return 0


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="행동 격자")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.check:
        return _selfcheck()

    import json

    import psycopg2
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                           / "01-research" / "collectors"))
    from source_registry import load_project_env
    conn = psycopg2.connect(load_project_env()["DATABASE_URL"], connect_timeout=20)
    try:
        g = build(conn)
    finally:
        conn.close()
    print(json.dumps(g.as_dict(), ensure_ascii=False, indent=2)
          if a.json else render(g))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
