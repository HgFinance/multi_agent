#!/usr/bin/env python3
"""회계 질의 Level 분류와 모델 등급 배정 — 비용·성능 최적화.

소유: 도현 (회계/포트폴리오본부)
근거: docs/HEDGE_FUND_MASTER_PLAN.md 5.9(결정론 계층), 12.3
      docs/02-engineering/WORKER_MODEL_MATRIX.md (직원 모델 변경 절차)
      CLAUDE.local.md 원칙 5, apps/api/accounting.py (부서 Agent 질의 경로)

회계 질의는 난이도 편차가 크다. "지금 NAV 얼마"와 "이번 달 Break 가 왜 반복되나"를
같은 값으로 태우면 한쪽은 낭비고 한쪽은 부족하다. 그래서 질의를 네 단계로 나눈다.

  L0 결정론 조회   모델을 **아예 안 부른다.** 원장·스냅샷에서 그대로 읽으면 되는 질의다
  L1 단일 사실 서술 확정된 수치 하나를 문장으로 옮기는 수준
  L2 다중 홉 대조   여러 원천을 연결해 설명해야 한다(Break 원인, 기간 비교, 귀속 분해)
  L3 마감·감사 서술 틀리면 되돌리기 어려운 서술

**가장 큰 절감은 L0 다.** 제일 싼 모델은 안 부르는 모델이고, 덤으로 원장 수치가
LLM 문장을 거치지 않으니 원칙 5(회계 수치를 LLM 문장에서 확정하지 않는다)도 같이 지켜진다.

**분류는 결정론이다.** LLM 에게 난이도를 물어보면 난이도 판정에 또 모델을 태우게 된다.
키워드 규칙은 accounting_ops.yaml 에 있고 높은 레벨이 먼저 맞으면 그 레벨로 확정한다 —
"마감 NAV 얼마"는 L0 이 아니라 L3 이다(마감 맥락이 조회 의도를 이긴다).

**직원 Worker 모델은 여기서 못 바꾼다.** 이 라우팅은 부서장 Hermes 질의 경로 전용이다.
Worker 모델은 `employee_runtime` 이 소유하고 WORKER_MODEL_MATRIX.md 의 절차
(benchmark -> HR 제안 -> QA 검증 -> CEO 승인)를 거쳐야 바뀐다.

**지식 그래프는 만들지 않는다.** L2 가 관계형(SQL·원장 조인)으로 안 풀린다는 것이
실측될 때만 논의를 연다. `record_relational_miss()` / `knowledge_graph_readiness()` 가
그 계측이고, 조건 충족 전에는 저장소를 하나 더 늘리지 않는다.

자체 점검: python departments/05-accounting-portfolio/query_router.py
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

_HERE = Path(__file__).resolve().parent

import yaml

OPS_PATH = _HERE / "accounting_ops.yaml"
# L2 관계형 실패 관측 로그. 지식 그래프 도입 판단의 유일한 근거다.
# ponytail: 파일 append 로 충분하다. 이 계측이 실제로 트리거를 넘기면 그때 테이블로 옮긴다.
MISS_LOG = _HERE / "reports" / "l2_relational_misses.jsonl"

# 규칙 평가 순서. 위에서 맞으면 아래는 보지 않는다.
#   L3·L2 가 먼저인 이유: 마감/원인 같은 **맥락**은 조회 의도를 이긴다.
#     "마감 NAV 얼마"는 조회가 아니라 마감 판단이다.
#   L0 이 L1 보다 먼저인 이유: L1 키워드(알려/설명/요약)는 난이도가 아니라 **말투**다.
#     "NAV 알려줘"는 정중하게 물어본 조회일 뿐이므로 모델을 태울 이유가 없다.
#     지목된 데이터 명사가 일반 동사보다 강한 신호다.
LEVEL_ORDER = ("L3", "L2", "L0", "L1")
DEFAULT_LEVEL = "L2"   # 어디에도 안 걸리면 싼 쪽이 아니라 중간으로 간다(모르면 덜 깎는다)


class QueryRoutingError(Exception):
    """질의를 라우팅할 수 없는 경우. 임의 등급으로 태우지 않는다."""


@dataclass(frozen=True)
class Routing:
    level: str
    level_name: str
    tier: str
    model: str | None
    calls_model: bool
    matched: tuple[str, ...]
    reason: str
    # L0 는 모델 대신 여기를 읽는다.
    deterministic_source: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level, "level_name": self.level_name, "tier": self.tier,
            "model": self.model, "calls_model": self.calls_model,
            "matched_keywords": list(self.matched), "reason": self.reason,
            "deterministic_source": self.deterministic_source,
            "decided_by": "deterministic",
            # 라우팅은 답이 아니다 - 소비자가 이걸 회계 수치로 오해하지 않게 박는다.
            "authoritative": False,
        }


def load_ops(path: Path = OPS_PATH) -> dict[str, Any]:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise QueryRoutingError(f"운영 튜닝 파일을 읽을 수 없습니다: {path}") from exc
    block = (doc or {}).get("query_levels")
    if not block:
        raise QueryRoutingError(f"{path} 에 query_levels 블록이 없습니다. 튜닝값을 코드에 두지 않습니다")
    return block


# L0 질의가 읽어야 할 결정론 원천. 모델을 부르는 대신 여기로 보낸다.
DETERMINISTIC_SOURCE = (
    "GET /ui/snapshot 또는 accounting-api 읽기 뷰(api.positions / "
    "accounting.portfolio_snapshots). 수치는 원장에서만 나온다"
)


def classify(query: str, *, ops: Mapping[str, Any] | None = None) -> Routing:
    """질의 하나의 Level 과 모델 등급을 정한다. LLM 을 부르지 않는다."""
    if not str(query or "").strip():
        raise QueryRoutingError("빈 질의는 라우팅하지 않습니다")
    ops = ops if ops is not None else load_ops()
    levels, rules = ops["levels"], ops["rules"]
    tiers = ops["model_tiers"]
    lowered = str(query).lower()

    level, matched = DEFAULT_LEVEL, ()
    for candidate in LEVEL_ORDER:
        words = tuple(w for w in (rules.get(candidate, {}).get("keywords") or [])
                      if str(w).lower() in lowered)
        if words:
            level, matched = candidate, words
            break

    if level not in levels:
        raise QueryRoutingError(f"levels 에 없는 등급입니다: {level}")
    spec = levels[level]
    tier = str(spec["tier"])
    if tier not in tiers:
        raise QueryRoutingError(f"model_tiers 에 없는 등급입니다: {tier}")
    model = tiers[tier]
    reason = (f"{spec['name']} — {spec['why']}"
              + (f" (일치: {', '.join(matched)})" if matched
                 else " (일치 키워드 없음 - 기본 등급)"))
    return Routing(
        level=level, level_name=str(spec["name"]), tier=tier, model=model,
        calls_model=model is not None, matched=matched, reason=reason,
        deterministic_source=DETERMINISTIC_SOURCE if model is None else None,
    )


def routing_note(routing: Routing) -> str:
    """호출자에게 붙일 한 줄. 왜 이 등급인지가 응답에 남아야 감사에서 설명된다."""
    if not routing.calls_model:
        return (f"[{routing.level}] 모델 호출 없음 — {routing.reason}. "
                f"원천: {routing.deterministic_source}")
    return f"[{routing.level}] tier={routing.tier} model={routing.model} — {routing.reason}"


# ── 지식 그래프 도입 판단용 계측 (그래프를 만들지 않는다) ──────────────────
def record_relational_miss(query: str, *, resolved_relationally: bool, detail: str = "",
                           now: datetime | None = None, path: Path = MISS_LOG,
                           ops: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """L2 질의가 관계형으로 풀렸는지 한 줄 남긴다. L2 가 아니면 기록하지 않는다.

    **이 로그는 회계 기록이 아니다.** 지식 그래프를 만들지 말지 판단할 운영 계측이며,
    금액·NAV 를 담지 않는다(질의 원문과 성패만).
    """
    routing = classify(query, ops=ops)
    if routing.level != "L2":
        return None
    row = {
        "at": (now or datetime.now(timezone.utc)).isoformat(),
        "query": str(query)[:500],
        "resolved_relationally": bool(resolved_relationally),
        "detail": str(detail)[:500],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def _load_misses(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def knowledge_graph_readiness(observations: Iterable[Mapping[str, Any]] | None = None, *,
                              path: Path = MISS_LOG,
                              ops: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """지식 그래프를 논의할 조건이 실측됐는가. **만들라는 신호가 아니라 논의 개시 신호다.**"""
    ops = ops if ops is not None else load_ops()
    trigger = ops["knowledge_graph_trigger"]
    rows = list(observations) if observations is not None else _load_misses(path)
    total = len(rows)
    missed = sum(1 for r in rows if not r.get("resolved_relationally"))
    ratio = (missed / total) if total else 0.0
    enough = total >= int(trigger["min_samples"])
    over = ratio > float(trigger["relational_miss_ratio"])
    return {
        "samples": total, "relational_misses": missed, "miss_ratio": round(ratio, 4),
        "min_samples": int(trigger["min_samples"]),
        "threshold": float(trigger["relational_miss_ratio"]),
        "enough_samples": enough,
        "threshold_exceeded": over,
        # 둘 다여야 논의를 연다. 표본이 적은데 비율만 높은 것은 근거가 아니다.
        "should_open_discussion": bool(enough and over),
        "build_graph": False,   # 이 함수는 어떤 경우에도 그래프를 만들라고 하지 않는다
        "note": str(trigger["note"]),
    }


if __name__ == "__main__":
    import tempfile

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ops = load_ops()

    def raises(fn, why):
        try:
            fn()
        except QueryRoutingError:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    # 1. 튜닝값은 코드가 아니라 accounting_ops.yaml 에서 온다
    assert set(ops["levels"]) == {"L0", "L1", "L2", "L3"}
    assert ops["model_tiers"]["none"] is None
    assert ops["tier_routing_approved"] is False, "승인 없이 등급 분화가 켜졌다"
    print("  튜닝값 YAML 적재           OK")

    # 2. **L0 는 모델을 안 부른다** - 이 파일의 절감 대부분이 여기서 나온다
    cheap = classify("현재 NAV 와 현금 잔고 알려줘")
    assert cheap.level == "L0" and cheap.calls_model is False and cheap.model is None
    assert cheap.deterministic_source and "원장에서만" in cheap.deterministic_source
    assert "모델 호출 없음" in routing_note(cheap)
    print("  L0 모델 호출 없음          OK")

    # 3. 높은 레벨이 먼저 맞으면 그 레벨이다 - 맥락이 조회 의도를 이긴다
    close = classify("마감 기준 NAV 확정해도 되는지 설명해줘")
    assert close.level == "L3", close.reason      # nav(L0)·설명(L1)이 있어도 마감/확정이 이긴다
    assert close.tier == "heavy" and close.calls_model is True
    why = classify("이번 주 대사 Break 가 왜 반복되는지 원인 분해해줘")
    assert why.level == "L2" and why.tier == "standard"
    plain = classify("포트폴리오 요약 설명해줘")
    assert plain.level == "L1" and plain.tier == "light"
    print("  레벨 우선순위 (맥락 우선)  OK")

    # 4. 모르는 질의는 싼 쪽이 아니라 중간으로 간다 (모르면 덜 깎는다)
    unknown = classify("zzz 알수없는질의 zzz")
    assert unknown.level == DEFAULT_LEVEL == "L2", unknown.level
    assert unknown.matched == () and "기본 등급" in unknown.reason
    print("  미분류 기본 등급           OK")

    # 5. 등급 -> 모델 매핑은 표에서만 온다. 표에 없으면 태우지 않는다
    assert classify("마감 승인").model == ops["model_tiers"]["heavy"]
    bad_tier = {**ops, "levels": {**ops["levels"], "L1": {**ops["levels"]["L1"], "tier": "ghost"}}}
    raises(lambda: classify("요약 설명해줘", ops=bad_tier), "표에 없는 등급")
    raises(lambda: classify("   "), "빈 질의")
    raises(lambda: load_ops(_HERE / "없는파일.yaml"), "없는 튜닝 파일")
    print("  등급 매핑 fail-closed      OK")

    # 6. **직원 Worker 모델은 이 라우팅과 무관하다** (WORKER_MODEL_MATRIX 절차 보호)
    profile = yaml.safe_load((_HERE / "hermes" / "config.yaml").read_text(encoding="utf-8"))
    assert profile["employee_runtime"]["model_default"] == "qwen3:1.7b"
    assert profile["employee_runtime"]["model_selection"]["active_model"] == "qwen3:1.7b"
    assert set(ops["model_tiers"].values()) - {None} == {profile["model"]["default"]}, \
        "등급 표가 승인 없이 부서장 모델 밖으로 나갔다"
    print("  Worker 모델 불변           OK")

    # 7. 지식 그래프 계측 - L2 만 기록하고, 조건 전에는 절대 만들지 않는다
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "misses.jsonl"
        assert record_relational_miss("현재 NAV 얼마", resolved_relationally=True,
                                      path=log) is None, "L0 질의가 기록됐다"
        for i in range(10):
            record_relational_miss(f"Break 원인 대조 {i}", resolved_relationally=i % 2 == 0,
                                   path=log)
        ready = knowledge_graph_readiness(path=log)
        assert ready["samples"] == 10 and ready["relational_misses"] == 5
        assert ready["miss_ratio"] == 0.5 and ready["threshold_exceeded"] is True
        # 비율은 넘겼지만 표본이 부족하다 -> 논의를 열지 않는다
        assert ready["enough_samples"] is False and ready["should_open_discussion"] is False
        assert ready["build_graph"] is False

        many = [{"resolved_relationally": i % 3 != 0} for i in range(60)]
        opened = knowledge_graph_readiness(many)
        assert opened["enough_samples"] is True and opened["threshold_exceeded"] is True
        assert opened["should_open_discussion"] is True
        assert opened["build_graph"] is False, "계측이 그래프를 만들라고 했다"

        # 관계형으로 다 풀리면 논의 자체가 없다
        fine = knowledge_graph_readiness([{"resolved_relationally": True}] * 60)
        assert fine["should_open_discussion"] is False and fine["miss_ratio"] == 0.0
    print("  지식 그래프 계측           OK")

    # 8. 로그에 회계 수치가 들어가지 않는다 (기억·계측과 원장을 섞지 않는다)
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "m.jsonl"
        row = record_relational_miss("Break 원인 비교", resolved_relationally=False,
                                     detail="조인으로 못 품", path=log)
        assert set(row) == {"at", "query", "resolved_relationally", "detail"}, row
        for forbidden in ("nav", "amount", "balance", "cash"):
            assert forbidden not in row, f"계측 로그에 회계 필드 {forbidden} 가 있다"
    print("  계측 로그 비회계 보장      OK")

    print("ok - 회계 질의 Level 라우팅 8개 영역 점검 통과 "
          f"(L0 는 모델 호출 없음, 등급 분화 승인={ops['tier_routing_approved']})")
