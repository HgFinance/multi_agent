#!/usr/bin/env python3
"""거래소·브로커 규칙 RAG + 분할 설계 실현가능성 검사 (execution-planning / venue-cost).

소유: 도현 (트레이딩본부)
근거: docs/06-integrations/ls-openapi/  (2026-07-29 수집, 42 API / 365 TR)
      CLAUDE.local.md "LS증권 Open API 참조" — 정정·취소 초당 3건, 계좌 조회 초당 1~2건
      docs/HEDGE_FUND_MASTER_PLAN.md 19.8 (Execution Desk), 개발 원칙 9번

집행 계획이 **규칙 숫자를 지어내지 못하게** 하는 계층이다. 둘로 나뉜다:

2026-08-06 이전에는 execution-planning-worker / venue-cost-worker 두 직원이 이 근거를
받아 서술했다. 그 둘이 tool 로 강등되면서(답이 하나로 정해지는 일이었다) 소비자는
`desk-runner` 하나가 됐다 — **이 모듈은 그대로다.** 원래부터 판정을 결정론으로
내놓고 있었고, 사라진 것은 그 판정을 다시 서술하던 계층뿐이다.

  1. 검색(RAG) — `search()` 가 저장소의 LS 문서에서 해당 TR 규칙만 뽑아 준다.
     LLM 은 이 표 밖의 숫자를 쓸 수 없고, 쓰면 `verify_citations()` 가 잡는다.
     `skills/agentic-rag` 와 같은 원칙: 검색·인용 검증은 결정론적 Python, LLM 은 서술만.
  2. 판정 — `check_plan_feasible()` 은 LLM 을 아예 부르지 않는다. 초당 한도를 넘는
     분할 설계는 서술이 아무리 그럴듯해도 **애초에 불가능**하므로 코드가 거부한다.

**왜 임베딩을 안 쓰는가.** 규칙 검색의 정답은 "CSPAT00701 의 한도는 3" 하나뿐이다.
근사 이웃을 돌려주는 검색은 여기서 개선이 아니라 결함이다 — 옆 TR 의 한도를 가져오면
그게 바로 억제하려던 환각이다. TR 코드·경로 정확 일치를 먼저 보고 토큰 겹침으로
보조한다. 인터페이스(`search(query) -> list[ScoredRule]`)는 agentic-rag 와 같은 모양이라
서술형 규칙 문서가 늘어 pgvector 가 필요해지면 이 함수만 바꾸면 된다.

**한도를 경로 단위로 합산한다(보수적).** 원문은 TR 별 초당 제한을 주지만 정정·취소가
같은 `/stock/order` 를 쓴다 — 한도가 TR별인지 경로별인지 원문에 없다. 개발 원칙 9번
(실패 시 진입 차단 방향)에 따라 더 빡빡한 경로 합산으로 본다. 실측으로 TR별임이
확인되면 `_PER_TR_LIMITS = True` 로 뒤집는다.

자체 점검: python departments/02-trading/execution/broker_rules.py
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[2]
LS_DOCS = _REPO_ROOT / "docs" / "06-integrations" / "ls-openapi"

# 한도를 TR별로 볼 것인가 경로별로 합산할 것인가. 위 docstring 참고 — 기본은 보수적.
_PER_TR_LIMITS = False

# 우리가 집행에 실제로 쓰는 TR (CLAUDE.local.md "우리 파트가 쓰는 것" 표).
# 여기 없는 TR 은 색인은 되지만 계획 검사에 쓰이지 않는다.
NEW_ORDER_TR = "CSPAT00601"
REPLACE_TR = "CSPAT00701"
CANCEL_TR = "CSPAT00801"
ACCOUNT_TRS = ("t0424", "t0425", "CSPAQ12200", "CSPAQ13700")


class BrokerRuleError(Exception):
    """규칙을 읽을 수 없거나 색인 밖 규칙을 인용한 경우. 추측해서 채우지 않는다."""


@dataclass(frozen=True)
class Rule:
    """규칙 한 줄. 값과 **출처**를 항상 같이 들고 다닌다 - 출처 없는 숫자는 인용할 수 없다."""

    rule_id: str            # "ls:CSPAT00701"
    title: str              # 현물정정주문
    category: str           # 주식
    path: str               # /stock/order
    per_second: int | None  # 초당 제한 (문서 표의 값)
    has_paper_domain: bool  # 모의투자 Domain 이 선언돼 있는가
    doc: str                # 저장소 상대 경로 (감사 추적)
    source_url: str         # 원문 링크

    @property
    def text(self) -> str:
        limit = "미표기" if self.per_second is None else f"초당 {self.per_second}건"
        paper = "모의투자 Domain 있음" if self.has_paper_domain else "모의투자 Domain 없음(운영 전용)"
        return (f"[{self.category}] {self.title} {self.rule_id.split(':')[-1]} "
                f"{self.path} — {limit}, {paper}")


@dataclass(frozen=True)
class ScoredRule:
    rule: Rule
    score: float


# ── 코퍼스 적재 (저장소의 LS 문서를 그대로 읽는다 - 숫자를 복사해두지 않는다) ──
_HEADER_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|\s*$")
_TR_ROW = re.compile(
    r"^\|\s*\d+\s*\|\s*\[([^\]]+)\]\([^)]*\)\s*\|\s*`([^`]+)`\s*\|\s*([^|]*?)\s*\|")
_SOURCE_URL = re.compile(r"\[원문 문서\]\((https?://[^)]+)\)")


def _header_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("## TR 목록"):
            break
        m = _HEADER_ROW.match(line)
        if m:
            fields[m.group(1)] = m.group(2).strip("` ")
    return fields


def _parse_doc(path: Path) -> list[Rule]:
    text = path.read_text(encoding="utf-8")
    fields = _header_fields(text)
    url = _SOURCE_URL.search(text)
    # 모의투자 Domain 이 "-" 면 그 API 는 운영 Domain 하나뿐이다 (CLAUDE.local.md 실측).
    paper = fields.get("모의투자 Domain", "-").strip() not in {"-", ""}
    rules = []
    for line in text.splitlines():
        m = _TR_ROW.match(line)
        if not m:
            continue
        title, tr_code, limit_raw = m.group(1), m.group(2), m.group(3)
        try:
            per_second: int | None = int(limit_raw)
        except ValueError:
            per_second = None   # 표에 "-" 나 빈칸이면 모른다. 0 으로 읽지 않는다
        rules.append(Rule(
            rule_id=f"ls:{tr_code}", title=title,
            category=fields.get("대분류", "미분류"), path=fields.get("접속 경로", ""),
            per_second=per_second, has_paper_domain=paper,
            doc=str(path.relative_to(_REPO_ROOT)).replace("\\", "/"),
            source_url=url.group(1) if url else "",
        ))
    return rules


@lru_cache(maxsize=1)
def load_rules(docs_root: str | None = None) -> dict[str, Rule]:
    """LS 문서 전체를 규칙 색인으로 만든다. TR 코드가 키다."""
    root = Path(docs_root) if docs_root else LS_DOCS
    if not root.is_dir():
        raise BrokerRuleError(
            f"LS 규칙 문서를 찾을 수 없습니다: {root}. "
            "규칙 없이 집행 계획을 검증하지 않습니다(개발 원칙 9번)")
    index: dict[str, Rule] = {}
    for doc in sorted(root.rglob("*.md")):
        for rule in _parse_doc(doc):
            # 같은 TR 이 여러 문서에 나오면 먼저 만난 것을 유지한다 - 문서 번호 순이라 결정론적.
            index.setdefault(rule.rule_id, rule)
    if not index:
        raise BrokerRuleError(f"{root} 에서 TR 규칙을 하나도 읽지 못했습니다")
    return index


# ── 검색 (결정론) ──────────────────────────────────────────────────────────
def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^0-9A-Za-z가-힣]+", text.lower()) if len(t) > 1}


def search(query: str, *, k: int = 5, rules: Mapping[str, Rule] | None = None) -> list[ScoredRule]:
    """규칙 검색. TR 코드·경로 정확 일치가 항상 토큰 겹침을 이긴다.

    반환 모양은 skills/agentic-rag 의 `search()` 와 같다(질의 in, 점수 붙은 조각 out) -
    서술형 규칙 문서가 늘어 pgvector 가 필요해지면 이 함수만 교체한다.
    """
    index = rules if rules is not None else load_rules()
    q_tokens, q_lower = _tokens(query), query.lower()
    scored: list[ScoredRule] = []
    for rule in index.values():
        code = rule.rule_id.split(":")[-1]
        score = 0.0
        if code.lower() in q_lower:
            score += 100.0                      # TR 코드 직접 지목 - 근사 이웃이 끼어들 자리가 없다
        if rule.path and rule.path.lower() in q_lower:
            score += 10.0
        overlap = q_tokens & _tokens(rule.text)
        score += len(overlap)
        if score > 0:
            scored.append(ScoredRule(rule=rule, score=score))
    scored.sort(key=lambda s: (-s.score, s.rule.rule_id))
    return scored[:k]


def rule_context(query: str, *, k: int = 5, rules: Mapping[str, Rule] | None = None) -> str:
    """Worker 프롬프트에 넣는 근거 블록. **이 표 밖의 숫자는 인용할 수 없다.**"""
    hits = search(query, k=k, rules=rules)
    if not hits:
        return ("검색된 규칙이 없습니다. 규칙 숫자를 추정해 답하지 말고 "
                "'해당 규칙을 찾지 못했다'고 답하십시오.")
    lines = ["다음 규칙만 인용할 수 있습니다. 표에 없는 숫자를 만들어 쓰지 마십시오.",
             "각 주장에 rule_id 를 붙이십시오.", ""]
    lines += [f"- {s.rule.rule_id} | {s.rule.text} | 출처 {s.rule.doc}" for s in hits]
    return "\n".join(lines)


def verify_citations(refs: Iterable[str], *,
                     rules: Mapping[str, Rule] | None = None) -> dict[str, Any]:
    """인용 검증. 색인에 없는 rule_id 는 날조다 - LLM 이 아니라 여기서 잡는다."""
    index = rules if rules is not None else load_rules()
    refs = [str(r) for r in refs]
    unknown = sorted({r for r in refs if r not in index})
    return {"refs": refs, "unknown_refs": unknown,
            "uncited": not refs, "grounded": bool(refs) and not unknown}


# ── 분할 설계 실현가능성 (결정론 - LLM 을 부르지 않는다) ────────────────────
@dataclass(frozen=True)
class ExecutionPlanDraft:
    """집행 계획 초안. `trigger_payload.derive_execution_plan()` 이 프리셋에서 뽑는다.

    `philosophies.yaml` 의 slices / cancel_after_min 이 그대로 여기 들어온다.
    """

    slices: int
    window_minutes: float                  # 이 계획이 쓸 시간 창
    replaces_per_slice: int = 0            # 슬라이스당 재호가(정정) 횟수
    cancels: int | None = None             # 예상 취소 요청 수. None 이면 슬라이스당 1건 가정
    account_polls_per_minute: float = 0.0  # 잔고·체결 조회 폴링
    adapter: str = "paper"                 # paper | ls-live | ls-paper


@dataclass(frozen=True)
class Violation:
    check: str
    detail: str
    rule_ids: tuple[str, ...]


def _limit_for(codes: Iterable[str], index: Mapping[str, Rule]) -> tuple[int, tuple[str, ...]]:
    """검사에 쓸 초당 한도. 경로 합산 모드면 관련 TR 중 가장 빡빡한 값을 쓴다."""
    picked = [(f"ls:{c}", index[f"ls:{c}"]) for c in codes if f"ls:{c}" in index]
    limits = [(rid, r.per_second) for rid, r in picked if r.per_second is not None]
    if not limits:
        raise BrokerRuleError(
            f"초당 한도를 읽을 수 없는 TR 입니다: {list(codes)}. 한도를 추정하지 않습니다")
    rid, value = min(limits, key=lambda x: x[1])
    return value, tuple(r for r, _ in limits) if not _PER_TR_LIMITS else (rid,)


def check_plan_feasible(plan: ExecutionPlanDraft, *,
                        rules: Mapping[str, Rule] | None = None) -> dict[str, Any]:
    """분할 설계가 브로커 한도 안에서 **물리적으로 가능한지** 판정한다.

    서술이 아니라 산수다. 넘으면 계획을 고쳐야 하고, 고치지 않은 계획은 통과하지 않는다.
    """
    index = rules if rules is not None else load_rules()
    if plan.slices < 1:
        raise BrokerRuleError(f"slices 는 1 이상이어야 합니다: {plan.slices}")
    if plan.window_minutes <= 0:
        raise BrokerRuleError(f"window_minutes 는 0 보다 커야 합니다: {plan.window_minutes}")

    window_s = plan.window_minutes * 60.0
    violations: list[Violation] = []

    # 1) 신규 주문 - 슬라이스 하나가 주문 하나다
    new_limit, new_rules = _limit_for([NEW_ORDER_TR], index)
    new_floor = plan.slices / new_limit
    if new_floor > window_s:
        violations.append(Violation(
            "new_order_rate",
            f"신규 주문 {plan.slices}건은 초당 {new_limit}건 한도에서 최소 {new_floor:.1f}초가 "
            f"필요한데 계획 창은 {window_s:.0f}초입니다",
            new_rules))

    # 2) 정정·취소 - 여기가 실제로 먼저 걸린다 (초당 3건)
    cancels = plan.slices if plan.cancels is None else plan.cancels
    amend_count = plan.slices * plan.replaces_per_slice + cancels
    amend_limit, amend_rules = _limit_for([REPLACE_TR, CANCEL_TR], index)
    amend_floor = amend_count / amend_limit if amend_count else 0.0
    if amend_floor > window_s:
        violations.append(Violation(
            "amend_cancel_rate",
            f"정정 {plan.slices * plan.replaces_per_slice}건 + 취소 {cancels}건 = {amend_count}건은 "
            f"초당 {amend_limit}건 한도에서 최소 {amend_floor:.1f}초가 필요한데 "
            f"계획 창은 {window_s:.0f}초입니다",
            amend_rules))

    # 3) 계좌 조회 폴링 - 대사를 종목마다 돌리면 여기서 막힌다
    if plan.account_polls_per_minute > 0:
        acct_limit, acct_rules = _limit_for(ACCOUNT_TRS, index)
        if plan.account_polls_per_minute / 60.0 > acct_limit:
            violations.append(Violation(
                "account_poll_rate",
                f"계좌 조회 분당 {plan.account_polls_per_minute:g}회는 초당 "
                f"{plan.account_polls_per_minute / 60:.2f}회로 한도 {acct_limit}회를 넘습니다. "
                "종목별 조회 대신 배치로 받아 대조하십시오",
                acct_rules))

    # 4) 모의투자 경로 - REST 주문에는 모의투자 Domain 이 없다 (실측 2026-07-29)
    if plan.adapter == "ls-paper":
        order_rule = index.get(f"ls:{NEW_ORDER_TR}")
        if order_rule is not None and not order_rule.has_paper_domain:
            violations.append(Violation(
                "paper_domain_absent",
                f"{order_rule.path} 에는 모의투자 Domain 이 없습니다(운영 Domain 전용). "
                "Paper 체결은 departments/02-trading/broker/paper_broker.py 가 담당합니다",
                (order_rule.rule_id,)))

    min_window_s = max(new_floor, amend_floor)
    return {
        "feasible": not violations,
        "violations": [{"check": v.check, "detail": v.detail, "rule_ids": list(v.rule_ids)}
                       for v in violations],
        "min_window_seconds": round(min_window_s, 2),
        "min_window_minutes": round(min_window_s / 60.0, 2),
        "max_slices_in_window": int(new_limit * window_s),
        "cited_rules": sorted({r for v in violations for r in v.rule_ids}
                              or set(new_rules) | set(amend_rules)),
        # 이 판정은 결정론이다 - 소비자가 LLM 서술과 섞지 않게 계약으로 박는다.
        "decided_by": "deterministic",
        "authoritative": True,
    }


def max_feasible_slices(window_minutes: float, replaces_per_slice: int = 0, *,
                        rules: Mapping[str, Rule] | None = None) -> int:
    """주어진 시간 창에서 가능한 최대 분할 수. 계획을 거부만 하지 말고 대안을 준다."""
    index = rules if rules is not None else load_rules()
    window_s = window_minutes * 60.0
    new_limit, _ = _limit_for([NEW_ORDER_TR], index)
    amend_limit, _ = _limit_for([REPLACE_TR, CANCEL_TR], index)
    # 슬라이스 하나당 신규 1 + 정정 replaces + 취소 1
    by_new = int(new_limit * window_s)
    by_amend = int(amend_limit * window_s / max(replaces_per_slice + 1, 1))
    return max(min(by_new, by_amend), 0)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    index = load_rules()

    # 1. 코퍼스가 저장소 문서에서 실제로 읽힌다 (숫자를 코드에 복사해두지 않았다)
    assert len(index) > 300, f"TR 색인이 너무 작다: {len(index)}"
    replace = index[f"ls:{REPLACE_TR}"]
    assert replace.per_second == 3, replace
    assert replace.path == "/stock/order" and replace.title == "현물정정주문", replace
    assert replace.doc.startswith("docs/06-integrations/ls-openapi/"), replace.doc
    assert replace.source_url.startswith("https://openapi.ls-sec.co.kr/"), replace.source_url
    assert index[f"ls:{NEW_ORDER_TR}"].per_second == 10
    assert index[f"ls:{CANCEL_TR}"].per_second == 3
    assert index["ls:t0424"].per_second == 2
    assert index["ls:CSPAQ12200"].per_second == 1
    # REST 주문에는 모의투자 Domain 이 없다 - 이게 사라지면 문서가 바뀐 것이다
    assert index[f"ls:{NEW_ORDER_TR}"].has_paper_domain is False
    print("  규칙 코퍼스 적재           OK")

    # 2. 검색은 TR 코드 정확 일치를 이웃보다 항상 앞세운다 (환각 억제의 핵심)
    top = search("CSPAT00701 정정 주문 한도")[0]
    assert top.rule.rule_id == f"ls:{REPLACE_TR}", top
    assert search("존재하지않는TR코드zzz") == []
    ctx = rule_context("현물정정주문 초당 제한")
    assert f"ls:{REPLACE_TR}" in ctx and "만들어 쓰지" in ctx
    assert "규칙이 없습니다" in rule_context("zzz없는규칙zzz")
    print("  규칙 검색 + 근거 블록      OK")

    # 3. 색인 밖 인용은 날조로 잡힌다
    ok = verify_citations([f"ls:{REPLACE_TR}", f"ls:{CANCEL_TR}"])
    assert ok["grounded"] is True and ok["unknown_refs"] == []
    bad = verify_citations([f"ls:{REPLACE_TR}", "ls:CSPAT99999"])
    assert bad["grounded"] is False and bad["unknown_refs"] == ["ls:CSPAT99999"]
    none = verify_citations([])
    assert none["uncited"] is True and none["grounded"] is False, "무인용이 통과했다"
    print("  인용 검증 (날조 차단)      OK")

    # 4. **한도를 넘는 분할은 불가능 판정을 받는다** - 이 파일의 존재 이유
    #    10초 창에 40슬라이스 + 슬라이스당 정정 2회 = 정정 80 + 취소 40 = 120건.
    #    초당 3건이면 최소 40초가 필요하다.
    tight = check_plan_feasible(ExecutionPlanDraft(
        slices=40, window_minutes=1.0 / 6, replaces_per_slice=2))
    assert tight["feasible"] is False
    checks = {v["check"] for v in tight["violations"]}
    assert "amend_cancel_rate" in checks, tight["violations"]
    assert tight["min_window_seconds"] == 40.0, tight["min_window_seconds"]
    assert f"ls:{REPLACE_TR}" in tight["cited_rules"], tight["cited_rules"]
    # 위반 사유에 근거 rule_id 가 항상 붙는다 - "왜 불가능한지"가 출처와 함께 남는다
    assert all(v["rule_ids"] for v in tight["violations"])

    # 넉넉한 창이면 통과한다 - 게이트가 항상 막기만 하는 게 아니라는 확인
    roomy = check_plan_feasible(ExecutionPlanDraft(
        slices=10, window_minutes=30, replaces_per_slice=2))
    assert roomy["feasible"] is True and roomy["violations"] == []
    print("  분할 한도 실현가능성       OK")

    # 5. 계좌 조회 폴링 - 종목마다 돌리면 막힌다
    poll = check_plan_feasible(ExecutionPlanDraft(
        slices=1, window_minutes=30, account_polls_per_minute=120))
    assert poll["feasible"] is False
    assert {v["check"] for v in poll["violations"]} == {"account_poll_rate"}
    assert "배치" in poll["violations"][0]["detail"]
    assert check_plan_feasible(ExecutionPlanDraft(
        slices=1, window_minutes=30, account_polls_per_minute=30))["feasible"] is True
    print("  계좌 조회 폴링 한도        OK")

    # 6. LS 모의투자 REST 주문 경로는 존재하지 않는다
    ls_paper = check_plan_feasible(ExecutionPlanDraft(
        slices=1, window_minutes=30, adapter="ls-paper"))
    assert ls_paper["feasible"] is False
    assert {v["check"] for v in ls_paper["violations"]} == {"paper_domain_absent"}
    assert "paper_broker.py" in ls_paper["violations"][0]["detail"]
    # 우리 Paper Broker 경로는 이 검사에 걸리지 않는다
    assert check_plan_feasible(ExecutionPlanDraft(
        slices=1, window_minutes=30, adapter="paper"))["feasible"] is True
    print("  모의투자 Domain 부재       OK")

    # 7. 거부만 하지 않고 대안을 준다
    # 10초 창, 슬라이스당 정정 2 + 취소 1 = 3건 -> 초당 3건이면 10슬라이스가 상한이다.
    assert max_feasible_slices(1.0 / 6, replaces_per_slice=2) == 10, \
        max_feasible_slices(1.0 / 6, replaces_per_slice=2)
    assert check_plan_feasible(ExecutionPlanDraft(
        slices=10, window_minutes=1.0 / 6, replaces_per_slice=2))["feasible"] is True
    assert check_plan_feasible(ExecutionPlanDraft(
        slices=11, window_minutes=1.0 / 6, replaces_per_slice=2))["feasible"] is False
    # 정정이 없으면 취소만 남아 상한이 3배가 된다 - 재호가 횟수가 분할 수를 지배한다
    assert max_feasible_slices(1.0 / 6) == 30
    print("  최대 분할 수 대안 제시     OK")

    # 8. 한도를 모르면 추정하지 않는다 / 잘못된 입력은 조용히 넘기지 않는다
    def raises(fn, why):
        try:
            fn()
        except BrokerRuleError:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    unknown_limit = {"ls:CSPAT00601": Rule(
        rule_id=f"ls:{NEW_ORDER_TR}", title="현물주문", category="주식", path="/stock/order",
        per_second=None, has_paper_domain=False, doc="d", source_url="u")}
    raises(lambda: check_plan_feasible(ExecutionPlanDraft(slices=1, window_minutes=1),
                                       rules=unknown_limit), "한도 미표기 TR")
    raises(lambda: check_plan_feasible(ExecutionPlanDraft(slices=0, window_minutes=1)), "slices 0")
    raises(lambda: check_plan_feasible(ExecutionPlanDraft(slices=1, window_minutes=0)), "창 0분")
    raises(lambda: load_rules(str(_HERE / "없는경로")), "규칙 문서 없는 경로")
    print("  한도 미상 fail-closed      OK")

    print("ok - 거래소·브로커 규칙 RAG 8개 영역 점검 통과 "
          f"(TR {len(index)}개 색인, 정정·취소 {index[f'ls:{REPLACE_TR}'].per_second}/s 강제)")
