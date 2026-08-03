#!/usr/bin/env python3
"""분석가별 도구 상자 - 자율성과 검증 가능성을 **같이** 키운다.

담당: 재일 (리서치본부)
근거: docs/02-engineering/RESEARCH_QUANT_AGENTIC_FRAMEWORK.md
        5.2절(세 계층의 책임), 6.1절 3단계(Query Plan)·6단계(Evidence Validation),
        6.2절(역할별 Retrieval), 12.2.1절(Web Search MCP 와 직원 배정)
      docs/02-engineering/RESEARCH_OUTPUT_ADVANCEMENT_STRATEGY.md
        4.1절(Evidence before Narrative), 4.5절(계산과 승인은 Agent 밖에 둔다)
      재일님 지시 2026-08-03 "각 에이전트 밑에 랭그래프로 도구들을 사용할 수 있게",
        "너무 하드코딩된 규칙을 따르는 것 같아서 보고서의 질이 떨어진다"

▶ 이 모듈이 푸는 문제
  지금 분석가는 payload 를 **한 번 받아 한 번 서술**하고 끝난다. "이 공시 원문을
  봐야겠다", "동종업계와 비교해야겠다" 를 할 수단이 없다.

  그래서 number_guard 가 가혹해 보인다 - "readout 에 없는 숫자를 쓰지 마라"인데
  readout 을 넓힐 방법이 없으니 **모든 추가 통찰이 창작으로 보인다.**
  화이트리스트가 곧 1회 fetch 결과인 것이 문제이지 가드가 문제가 아니다.

  도구를 주면 뒤집힌다. 분석가가 **정당하게 가져온 것이 화이트리스트에 추가**되므로
  자율성과 검증 가능성이 같이 커진다. 가드를 푸는 것이 답이 아니다.

▶ 세 계층을 지킨다
  Hermes      부서 업무 생명주기 (이 모듈 밖)
  LangGraph   Case 상태 전이·분기·예산 (호출자)
  도구        여기. **판정하지 않는다** - 가져오고, 숫자를 풀에 넣고, 근거 ID 를 단다.

▶ 자율성에 예산을 둔다 (무한 루프가 아니라 예산 안의 자율)
  ResearchCaseV2.budgets.max_retrieval_rounds 와 같은 뜻이다. 라운드를 넘기면
  **더 못 부른다** - 부를 수 있는데 안 부른 것과 예산이 끝난 것을 구분해 남긴다.

▶ 권한은 hermes/config.yaml 이 정본이다
  tool_allowlist 에 없는 도구는 이 상자에 담기지 않는다. 코드가 선언을 어기면
  선언이 거짓이 된다 - 자체 점검이 둘을 대조한다.

실행: python agents/analyst_toolbox.py     # 자체 점검 (네트워크·LLM 없음)
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent / "evidence", _HERE.parent / "contracts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

TOOLBOX_VERSION = "research-analyst-toolbox-v1"

_CONFIG = _HERE.parent / "hermes" / "config.yaml"


class ToolError(RuntimeError):
    """도구 호출이 실패했다. **통과로 위장하지 않는다.**"""


class BudgetExceeded(ToolError):
    """검색 예산을 다 썼다. 더 부르지 않는다."""


class NotAllowed(ToolError):
    """이 페르소나에게 허용되지 않은 도구다(tool_allowlist 위반)."""


@dataclass(frozen=True)
class ToolResult:
    """도구 한 번의 결과.

    ▶ 세 가지를 **함께** 낸다. 하나라도 빠지면 이 계층의 목적이 사라진다.
      data          분석가가 읽을 내용
      numbers       검증 풀에 추가될 수치 {키: 값} - 이게 있어야 서술이 자유로워진다
      evidence_refs 인용 가능한 근거 ID - 이게 있어야 주장이 fact 가 된다
    """

    tool: str
    ok: bool
    data: Any = None
    numbers: dict[str, float] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.ok and not self.reason:
            raise ValueError(f"{self.tool}: 실패인데 사유가 없다 - 침묵하는 실패는 금지")
        for k, v in self.numbers.items():
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise ValueError(f"{self.tool}: numbers[{k!r}] 가 수치가 아니다({v!r})")


@dataclass(frozen=True)
class ToolSpec:
    """도구 하나의 계약."""

    name: str                 # 'research.evidence.disclosures.read' 같은 allowlist 표기
    label: str                # LLM 이 부를 짧은 이름
    what: str                 # 무엇을 주는가 (프롬프트에 그대로 들어간다)
    when: str                 # 언제 쓰는가 - 이게 없으면 LLM 이 아무 때나 부른다
    run: Callable[..., ToolResult]
    args: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 권한 - hermes/config.yaml 이 정본
# ---------------------------------------------------------------------------

def load_allowlist(path: Path | None = None) -> dict[str, tuple[str, ...]]:
    """config.yaml 의 tool_allowlist 를 읽는다. 페르소나 -> 도구 이름들.

    yaml 의존을 피하려 최소 파서를 쓴다 - 이 블록은 형태가 단순하고 고정이며,
    새 의존을 들이는 기준(개발원칙 8)을 만족시키지 못한다.
    """
    p = path or _CONFIG
    if not p.exists():
        raise ToolError(f"config.yaml 이 없다: {p} - 권한 정본 없이 도구를 열지 않는다")
    out: dict[str, list[str]] = {}
    cur: str | None = None
    inside = False
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("tool_allowlist:"):
            inside = True
            continue
        if inside:
            indent = len(line) - len(line.lstrip())
            if indent <= 2 and stripped.endswith(":") and "[" not in stripped:
                break              # 다음 최상위 키 - allowlist 블록 끝
            if ":" in stripped and "[" in stripped:
                cur = stripped.split(":", 1)[0].strip()
                body = stripped.split("[", 1)[1]
                out[cur] = []
                out[cur] += [x.strip().rstrip("],") for x in body.split(",") if x.strip(" ],")]
            elif cur and (stripped.startswith(("research.", "market.", "case."))
                          or stripped.rstrip("],").strip().startswith(("research.", "market.", "case."))):
                out[cur] += [x.strip().rstrip("],")
                             for x in stripped.split(",") if x.strip(" ],")]
    return {k: tuple(dict.fromkeys(v)) for k, v in out.items() if v}


# ---------------------------------------------------------------------------
# 도구 상자
# ---------------------------------------------------------------------------

@dataclass
class ToolBox:
    """한 분석가가 이번 Case 에서 쓸 수 있는 도구들.

    **상태를 갖는다** - 라운드 예산과 누적 수치 풀. 그래서 frozen 이 아니다.
    """

    persona: str
    specs: tuple[ToolSpec, ...]
    max_rounds: int = 2
    rounds_used: int = 0
    calls: list[ToolResult] = field(default_factory=list)

    def catalog(self) -> list[dict]:
        """LLM 에게 보여줄 도구 목록. **when 을 반드시 함께 준다.**"""
        return [{"tool": s.label, "what": s.what, "when": s.when, "args": list(s.args)}
                for s in self.specs]

    def _spec(self, label: str) -> ToolSpec:
        for s in self.specs:
            if s.label == label:
                return s
        raise NotAllowed(
            f"{self.persona} 는 '{label}' 을 쓸 수 없다 - 가능: "
            f"{[s.label for s in self.specs]}")

    def call(self, label: str, **kwargs) -> ToolResult:
        if self.rounds_used >= self.max_rounds:
            raise BudgetExceeded(
                f"검색 예산 {self.max_rounds}회를 다 썼다 - 더 부르지 않는다. "
                f"부족하면 그 사실을 cautions 에 남긴다")
        spec = self._spec(label)
        self.rounds_used += 1
        try:
            r = spec.run(**kwargs)
        except Exception as e:  # noqa: BLE001
            r = ToolResult(tool=label, ok=False,
                           reason=f"{type(e).__name__}: {e}"[:200])
        self.calls.append(r)
        return r

    # ── 이 상자가 존재하는 이유 ────────────────────────────────────────────
    def retrieved_numbers(self) -> dict[str, float]:
        """도구로 **정당하게 가져온** 수치. number_guard 풀에 합쳐진다.

        이것이 이 계층의 핵심이다 - 분석가가 스스로 근거를 넓히면 서술도
        그만큼 자유로워진다. 창작은 여전히 막힌다(가져오지 않은 수치는 풀에 없다).
        """
        pool: dict[str, float] = {}
        for r in self.calls:
            if r.ok:
                for k, v in r.numbers.items():
                    pool[f"{r.tool}.{k}"] = float(v)
        return pool

    def retrieved_refs(self) -> tuple[str, ...]:
        """도구로 가져온 근거 ID. 주장이 fact 가 되려면 이게 필요하다."""
        seen: list[str] = []
        for r in self.calls:
            if r.ok:
                for e in r.evidence_refs:
                    if e not in seen:
                        seen.append(e)
        return tuple(seen)

    def trace(self) -> dict:
        """무엇을 왜 불렀는지. **부를 수 있었는데 안 부른 것도 드러낸다.**"""
        used = [r.tool for r in self.calls]
        return {
            "persona": self.persona,
            "available": [s.label for s in self.specs],
            "called": used,
            "not_called": [s.label for s in self.specs if s.label not in used],
            "rounds_used": self.rounds_used,
            "max_rounds": self.max_rounds,
            "budget_exhausted": self.rounds_used >= self.max_rounds,
            "failures": [{"tool": r.tool, "reason": r.reason}
                         for r in self.calls if not r.ok],
        }


# ---------------------------------------------------------------------------
# 페르소나 -> 도구 배정 (역할별 Retrieval, framework 6.2)
# ---------------------------------------------------------------------------
#
# allowlist 이름과 짝지어 선언한다. allowlist 에 없는 도구는 담기지 않는다.

PERSONA_TOOLS: dict[str, tuple[tuple[str, str, str, str, tuple[str, ...]], ...]] = {
    "fundamental-analyst": (
        ("research.evidence.disclosures.read", "disclosure_detail",
         "특정 공시의 제목·유형·시각과 중요도 분류(MATERIAL/NOTABLE/ROUTINE)",
         "재무 수치의 배경(공급계약·증자·자기주식)이 필요할 때. "
         "재무제표만으로 설명 안 되는 변화가 보이면 부른다", ("symbol", "days")),
        ("research.evidence.financials.read", "financial_history",
         "과거 보고기간의 재무 항목 시계열(정정본은 최신 revision)",
         "전년 대비 한 시점 비교로 부족할 때. 추세인지 일회성인지 가르려면 부른다",
         ("symbol", "limit")),
    ),
    "news-sentiment-analyst": (
        ("research.evidence.stories.read", "story_cluster",
         "같은 사건을 다룬 기사 묶음과 독립 출처 수",
         "한 건의 보도인지 여러 출처가 확인한 사건인지 가를 때. "
         "**단일 출처를 사실로 올리지 않기 위해** 부른다", ("symbol", "hours")),
        ("research.evidence.news.read", "news_window",
         "더 넓은 시간창의 기사 목록(document_id 포함)",
         "24시간 창에 재료가 부족할 때. 창을 넓힌 사실을 반드시 서술에 남긴다",
         ("symbol", "hours")),
    ),
    "sector-regime-analyst": (
        ("research.macro.read", "macro_series",
         "거시·스타일 지수 관측치(vintage 포함, PIT)",
         "레짐 판정의 근거가 한 지표뿐일 때. 다른 계열이 같은 방향인지 확인",
         ("code", "days")),
        ("market.breadth.read", "breadth_history",
         "시장 폭 지표의 과거 시계열",
         "오늘 값이 이례적인지 판단할 때. **비교 기준 없는 단일 값은 판정이 아니다**",
         ("days",)),
    ),
    "geopolitical-analyst": (
        ("research.macro.read", "macro_series",
         "GPR·GDELT 계열 관측치(발표 지연 포함)",
         "지수 하나로 국면을 말하기 전에 다른 계열과 대조할 때", ("code", "days")),
    ),
    "technical-analyst": (
        ("market.bars.read", "price_history",
         "더 긴 일봉 시계열(종가·거래량)",
         "20일 창으로 부족할 때. 장기 추세와 단기 신호가 어긋나는지 확인",
         ("symbol", "limit")),
    ),
    "microstructure-analyst": (
        ("market.microstructure.read", "micro_history",
         "과거 일자의 미시구조 집계",
         "오늘 스프레드·불균형이 평소 대비 이례적인지 볼 때. "
         "**비교 없는 STRESSED 판정은 근거가 약하다**", ("symbol", "date")),
    ),
}


def build_toolbox(persona: str, runners: dict[str, Callable[..., ToolResult]],
                  *, max_rounds: int = 2,
                  allowlist: dict[str, tuple[str, ...]] | None = None) -> ToolBox:
    """페르소나의 도구 상자를 만든다.

    runners 는 label -> 실제 호출 함수다. 없는 것은 **담지 않는다** -
    있다고 해놓고 부르면 실패하는 것보다 아예 없는 편이 정직하다.
    """
    allow = allowlist if allowlist is not None else load_allowlist()
    granted = set(allow.get(persona, ()))
    specs: list[ToolSpec] = []
    for scope, label, what, when, args in PERSONA_TOOLS.get(persona, ()):
        if scope not in granted:
            continue          # 선언에 없으면 담지 않는다
        if label not in runners:
            continue          # 구현이 없으면 담지 않는다
        specs.append(ToolSpec(name=scope, label=label, what=what, when=when,
                              run=runners[label], args=args))
    return ToolBox(persona=persona, specs=tuple(specs), max_rounds=max_rounds)


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·LLM 없음
# ---------------------------------------------------------------------------

def _fake(label: str, *, ok=True, nums=None, refs=(), reason=""):
    def run(**kw):
        return ToolResult(tool=label, ok=ok, data={"kw": kw},
                          numbers=nums or {}, evidence_refs=refs, reason=reason)
    return run


def _check_allowlist_parses():
    allow = load_allowlist()
    assert "fundamental-analyst" in allow, sorted(allow)
    assert "research.evidence.financials.read" in allow["fundamental-analyst"], \
        allow["fundamental-analyst"]
    # 총괄은 자체 조회가 없다 - 선언이 그렇게 돼 있어야 한다
    assert "research.evidence.news.read" not in allow.get("research-supervisor", ())
    print(f"  allowlist 파싱({len(allow)}인)   OK")


def _check_declared_tools_exist_in_allowlist():
    """PERSONA_TOOLS 가 선언에 없는 권한을 요구하지 않는가.

    코드가 선언을 어기면 선언이 거짓이 된다 - 게이트웨이가 403 을 낼 것이다.
    """
    allow = load_allowlist()
    bad = []
    for persona, tools in PERSONA_TOOLS.items():
        granted = set(allow.get(persona, ()))
        for scope, label, *_ in tools:
            if scope not in granted:
                bad.append(f"{persona} -> {label} ({scope})")
    assert not bad, ("allowlist 에 없는 권한을 도구가 요구한다:\n  - "
                     + "\n  - ".join(bad)
                     + "\n  config.yaml 에 먼저 넣거나 도구를 빼야 한다")
    print("  도구 <-> 선언 일치       OK")


def _check_budget_is_enforced():
    box = build_toolbox("fundamental-analyst",
                        {"financial_history": _fake("financial_history",
                                                    nums={"매출액_yoy_pct": 6.29})},
                        max_rounds=1, allowlist={"fundamental-analyst": (
                            "research.evidence.financials.read",)})
    assert [s.label for s in box.specs] == ["financial_history"]
    box.call("financial_history", symbol="005380", limit=8)
    try:
        box.call("financial_history", symbol="005380", limit=8)
        raise AssertionError("예산을 넘겼는데 통과했다")
    except BudgetExceeded as e:
        assert "예산" in str(e)
    assert box.trace()["budget_exhausted"] is True
    print("  예산 안의 자율           OK")


def _check_not_allowed_is_refused():
    box = build_toolbox("technical-analyst",
                        {"price_history": _fake("price_history")},
                        allowlist={"technical-analyst": ("market.bars.read",)})
    try:
        box.call("disclosure_detail", symbol="005380")
        raise AssertionError("허용 안 된 도구가 통과했다")
    except NotAllowed as e:
        assert "쓸 수 없다" in str(e)
    # 구현이 없으면 아예 담기지 않는다 - '있다고 해놓고 실패' 보다 정직하다
    empty = build_toolbox("technical-analyst", {},
                          allowlist={"technical-analyst": ("market.bars.read",)})
    assert empty.specs == ()
    print("  권한 밖 거부             OK")


def _check_pool_and_refs_grow():
    """**이 모듈의 존재 이유** - 정당하게 가져온 수치가 검증 풀을 넓히는가."""
    box = build_toolbox("news-sentiment-analyst", {
        "story_cluster": _fake("story_cluster", nums={"독립출처수": 3.0},
                               refs=("doc-a", "doc-b")),
        "news_window": _fake("news_window", nums={"기사수": 42.0},
                             refs=("doc-b", "doc-c")),
    }, allowlist={"news-sentiment-analyst": (
        "research.evidence.stories.read", "research.evidence.news.read")})
    box.call("story_cluster", symbol="005380", hours=48)
    box.call("news_window", symbol="005380", hours=72)
    pool = box.retrieved_numbers()
    assert pool == {"story_cluster.독립출처수": 3.0, "news_window.기사수": 42.0}, pool
    # 근거는 중복 없이 순서대로
    assert box.retrieved_refs() == ("doc-a", "doc-b", "doc-c")
    print("  풀·근거 확장             OK")


def _check_failure_is_not_silent():
    box = build_toolbox("geopolitical-analyst",
                        {"macro_series": _fake("macro_series", ok=False,
                                               reason="API 500")},
                        allowlist={"geopolitical-analyst": ("research.macro.read",)})
    r = box.call("macro_series", code="GPR", days=30)
    assert r.ok is False and "API 500" in r.reason
    # 실패한 도구의 수치는 풀에 들어가지 않는다
    assert box.retrieved_numbers() == {}
    assert box.trace()["failures"] == [{"tool": "macro_series", "reason": "API 500"}]
    # 사유 없는 실패는 만들 수 없다
    try:
        ToolResult(tool="x", ok=False)
        raise AssertionError("사유 없는 실패가 생성됐다")
    except ValueError as e:
        assert "침묵하는 실패" in str(e)
    print("  실패는 침묵하지 않는다   OK")


def _check_trace_shows_unused():
    """부를 수 있었는데 안 부른 것도 드러나는가 - 도구 활용도를 재려면 필요하다."""
    box = build_toolbox("fundamental-analyst", {
        "disclosure_detail": _fake("disclosure_detail"),
        "financial_history": _fake("financial_history"),
    }, allowlist={"fundamental-analyst": (
        "research.evidence.disclosures.read", "research.evidence.financials.read")})
    box.call("disclosure_detail", symbol="005380", days=7)
    t = box.trace()
    assert t["called"] == ["disclosure_detail"]
    assert t["not_called"] == ["financial_history"], t
    print("  안 부른 도구도 보고      OK")


def _check_catalog_has_when():
    """LLM 에게 'when' 을 주는가 - 없으면 아무 때나 부르거나 아예 안 부른다."""
    box = build_toolbox("microstructure-analyst",
                        {"micro_history": _fake("micro_history")},
                        allowlist={"microstructure-analyst": (
                            "market.microstructure.read",)})
    c = box.catalog()
    assert c and all(x["what"] and x["when"] for x in c), c
    assert "비교 없는" in c[0]["when"], c[0]["when"]
    print("  도구 설명에 when 포함    OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{TOOLBOX_VERSION} 자체 점검 (네트워크·LLM 없음)")
    _check_allowlist_parses()
    _check_declared_tools_exist_in_allowlist()
    _check_budget_is_enforced()
    _check_not_allowed_is_refused()
    _check_pool_and_refs_grow()
    _check_failure_is_not_silent()
    _check_trace_shows_unused()
    _check_catalog_has_when()
    print("분석가 도구 상자 8개 영역 통과.")
