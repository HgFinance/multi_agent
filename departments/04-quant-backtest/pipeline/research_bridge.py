"""리서치 -> 퀀트 다리 - 인사이트를 가설 씨앗으로 넘긴다.

담당: 재일 (퀀트·백테스트본부 QNT)
근거: 재일님 지시 2026-08-04 "리서치 퀀트 부서 배선 연결"

▶ 왜 필요한가
  두 본부가 따로 놀고 있었다. 리서치는 매일 Packet 을 내고 인사이트를
  발행하는데(2026-08-04 기준 packet_claims 234건 중 INSIGHT 25건), 퀀트는
  그것을 전혀 안 본다. 퀀트 가설은 시장 관측(폭·SMA)만으로 만들어진다.

  리서치 인사이트는 이미 **가설이 요구하는 모양**을 갖고 있다 -
  반증 조건(falsification_note), 판정 지평(horizon_days), 근거 축(source_node).
  없는 것을 만들 필요가 없고 형태를 옮기면 된다.

▶ 넘기는 것은 **씨앗이지 가설이 아니다**
  인사이트는 "005930 이 지금 이런 상태다" 는 **한 종목·한 시점** 관측이다.
  전략 가설은 "이런 조건이면 어느 종목이든" 이라는 **일반 규칙**이다.
  둘은 다르고, 그 간극을 메우는 것은 QNT-01 의 일이다.

  그래서 이 모듈은 HypothesisSpec 을 만들지 않는다. 씨앗(seed)만 만들고
  일반화가 필요하다는 사실을 명시한다 - 한 종목 관측을 그대로 전략으로
  삼으면 표본 1개짜리 백테스트가 된다.

▶ 채점 성적은 아직 못 쓴다
  research.packet_outcomes 가 **0행**이다(5·20일 지평 미도래). 성적 기반
  선별은 지평이 지난 뒤에 붙인다 - 없는 성적을 0 으로 채우면 모든 인사이트가
  똑같이 나쁜 것이 된다.

▶ PIT
  오늘 인사이트로 2024 년부터 백테스트하면 우리 자신의 사후 관측을 과거에
  넣는 셈이다. strategy_scout 의 컨셉 차용과 같은 성격이라 **씨앗에
  observed_at 을 실어** 그 사실이 남게 한다.

자체 점검: python departments/04-quant-backtest/pipeline/research_bridge.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

BRIDGE_VERSION = "quant-research-bridge-v1"

# 한 번에 넘길 씨앗 수. 많이 넘기면 QNT-01 이 소화 못 하고, 소화 못 한
# 씨앗이 쌓이면 그 자체가 소음이다.
MAX_SEEDS = 5

# 인사이트 종류별로 전략화 가능성이 다르다. 여기 없는 종류는 넘기지 않는다 -
# "무엇이든 전략이 될 수 있다" 고 두면 걸러지지 않는다.
TRADABLE_KINDS = {
    # 서로 다른 축이 같은/반대 방향 -> 조건부 진입 규칙으로 옮길 수 있다
    "INSIGHT_CROSS_SIGNAL": "cross_signal",
    "INSIGHT_DIVERGENCE": "divergence",
    # 국면 판정 -> 레짐 조건부 전략
    "INSIGHT_REGIME_READ": "regime_conditional",
    # 상하방 비대칭 -> 포지션 사이징 규칙
    "INSIGHT_RISK_ASYMMETRY": "asymmetry",
    # CAUSAL_HYPOTHESIS 는 뺀다 - 인과 주장은 한 사례로 검증할 수 없고,
    # 전략으로 옮기면 사후 서사를 규칙으로 굳히게 된다.
}


@dataclass
class HypothesisSeed:
    """가설 씨앗. **HypothesisSpec 이 아니다** - 일반화가 남아 있다."""
    source_claim_kind: str
    edge_type: str
    symbol: str
    claim_text: str
    falsifier: str
    horizon_days: int
    source_nodes: str
    observed_at: str
    needs_generalization: bool = True
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "source": "research.packet_claims",
            "source_claim_kind": self.source_claim_kind,
            "edge_type": self.edge_type,
            "symbol": self.symbol,
            "claim_text": self.claim_text,
            "falsifier": self.falsifier,
            "horizon_days": self.horizon_days,
            "source_nodes": self.source_nodes,
            "observed_at": self.observed_at,
            # ▶ 이 두 줄이 씨앗을 가설로 오해하지 못하게 막는다
            "needs_generalization": self.needs_generalization,
            "not_a_hypothesis":
                "한 종목·한 시점 관측이다. 어느 종목이든 성립하는 규칙으로 "
                "옮기는 것은 QNT-01 의 일이며, 그대로 백테스트하면 표본 "
                "1개짜리 실험이 된다",
            "notes": list(self.notes),
        }


def to_seeds(claims: Iterable[dict], *, max_seeds: int = MAX_SEEDS,
             now: datetime | None = None) -> tuple[list[HypothesisSeed], list[dict]]:
    """packet_claims 행 -> (씨앗, 거부+사유).

    **조용히 버리지 않는다** - 왜 안 넘어왔는지 보여야 리서치 쪽을 고친다.
    """
    seeds: list[HypothesisSeed] = []
    rejected: list[dict] = []
    for c in claims or []:
        kind = str(c.get("kind") or "")
        if kind not in TRADABLE_KINDS:
            rejected.append({"kind": kind,
                             "reason": "전략화 대상 종류가 아니다"})
            continue
        fals = str(c.get("falsification_note") or "").strip()
        if not fals:
            # 반증 조건이 없으면 검증할 수 없다 - 인사이트 계약이 이미
            # 요구하는 것이므로 없다는 것은 다른 경로로 들어온 행이다
            rejected.append({"kind": kind, "reason": "반증 조건이 없다"})
            continue
        horizon = int(c.get("horizon_days") or 0)
        if horizon < 1:
            rejected.append({"kind": kind, "reason": "판정 지평이 없다"})
            continue
        seeds.append(HypothesisSeed(
            source_claim_kind=kind,
            edge_type=TRADABLE_KINDS[kind],
            symbol=str(c.get("symbol") or ""),
            claim_text=str(c.get("threshold_text") or "")[:300],
            falsifier=fals[:300],
            horizon_days=horizon,
            source_nodes=str(c.get("source_node") or ""),
            observed_at=str(c.get("created_at") or c.get("as_known_at") or ""),
        ))

    # 지평이 짧은 것부터 - 빨리 채점되는 것이 먼저 배운다
    seeds.sort(key=lambda s: (s.horizon_days, s.symbol))
    if len(seeds) > max_seeds:
        # ▶ 잘라낸 사실을 남긴다. 조용히 자르면 "이게 전부" 로 읽힌다.
        rejected.append({"kind": "-",
                         "reason": f"상한 {max_seeds} 초과로 "
                                   f"{len(seeds) - max_seeds}건 보류"})
        seeds = seeds[:max_seeds]
    return seeds, rejected


def fetch_insight_claims(conn, *, limit: int = 50) -> list[dict]:
    """리서치 인사이트 주장을 읽는다. **읽기 전용이다.**

    퀀트는 리서치 테이블에 쓰지 않는다 - 쓰기는 리서치만 한다.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select kind, symbol, threshold_text, falsification_note,
                   horizon_days, source_node, created_at
            from research.packet_claims
            where kind like 'INSIGHT%%'
            order by created_at desc
            limit %s
            """, (limit,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def bridge(conn=None, *, limit: int = 50) -> dict:
    """리서치 인사이트 -> 퀀트 씨앗. 실패는 사유를 남긴다."""
    own = conn is None
    try:
        if own:
            import psycopg2

            sys.path.insert(0, str(__import__("pathlib").Path(__file__)
                                   .resolve().parents[2] / "01-research" / "collectors"))
            from source_registry import load_project_env

            conn = psycopg2.connect(load_project_env()["DATABASE_URL"],
                                    connect_timeout=20)
        claims = fetch_insight_claims(conn, limit=limit)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"인사이트 조회 실패: {type(e).__name__}",
                "seeds": []}
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    seeds, rejected = to_seeds(claims)
    return {
        "ok": True,
        "read": len(claims),
        "seeds": [s.as_dict() for s in seeds],
        "rejected": rejected,
        # 채점 성적은 아직 못 쓴다 - 그 사실을 숨기지 않는다
        "scoring_available": False,
        "scoring_note": "research.packet_outcomes 가 아직 비어 있다"
                        "(5·20일 지평 미도래) - 성적 기반 선별은 그 뒤에 붙인다",
    }


# ── 자체 점검 ────────────────────────────────────────────────────────────────

def _claim(kind="INSIGHT_CROSS_SIGNAL", horizon=5, fals="5일 내 A 가 B 를 넘으면 틀림",
           symbol="005930", **kw):
    d = {"kind": kind, "symbol": symbol, "threshold_text": "교차 해석 문장",
         "falsification_note": fals, "horizon_days": horizon,
         "source_node": "technical,regime", "created_at": "2026-08-04T00:00:00Z"}
    d.update(kw)
    return d


def _check_only_tradable_kinds():
    """전략화 가능한 종류만 넘긴다 - '무엇이든 전략' 이면 안 걸러진다."""
    seeds, rej = to_seeds([
        _claim(kind="INSIGHT_CROSS_SIGNAL"),
        _claim(kind="INSIGHT_CAUSAL_HYPOTHESIS"),   # 인과 주장은 제외
        _claim(kind="PRICE_RALLY"),                 # 인사이트가 아니다
    ])
    assert len(seeds) == 1 and seeds[0].edge_type == "cross_signal", seeds
    # ▶ 조용히 버리지 않는다 - 왜 안 넘어왔는지 보여야 고친다
    assert len(rej) == 2 and all(r["reason"] for r in rej), rej


def _check_falsifier_and_horizon_required():
    """반증 조건·지평이 없으면 검증할 수 없다."""
    _, rej = to_seeds([_claim(fals=""), _claim(horizon=0)])
    reasons = [r["reason"] for r in rej]
    assert any("반증" in r for r in reasons), reasons
    assert any("지평" in r for r in reasons), reasons


def _check_seed_is_not_a_hypothesis():
    """**씨앗을 가설로 오해하지 못하게** 하는 문장이 실려 있는가.

    한 종목·한 시점 관측을 그대로 백테스트하면 표본 1개짜리 실험이 된다.
    """
    seeds, _ = to_seeds([_claim()])
    d = seeds[0].as_dict()
    assert d["needs_generalization"] is True
    assert "표본" in d["not_a_hypothesis"] and "QNT-01" in d["not_a_hypothesis"]
    # PIT - 언제 관측된 것인지 남는다
    assert d["observed_at"], d


def _check_short_horizon_first():
    """빨리 채점되는 것이 먼저 배운다."""
    seeds, _ = to_seeds([_claim(horizon=20), _claim(horizon=3), _claim(horizon=10)])
    assert [s.horizon_days for s in seeds] == [3, 10, 20], seeds


def _check_cap_is_reported():
    """잘라낸 사실을 남긴다 - 조용히 자르면 '이게 전부' 로 읽힌다."""
    seeds, rej = to_seeds([_claim(symbol=f"00000{i}") for i in range(9)],
                          max_seeds=3)
    assert len(seeds) == 3
    assert any("보류" in r["reason"] for r in rej), rej


def _check_failure_is_not_empty():
    """조회 실패를 '씨앗 0건' 으로 위장하지 않는다."""
    class _Boom:
        def cursor(self):
            raise OSError("연결 거부")

    r = bridge(conn=_Boom())
    assert r["ok"] is False and "실패" in r["reason"], r
    assert r["seeds"] == []


def _check_scoring_gap_is_declared():
    """채점이 아직 없다는 사실을 숨기지 않는다."""
    class _Empty:
        def cursor(self):
            class C:
                description = [("kind",), ("symbol",), ("threshold_text",),
                               ("falsification_note",), ("horizon_days",),
                               ("source_node",), ("created_at",)]
                def execute(self, *a, **k): pass
                def fetchall(self): return []
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return C()

    r = bridge(conn=_Empty())
    assert r["ok"] is True and r["scoring_available"] is False
    assert "지평 미도래" in r["scoring_note"]


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{BRIDGE_VERSION} 자체 점검 (DB 없음)")
    _check_only_tradable_kinds();        print("  전략화 종류만 통과       OK")
    _check_falsifier_and_horizon_required(); print("  반증·지평 필수          OK")
    _check_seed_is_not_a_hypothesis();   print("  씨앗 != 가설            OK")
    _check_short_horizon_first();        print("  짧은 지평 우선          OK")
    _check_cap_is_reported();            print("  상한 초과 보고          OK")
    _check_failure_is_not_empty();       print("  조회 실패 != 0건        OK")
    _check_scoring_gap_is_declared();    print("  채점 공백 명시          OK")
    print("리서치 다리 7개 영역 통과.")
