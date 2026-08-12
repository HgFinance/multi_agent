"""실험별 전략 시그널 - 코드가 실험마다 달라도 되게 만드는 계층.

담당: 재일 (퀀트·백테스트본부 QNT)

▶ 왜 필요한가
  리서치가 논문·서한에서 가져오는 방법론은 **서로 다른 계산**이다. 그것을 기성
  템플릿의 파라미터로만 표현하면 공장이 손잡이 돌리기가 된다. 그러니
  **실험마다 전략 코드가 다른 것이 정상이다.**

▶ 그럼 무엇을 막아야 하나
  코드가 다른 것이 아니라 **결과를 본 뒤 코드가 바뀌는 것**이다. 그래서 코드를
  금지하는 대신 **사전등록 대상에 코드를 넣는다**:

    ① 코드 해시가 사전등록 지문에 들어간다 -> 고치면 새 시도로 계수된다(DSR 감가)
    ② 실행 전에 결정론·PIT·반환형 검사를 통과해야 한다
    ③ 시그널은 PITView 만 받는다 -> 미래를 꺼낼 접근자가 아예 없다
    ④ 임포트·파일·네트워크·동적 실행이 문법 수준에서 거부된다

  ③이 핵심이다. LLM 이 쓴 백테스트 코드의 look-ahead 버그는 **항상 결과가 좋아지는
  방향으로** 틀린다. 사후 검사로 잡으려 하면 통과시킬 방법이 생기지만, 꺼낼 수
  없는 데이터는 쓸 수 없다.

▶ 이 모듈이 하지 않는 것
  - 시간·메모리 제한: 무한 루프는 여기서 막지 못한다. 러너/워커의 실행 타임아웃이
    담당한다(job_queue). **이 한계를 알고 쓴다** - "샌드박스니까 안전하다"고
    믿는 것이 제일 위험하다.
  - 코드 품질 판정: 좋은 전략인지는 실험이 판정한다. 여기서는 안전과 재현만 본다.

자체 점검: python departments/04-quant-backtest/pipeline/strategy_spec.py
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from dataclasses import dataclass, field

from strategy_templates import (
    TEMPLATES,
    PITView,
    Template,
    signal_scores,
)

MODULE_VERSION = "quant-strategy-spec-v1"

SIGNAL_FN_NAME = "signal"

# 커스텀 코드가 쓸 수 있는 전부. 여기 없는 이름은 실행 시점에 NameError 다.
# 임포트가 막혀 있으므로 수학 함수도 여기 없으면 못 쓴다(필요하면 PITView 에 넣는다).
SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "divmod": divmod, "enumerate": enumerate, "float": float, "int": int,
    "len": len, "list": list, "max": max, "min": min, "pow": pow,
    "range": range, "reversed": reversed, "round": round, "set": set,
    "sorted": sorted, "str": str, "sum": sum, "tuple": tuple, "zip": zip,
    "True": True, "False": False, "None": None,
}

# 문법 수준에서 거부하는 노드. **허용 목록이 아니라 금지 목록인 이유**: 파이썬
# 문법 노드는 계속 늘어나므로, 위험한 것을 명시적으로 막고 나머지는 이름 검사와
# 빈 전역(SAFE_BUILTINS)으로 통제한다. 이름을 못 부르면 문법이 있어도 못 쓴다.
FORBIDDEN_NODES = (
    ast.Import, ast.ImportFrom,          # 임포트 = 모든 방어의 우회로
    ast.Global, ast.Nonlocal,            # 바깥 상태 오염
    ast.ClassDef,                        # 필요 없다
    ast.With, ast.AsyncWith,             # 컨텍스트 매니저(파일 등)
    ast.Try,                             # 예외를 삼켜 실패를 숨긴다
    ast.Delete,
    ast.AsyncFunctionDef, ast.Await, ast.AsyncFor,
    ast.Yield, ast.YieldFrom,
)

FORBIDDEN_NAMES = frozenset({
    "exec", "eval", "compile", "open", "input", "breakpoint",
    "__import__", "globals", "locals", "vars",
    "getattr", "setattr", "delattr", "hasattr",   # 속성 우회 접근
    "type", "object", "super", "memoryview",
})


class StrategyCodeError(ValueError):
    """커스텀 전략 코드가 안전·계약 요건을 못 지켰다."""


# ---------------------------------------------------------------------------
# 검증
# ---------------------------------------------------------------------------

def validate_code(code: str) -> None:
    """문법 수준 검사. **통과 못 하면 컴파일조차 하지 않는다.**"""
    if not isinstance(code, str) or not code.strip():
        raise StrategyCodeError("전략 코드가 비어 있다")
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise StrategyCodeError(f"문법 오류: {exc}") from exc

    for node in ast.walk(tree):
        if isinstance(node, FORBIDDEN_NODES):
            raise StrategyCodeError(
                f"허용되지 않는 문법: {type(node).__name__} "
                f"(임포트·예외삼킴·클래스·파일 접근은 전부 막는다)")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise StrategyCodeError(f"허용되지 않는 이름: {node.id}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            # __class__/__globals__ 로 샌드박스를 빠져나가는 고전 경로
            raise StrategyCodeError(f"밑줄 속성 접근 금지: .{node.attr}")

    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    names = {f.name for f in fns}
    if SIGNAL_FN_NAME not in names:
        raise StrategyCodeError(
            f"최상위에 `def {SIGNAL_FN_NAME}(view, params)` 가 있어야 한다 "
            f"(발견: {sorted(names) or '없음'})")
    sig = next(f for f in fns if f.name == SIGNAL_FN_NAME)
    if len(sig.args.args) != 2:
        raise StrategyCodeError(
            f"`{SIGNAL_FN_NAME}` 은 인자 2개(view, params)를 받아야 한다 "
            f"- 현재 {len(sig.args.args)}개")
    # 최상위에 함수 정의 외의 실행문을 두지 않는다(임포트 시점 부작용 차단)
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.Expr)):
            raise StrategyCodeError(
                f"최상위에는 함수 정의만 둔다: {type(node).__name__}")
        if isinstance(node, ast.Expr) and not isinstance(node.value, ast.Constant):
            raise StrategyCodeError("최상위 실행문 금지(독스트링만 허용)")


def compile_signal(code: str):
    """검증된 코드를 제한 전역에서 컴파일해 `signal` 함수를 돌려준다."""
    validate_code(code)
    env: dict = {"__builtins__": dict(SAFE_BUILTINS)}
    exec(compile(code, "<strategy-spec>", "exec"), env)   # noqa: S102
    fn = env.get(SIGNAL_FN_NAME)
    if not callable(fn):
        raise StrategyCodeError(f"`{SIGNAL_FN_NAME}` 을 찾을 수 없다")
    return fn


# ---------------------------------------------------------------------------
# 스펙
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StrategySpec:
    """이 실험이 무엇을 계산하는가. **사전등록 지문에 들어간다.**"""

    kind: str                      # "template" | "custom"
    edge_type: str
    rank: str                      # TOP | BOTTOM
    params: dict = field(default_factory=dict)
    template_id: str = ""
    code: str = ""                 # kind == "custom" 일 때만
    min_history: int = 0

    def payload(self) -> dict:
        """해시 대상. **코드 원문 그대로** 들어간다 - 주석 한 글자만 바뀌어도
        다른 스펙이고, 그건 새 시도다(사후 수정을 시도로 세기 위한 것)."""
        return {
            "module": MODULE_VERSION,
            "kind": self.kind,
            "edge_type": self.edge_type,
            "rank": self.rank,
            "template_id": self.template_id,
            "params": {k: self.params[k] for k in sorted(self.params)},
            "code": self.code,
            "min_history": int(self.min_history),
        }

    def spec_hash(self) -> str:
        blob = json.dumps(self.payload(), sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"))
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def from_template(edge_type: str, params: dict | None = None) -> StrategySpec:
    tpl = next((t for t in TEMPLATES.values() if t.edge_type == edge_type), None)
    if tpl is None:
        raise StrategyCodeError(f"통제 어휘에 없는 edge_type: {edge_type!r}")
    p = dict(params or {})
    return StrategySpec(kind="template", edge_type=tpl.edge_type, rank=tpl.rank,
                        params=p, template_id=tpl.template_id,
                        min_history=tpl.min_history(p))


def from_code(*, edge_type: str, rank: str, code: str,
              params: dict | None = None, min_history: int = 0) -> StrategySpec:
    """커스텀 시그널. **여기서 검증에 실패하면 스펙 자체가 만들어지지 않는다** -
    사전등록에 못 들어가므로 실험을 시작할 수 없다."""
    if rank not in ("TOP", "BOTTOM"):
        raise StrategyCodeError(f"rank 는 TOP|BOTTOM 이어야 한다: {rank!r}")
    validate_code(code)
    return StrategySpec(kind="custom", edge_type=str(edge_type).strip().lower(),
                        rank=rank, params=dict(params or {}), code=code,
                        min_history=int(min_history))


def signal_fn(spec: StrategySpec):
    """스펙 -> 실행 가능한 시그널 함수 (view, params) -> dict."""
    if spec.kind == "template":
        tpl: Template = TEMPLATES[spec.template_id]
        return lambda view, params: tpl.signal(view, params)
    return compile_signal(spec.code)


def scores(spec: StrategySpec, market, until) -> dict:
    """시그널 실행. **Market 이 아니라 PITView 를 넘긴다.**"""
    if spec.kind == "template":
        return signal_scores(market, until, template=TEMPLATES[spec.template_id],
                             params=spec.params)
    out = compile_signal(spec.code)(PITView(market, until), dict(spec.params))
    return _coerce(out)


def _coerce(out) -> dict:
    """반환값 계약 강제: dict[str, float]. 어기면 실행을 세우고 알린다."""
    if out is None:
        return {}
    if not isinstance(out, dict):
        raise StrategyCodeError(
            f"시그널은 dict[str, float] 를 돌려줘야 한다 - 받은 형: {type(out).__name__}")
    clean = {}
    for k, v in out.items():
        if not isinstance(k, str):
            raise StrategyCodeError(f"시그널 키는 종목 문자열이어야 한다: {k!r}")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise StrategyCodeError(f"시그널 값은 수치여야 한다: {k}={v!r}")
        fv = float(v)
        if fv != fv or fv in (float("inf"), float("-inf")):
            # NaN/inf 는 정렬에서 조용히 1등이 되거나 순서를 뒤집는다
            raise StrategyCodeError(f"시그널 값이 NaN/inf 다: {k}={v!r}")
        clean[k] = fv
    return clean


# ---------------------------------------------------------------------------
# 사전등록 전 검증 - 이걸 통과해야 실험을 시작할 수 있다
# ---------------------------------------------------------------------------

def verify(spec: StrategySpec, market_short, market_long, until) -> dict:
    """결정론·PIT 불변·반환형을 실측으로 확인한다.

    market_short 와 market_long 은 **같은 규칙에 미래만 덧붙인** 두 시장이어야
    한다. 같은 `until` 의 시그널이 다르면 그 코드는 미래를 본 것이다.
    """
    findings: list[str] = []
    a = scores(spec, market_short, until)
    b = scores(spec, market_short, until)
    if a != b:
        findings.append("비결정론: 같은 입력에 다른 시그널이 나온다")
    c = scores(spec, market_long, until)
    if a != c:
        findings.append("PIT 위반: 미래 데이터가 과거 시그널을 바꿨다")
    if not a:
        findings.append("시그널이 비어 있다(표본 부족이거나 조건이 아무 종목도 안 잡는다)")
    return {"ok": not findings, "findings": findings,
            "spec_hash": spec.spec_hash(), "n_scored": len(a)}


# ── 자체 점검 ────────────────────────────────────────────────────────────────

_GOOD = """
def signal(view, params):
    '''20일 수익률 - 템플릿과 같은 계산을 커스텀 코드로 쓴 것'''
    lb = params.get("lookback_days", 20)
    out = {}
    for s in view.symbols:
        r = view.total_return(s, lb)
        if r is not None:
            out[s] = r
    return out
"""


def _mk(n_days=90, symbols=("A", "B", "C", "D")):
    import strategy_templates as st
    return st._mk(n_days, symbols)


def _check_import_is_rejected():
    for bad in ("import os\ndef signal(v, p): return {}",
                "from os import path\ndef signal(v, p): return {}"):
        try:
            validate_code(bad)
        except StrategyCodeError as e:
            assert "허용되지 않는 문법" in str(e), e
        else:
            raise AssertionError("임포트가 통과했다")


def _check_dangerous_names_are_rejected():
    for name in ("eval", "exec", "open", "__import__", "getattr", "globals"):
        code = f"def signal(v, p):\n    return {name}"
        try:
            validate_code(code)
        except StrategyCodeError as e:
            assert "허용되지 않는 이름" in str(e), (name, e)
        else:
            raise AssertionError(f"{name} 이 통과했다")


def _check_dunder_escape_is_rejected():
    """`().__class__.__bases__` 류 샌드박스 탈출 경로."""
    code = "def signal(v, p):\n    return ().__class__\n"
    try:
        validate_code(code)
    except StrategyCodeError as e:
        assert "밑줄 속성" in str(e), e
    else:
        raise AssertionError("dunder 접근이 통과했다")


def _check_toplevel_side_effect_is_rejected():
    code = "x = 1\ndef signal(v, p): return {}"
    try:
        validate_code(code)
    except StrategyCodeError as e:
        assert "함수 정의만" in str(e), e
    else:
        raise AssertionError("최상위 실행문이 통과했다")


def _check_signature_is_enforced():
    for bad, why in ((("def helper(v, p): return {}"), "signal 없음"),
                     ("def signal(v): return {}", "인자 1개")):
        try:
            validate_code(bad)
        except StrategyCodeError:
            pass
        else:
            raise AssertionError(f"{why} 가 통과했다")


def _check_no_builtins_leak_at_runtime():
    """검증을 통과해도 실행 전역에 위험한 이름이 없어야 한다(이중 방어)."""
    fn = compile_signal(_GOOD)
    g = fn.__globals__
    assert set(g["__builtins__"]) == set(SAFE_BUILTINS), sorted(g["__builtins__"])
    assert "__import__" not in g["__builtins__"]


def _check_custom_matches_template():
    """같은 계산을 커스텀으로 써도 템플릿과 값이 같아야 한다."""
    m = _mk()
    until = m.dates[-1]
    spec_t = from_template("momentum", {"lookback_days": 20})
    spec_c = from_code(edge_type="momentum", rank="TOP", code=_GOOD,
                       params={"lookback_days": 20}, min_history=21)
    assert scores(spec_t, m, until) == scores(spec_c, m, until)


def _check_pitview_makes_leakage_unreachable():
    """**미래를 보려 해도 PITView 로는 경로가 없다.**

    커스텀 코드가 무엇을 하든 - 심지어 확보된 거래일 수(n_days)를 점수에 섞어도 -
    미래를 덧붙인 시장과 결과가 같다. 기준일 이하만 노출되기 때문이다.
    이것이 1차 방어이고, verify 의 검사는 이 구조가 깨졌을 때를 위한 2차 방어다.
    """
    short, long = _mk(60), _mk(90)
    until = short.dates[-1]
    probe = """
def signal(view, params):
    n = view.n_days()
    d = len(view.dates())
    out = {}
    for s in view.symbols:
        r = view.total_return(s, 20)
        if r is not None:
            out[s] = r + n + d
    return out
"""
    spec = from_code(edge_type="momentum", rank="TOP", code=probe)
    a = scores(spec, short, until)
    b = scores(spec, long, until)
    assert a and a == b, "PITView 를 통해 미래가 새어 들어왔다"
    assert verify(spec, short, long, until)["ok"]


def _check_verify_detects_divergence():
    """**2차 방어가 실제로 작동하는가.** 과거가 다른 두 시장을 주면 PIT 위반으로
    보고해야 한다 - 구조가 깨졌을 때 이 검사가 마지막으로 잡는다."""
    a_mkt = _mk(60)
    b_mkt = _mk(60)
    # 시그널이 **실제로 읽는** 날짜의 종가를 바꾼다(= append-only 가 아닌 시장).
    # lookback 20 이면 기준일과 20일 전을 보므로 그 구간을 건드려야 검출된다.
    for d in b_mkt.dates[30:50]:
        b_mkt.closes[(d, "A")] = b_mkt.closes[(d, "A")] * 1.5
    spec = from_code(edge_type="momentum", rank="TOP", code=_GOOD)
    r = verify(spec, a_mkt, b_mkt, a_mkt.dates[-1])
    assert not r["ok"] and any("PIT" in f for f in r["findings"]), r


def _check_return_contract():
    m = _mk()
    until = m.dates[-1]
    for code, why in (
        ("def signal(v, p): return [1, 2]", "리스트"),
        ("def signal(v, p): return {'A': 'x'}", "문자열 값"),
        ("def signal(v, p): return {'A': float('nan')}", "NaN"),
        ("def signal(v, p): return {'A': float('inf')}", "inf"),
    ):
        spec = from_code(edge_type="momentum", rank="TOP", code=code)
        try:
            scores(spec, m, until)
        except StrategyCodeError:
            pass
        else:
            raise AssertionError(f"{why} 가 통과했다")


def _check_hash_changes_when_code_changes():
    """**코드를 고치면 새 스펙이다** - 사전등록 지문이 달라져 새 시도로 계수된다."""
    a = from_code(edge_type="momentum", rank="TOP", code=_GOOD)
    b = from_code(edge_type="momentum", rank="TOP",
                  code=_GOOD.replace("20)", "21)"))
    assert a.spec_hash() != b.spec_hash()
    # 주석 한 줄만 바뀌어도 다른 스펙이다(사후 수정을 세기 위한 의도적 엄격함)
    c = from_code(edge_type="momentum", rank="TOP", code=_GOOD + "\n# note\n")
    assert a.spec_hash() != c.spec_hash()
    # 같은 코드·같은 파라미터면 같은 해시(재현 가능해야 한다)
    assert a.spec_hash() == from_code(edge_type="momentum", rank="TOP",
                                      code=_GOOD).spec_hash()


def _check_template_spec_hash_is_stable():
    a = from_template("momentum", {"lookback_days": 20})
    b = from_template("momentum", {"lookback_days": 20})
    c = from_template("momentum", {"lookback_days": 30})
    assert a.spec_hash() == b.spec_hash() != c.spec_hash()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
    _check_import_is_rejected();            print("  임포트 거부              OK")
    _check_dangerous_names_are_rejected();  print("  위험 이름 거부           OK")
    _check_dunder_escape_is_rejected();     print("  dunder 탈출 거부         OK")
    _check_toplevel_side_effect_is_rejected(); print("  최상위 실행문 거부      OK")
    _check_signature_is_enforced();         print("  시그니처 강제            OK")
    _check_no_builtins_leak_at_runtime();   print("  런타임 전역 제한         OK")
    _check_custom_matches_template();       print("  커스텀 == 템플릿 값      OK")
    _check_pitview_makes_leakage_unreachable()
    print("  PIT: 구조적 도달 불가    OK")
    _check_verify_detects_divergence();     print("  PIT 위반 검출(2차 방어)  OK")
    _check_return_contract();               print("  반환 계약(NaN/inf 포함)  OK")
    _check_hash_changes_when_code_changes(); print("  코드 변경 -> 새 스펙     OK")
    _check_template_spec_hash_is_stable();  print("  템플릿 스펙 해시 안정    OK")
    print("전략 스펙 12개 영역 통과. 커스텀 코드는 사전등록 지문에 해시로 들어간다.")
