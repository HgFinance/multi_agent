"""전략 생명주기 (Champion–Challenger) - 좋은 백테스트를 운영 전략으로 오인하지 않는다.

담당: 재일 (퀀트·백테스트본부 QNT)
근거: 재일님 계획 2026-08-04 "전략 승격과 Champion–Challenger",
      CLAUDE.md "quant-backtest-department 는 Production 승격을 직접 하지 않는다"

▶ 릴리스 관문 다음이 비어 있었다
  release_gate 가 SUBMIT_TO_QA 까지 판정해도 그 뒤가 없다. 좋은 백테스트가
  나와도 **Shadow 로 갈지, 얼마나 지켜볼지, 나빠지면 어떻게 할지**를 아무도
  정하지 않는다.

  그런데 스키마에는 이미 있었다 - strategy.strategies.status 가
  RESEARCH / SHADOW / PAPER / LIVE_CANDIDATE / LIVE / SUSPENDED / RETIRED 를
  갖고 있는데 **퀀트가 그 테이블을 전혀 안 쓴다**(2026-08-04 실측, 호출처 0개).
  오늘 여섯 번째 같은 패턴이다.

▶ 이 모듈이 절대 하지 않는 것
  승격. Production 승격은 CEO·Risk·QA 권한이고, 퀀트는 **요청만** 만든다.
  release_gate 와 같은 경계이며, 자체점검이 AST 로 승격 함수가 생기지
  않았는지 확인한다.

▶ Champion 과 Challenger 는 같은 조건에서 비교한다
  신규 전략이 기존 Champion 을 이겼다고 말하려면 **같은 기간·같은 비용
  모델·같은 유니버스**여야 한다. 다른 조건에서 나온 숫자를 나란히 두면
  비교가 아니라 착시다.

▶ 나빠지면 자동 강등이 아니라 중단·롤백 **요청**이다
  Shadow/Paper 성적이 기준 아래로 떨어져도 기계가 상태를 내리지 않는다.
  기계가 운영 전략을 끄기 시작하면 한 번의 나쁜 표본이 멀쩡한 전략을
  죽인다. 사실을 드러내고 사람이 판단한다 - 다만 **드러내는 것은 자동이다.**

자체 점검: python departments/04-quant-backtest/pipeline/strategy_lifecycle.py
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

MODULE_VERSION = "quant-strategy-lifecycle-v1"

# strategy.strategies.status 제약과 같은 목록. 어긋나면 UPDATE 가 죽는다.
STATES = ("RESEARCH", "SHADOW", "PAPER", "LIVE_CANDIDATE", "LIVE",
          "SUSPENDED", "RETIRED")

# 퀀트가 **요청**할 수 있는 전이. LIVE 로 가는 마지막 칸은 여기 없다 -
# 그건 CEO·Risk·QA 가 한다.
QUANT_REQUESTABLE = {
    "RESEARCH": ("SHADOW",),
    "SHADOW": ("PAPER", "SUSPENDED", "RETIRED"),
    "PAPER": ("LIVE_CANDIDATE", "SUSPENDED", "RETIRED"),
    # LIVE_CANDIDATE -> LIVE 는 승인 경로다. 퀀트는 중단만 요청할 수 있다.
    "LIVE_CANDIDATE": ("SUSPENDED", "RETIRED"),
    "LIVE": ("SUSPENDED",),
    "SUSPENDED": ("RETIRED",),
    "RETIRED": (),
}

# Shadow/Paper 관찰 최소 기간. 짧으면 그 기간의 시장 국면만 본 것이다.
MIN_OBSERVE_DAYS = {"SHADOW": 20, "PAPER": 40}

# 실전 성적이 백테스트 대비 이만큼 밑돌면 중단을 요청한다.
# 0.5 는 "절반으로 깎였다" 는 뜻이고, 그 정도면 가정이 틀린 것이다.
DEGRADE_RATIO = 0.5

INTRADAY_MIN_SHADOW_EVENTS = 1_000
INTRADAY_MAX_FILL_CALIBRATION_MAE = 0.15
INTRADAY_MIN_LIVE_NET_BPS = 0.0


@dataclass
class LifecycleRequest:
    """전이 **요청**. 승격이 아니다."""
    strategy_code: str
    from_state: str
    to_state: str
    approved_by_quant: bool
    reasons: list = field(default_factory=list)
    needs_approval_from: str = ""

    def as_dict(self) -> dict:
        return {
            "strategy_code": self.strategy_code,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "requested_by": "quant-backtest-department",
            "reasons": list(self.reasons),
            "needs_approval_from": self.needs_approval_from,
            "not_a_promotion":
                "퀀트는 전이를 **요청**만 한다. 상태 변경은 승인 뒤에 "
                "일어나며 LIVE 승격은 CEO·Risk·QA 권한이다",
        }


def can_request(frm: str, to: str) -> bool:
    """퀀트가 이 전이를 요청할 수 있는가. **LIVE 는 못 한다.**"""
    return to in QUANT_REQUESTABLE.get((frm or "").strip().upper(), ())


def evaluate_promotion(strategy_code: str, *, current_state: str,
                       gate_decision: str, observed_days: int = 0,
                       live_sharpe: float | None = None,
                       backtest_sharpe: float | None = None,
                       research_lane: str = "DAILY_CROSS_SECTIONAL",
                       execution_evidence: dict | None = None) -> LifecycleRequest:
    """다음 칸으로 갈 자격이 있는가. **판정이지 실행이 아니다.**"""
    frm = (current_state or "").strip().upper()
    reasons: list[str] = []

    if frm not in STATES:
        return LifecycleRequest(strategy_code, frm, "", False,
                                [f"알 수 없는 상태: {frm!r}"])

    # 성적 열화는 어느 단계에서든 중단 요청이 우선한다
    if (live_sharpe is not None and backtest_sharpe is not None
            and backtest_sharpe > 0
            and live_sharpe < backtest_sharpe * DEGRADE_RATIO):
        if can_request(frm, "SUSPENDED"):
            return LifecycleRequest(
                strategy_code, frm, "SUSPENDED", True,
                [f"실전 Sharpe {live_sharpe} < 백테스트 {backtest_sharpe} 의 "
                 f"{DEGRADE_RATIO:.0%} - 가정이 틀렸을 가능성이 크다. "
                 f"**자동 강등이 아니라 중단 요청이다**"],
                needs_approval_from="Risk + CEO")

    nxt = {"RESEARCH": "SHADOW", "SHADOW": "PAPER",
           "PAPER": "LIVE_CANDIDATE"}.get(frm)
    if nxt is None:
        return LifecycleRequest(strategy_code, frm, "", False,
                                [f"{frm} 에서 퀀트가 요청할 승격 경로가 없다"])

    if gate_decision != "SUBMIT_TO_QA":
        reasons.append(f"릴리스 관문이 {gate_decision} 다 - SUBMIT_TO_QA 가 아니면 "
                       f"다음 칸으로 못 간다")

    need = MIN_OBSERVE_DAYS.get(frm)
    if need is not None and observed_days < need:
        # ▶ 짧은 관찰은 그 기간의 시장 국면만 본 것이다. 통과시키면
        #   "한 달 좋았다" 를 실력으로 읽게 된다.
        reasons.append(f"{frm} 관찰 {observed_days}일 < 최소 {need}일")

    # Event-time edge usually dies at the queue/latency boundary.  Progress from
    # SHADOW/PAPER is therefore fail-closed on execution calibration, not Sharpe.
    if (research_lane or "").upper() == "INTRADAY_EVENT" and frm in {"SHADOW", "PAPER"}:
        evidence = execution_evidence or {}
        required = ("observed_events", "mean_live_net_bps", "latency_p95_ms",
                    "registered_latency_ms")
        missing = [key for key in required if evidence.get(key) is None]
        if missing:
            reasons.append(f"intraday execution evidence missing: {missing}")
        else:
            if int(evidence["observed_events"]) < INTRADAY_MIN_SHADOW_EVENTS:
                reasons.append(
                    f"intraday observed_events {evidence['observed_events']} < "
                    f"{INTRADAY_MIN_SHADOW_EVENTS}")
            if float(evidence["mean_live_net_bps"]) <= INTRADAY_MIN_LIVE_NET_BPS:
                reasons.append("intraday live net edge is not positive")
            if float(evidence["latency_p95_ms"]) > float(evidence["registered_latency_ms"]):
                reasons.append("intraday p95 latency exceeds preregistered latency")
        if str(evidence.get("execution") or "").upper() == "PASSIVE_FIFO_LOWER_BOUND":
            fill_error = evidence.get("fill_calibration_mae")
            if fill_error is None:
                reasons.append("passive fill calibration is unmeasured")
            elif float(fill_error) > INTRADAY_MAX_FILL_CALIBRATION_MAE:
                reasons.append(
                    f"passive fill calibration MAE {fill_error} > "
                    f"{INTRADAY_MAX_FILL_CALIBRATION_MAE}")

    ok = not reasons
    return LifecycleRequest(
        strategy_code, frm, nxt, ok,
        reasons or [f"{frm} 기준 충족 - {nxt} 전이를 요청한다"],
        needs_approval_from=("AI QA + Risk" if nxt != "LIVE_CANDIDATE"
                             else "AI QA + Risk -> CEO") if ok else "")


def compare(champion: dict, challenger: dict) -> dict:
    """Champion vs Challenger. **같은 조건이 아니면 비교하지 않는다.**

    기간·비용 모델·유니버스가 다르면 나란히 둔 숫자는 비교가 아니라 착시다.
    """
    keys = ("period", "cost_model_version", "universe_version")
    mismatch = [k for k in keys
                if str(champion.get(k) or "") != str(challenger.get(k) or "")]
    if mismatch:
        return {"comparable": False, "mismatched": mismatch,
                "reason": f"조건이 다르다({', '.join(mismatch)}) - 다른 조건의 "
                          f"숫자를 나란히 두면 비교가 아니라 착시다"}

    cs, hs = champion.get("sharpe"), challenger.get("sharpe")
    if cs is None or hs is None:
        return {"comparable": False,
                "reason": "한쪽 Sharpe 가 미확인 - 없는 것과 나쁜 것은 다르다"}
    return {
        "comparable": True,
        "champion_sharpe": cs, "challenger_sharpe": hs,
        "challenger_wins": hs > cs,
        # 이겼다고 교체가 아니다. 교체는 승인 경로다.
        "note": "우열은 사실이고 교체는 결정이다 - 결정은 CEO·Risk·QA 가 한다",
    }


# ── 자체 점검 ────────────────────────────────────────────────────────────────

def _check_quant_cannot_request_live():
    """**퀀트는 LIVE 를 요청조차 못 한다.** 승격은 CEO·Risk·QA 권한이다."""
    for frm in STATES:
        assert not can_request(frm, "LIVE"), frm
    # 중단·폐기는 어느 단계에서든 요청할 수 있다(안전 방향)
    assert can_request("LIVE", "SUSPENDED")
    assert can_request("SHADOW", "SUSPENDED")


def _check_gate_must_pass():
    r = evaluate_promotion("S1", current_state="RESEARCH",
                           gate_decision="HOLD")
    assert r.approved_by_quant is False, r
    assert any("SUBMIT_TO_QA" in x for x in r.reasons), r.reasons


def _check_observation_minimum():
    """짧은 관찰은 그 기간의 국면만 본 것이다."""
    short = evaluate_promotion("S1", current_state="SHADOW",
                               gate_decision="SUBMIT_TO_QA", observed_days=5)
    assert short.approved_by_quant is False and "최소" in short.reasons[0], short
    ok = evaluate_promotion("S1", current_state="SHADOW",
                            gate_decision="SUBMIT_TO_QA", observed_days=25)
    assert ok.approved_by_quant is True and ok.to_state == "PAPER", ok
    assert "QA" in ok.needs_approval_from


def _check_degradation_requests_suspend_not_demote():
    """**자동 강등이 아니라 중단 요청이다.**

    기계가 운영 전략을 끄기 시작하면 한 번의 나쁜 표본이 멀쩡한 전략을 죽인다.
    """
    r = evaluate_promotion("S1", current_state="PAPER",
                           gate_decision="SUBMIT_TO_QA", observed_days=60,
                           live_sharpe=0.3, backtest_sharpe=1.2)
    assert r.to_state == "SUSPENDED", r
    assert "중단 요청" in r.reasons[0], r.reasons
    assert "Risk" in r.needs_approval_from
    # 열화가 아니면 정상 승격 요청
    ok = evaluate_promotion("S1", current_state="PAPER",
                            gate_decision="SUBMIT_TO_QA", observed_days=60,
                            live_sharpe=1.1, backtest_sharpe=1.2)
    assert ok.to_state == "LIVE_CANDIDATE" and ok.approved_by_quant, ok


def _check_compare_requires_same_conditions():
    """조건이 다르면 비교하지 않는다 - 나란히 둔 숫자는 착시다."""
    base = {"period": "2024-2026", "cost_model_version": "krx-cost-v2",
            "universe_version": "v2", "sharpe": 1.0}
    diff = dict(base, cost_model_version="krx-cost-v1", sharpe=1.9)
    r = compare(base, diff)
    assert r["comparable"] is False and "cost_model_version" in r["mismatched"]

    same = dict(base, sharpe=1.4)
    r2 = compare(base, same)
    assert r2["comparable"] is True and r2["challenger_wins"] is True
    # 이겼다고 교체가 아니다
    assert "결정은" in r2["note"]

    # 한쪽이 미확인이면 비교 불가 - 없는 것과 나쁜 것은 다르다
    assert compare(base, dict(base, sharpe=None))["comparable"] is False


def _check_unknown_state_is_not_guessed():
    r = evaluate_promotion("S1", current_state="듣보",
                           gate_decision="SUBMIT_TO_QA")
    assert r.approved_by_quant is False and "알 수 없는" in r.reasons[0], r


def _check_no_promotion_path_in_code():
    """**승격 함수가 생기지 않았는가.** release_gate 와 같은 경계다."""
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    banned = {"promote", "activate", "approve_"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith(("_check_", "test_")):
                continue          # 검사 자신은 제외(자기 이름에 걸리면 무의미)
            assert not any(b in node.name.lower() for b in banned), node.name
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = ([a.name for a in node.names]
                    if isinstance(node, ast.Import) else [node.module or ""])
            assert not any("psycopg2" in (m or "") for m in mods), \
                "DB 연결이 생겼다 - 이 모듈은 요청만 만들고 상태를 쓰지 않는다"


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
    _check_quant_cannot_request_live();  print("  LIVE 요청 불가          OK")
    _check_gate_must_pass();             print("  관문 미통과 차단        OK")
    _check_observation_minimum();        print("  최소 관찰 기간          OK")
    _check_degradation_requests_suspend_not_demote(); print("  열화=중단'요청'        OK")
    _check_compare_requires_same_conditions(); print("  동일 조건 비교          OK")
    _check_unknown_state_is_not_guessed(); print("  미지 상태 추측 안 함     OK")
    _check_no_promotion_path_in_code();  print("  승격 경로 부재          OK")
    print("전략 생명주기 7개 영역 통과.")
