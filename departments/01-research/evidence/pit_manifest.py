#!/usr/bin/env python3
"""Research Tool PIT Capability Manifest - 과거 Replay 에서 무엇을 부르면 안 되는가.

담당: 재일 (리서치본부)
근거: docs/02-engineering/RESEARCH_QUANT_AGENTIC_FRAMEWORK.md Phase RQF-0
        "모든 Research Tool 에 `as_known_at` 지원 여부 선언"
        완료 기준 "`as_known_at` 미지원 Tool 이 과거 Replay 에서 호출되면 Fail-closed 한다"
      docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md `RQF-02` (P0)
        완료 조건 "과거 Replay 에서 최신 조회 0건"
      CLAUDE.md 개발원칙 5 "미래 데이터가 Backtest 와 과거 Replay 에 들어가지 않게 한다"

▶ 왜 필요한가 (실측 2026-08-03)
  research-api 8개 엔드포인트 중 7개가 as_of 를 받는다. 그런데 **market-api 7개 중
  시간 컷오프가 있는 것은 /bars 의 `to` 하나뿐이다.** /snapshot, /breadth, /regime/daily,
  /microstructure 는 언제 불러도 **지금 값**을 준다.

  지금은 그게 문제가 아니다 - 실시간 실행에서는 '지금'이 곧 컷오프이기 때문이다.
  `pipeline_runs.as_known_at = started` 는 그래서 **참**이다.

  문제는 과거 Replay 다. as_of=2026-06-01 로 Packet 을 재현하는 순간 /breadth 는
  8월 값을 돌려주고, 그 숫자가 6월 판단의 근거로 기록된다. 조용히. 오류 없이.
  **그게 look-ahead 이고, 백테스트 전체를 무효로 만든다.**

▶ 이 모듈이 하는 일과 하지 않는 일
  한다  : 각 Tool 이 as_known_at 을 지원하는지 **선언**하고, Replay 에서 미지원
          Tool 호출을 **예외로 막는다**(fail-closed).
  안 한다: 미지원 Tool 을 지원하게 만들지 않는다. 그건 market-api 에 시간 축을
          넣는 별도 과제이며 공통 Platform 결정이 필요하다(ADR-0003 9절 1번).

  **선언은 거짓말할 수 있다.** 그래서 자체 점검이 API 소스를 AST 로 읽어 선언과
  대조한다 - 선언에 없는 엔드포인트가 생기거나, SUPPORTED 라고 해놓고 실제 함수에
  시간 인자가 없으면 실패한다. `scripts/check_gateway_coverage.py` 와 같은 방식이다.

실행: python evidence/pit_manifest.py     # 자체 점검 (네트워크·DB 없음)
"""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

MODULE_VERSION = "research-pit-manifest-v1"

_HERE = Path(__file__).resolve().parent
_API_DIR = _HERE.parent / "api"


class PitSupport(str, Enum):
    """이 Tool 이 '그때 알 수 있던 것' 만 돌려줄 수 있는가."""

    SUPPORTED = "SUPPORTED"        # 시간 인자를 받고 그 시점으로 잘라 준다
    UNSUPPORTED = "UNSUPPORTED"    # 언제 불러도 지금 값을 준다 - Replay 에서 금지
    NOT_APPLICABLE = "N/A"         # 시간 개념이 없다 (health 등). Replay 에서 무해


class RunMode(str, Enum):
    LIVE = "LIVE"        # 지금 = 컷오프. 전부 허용
    REPLAY = "REPLAY"    # 과거 재현. UNSUPPORTED 는 예외


class PitViolation(RuntimeError):
    """과거 Replay 에서 미래를 볼 수 있는 Tool 을 불렀다.

    이것을 경고로 낮추지 않는다. 경고는 무시되고, 무시된 look-ahead 는
    백테스트 결과를 조용히 낙관적으로 만든다.
    """


@dataclass(frozen=True)
class ToolCapability:
    service: str          # 'research-api' | 'market-api'
    path: str             # FastAPI 경로 (경로 파라미터 포함)
    support: PitSupport
    time_param: str | None  # 시간 컷오프 인자 이름 (SUPPORTED 면 필수)
    note: str

    def __post_init__(self) -> None:
        if self.support is PitSupport.SUPPORTED and not self.time_param:
            raise ValueError(f"{self.path}: SUPPORTED 인데 time_param 이 없다")
        if self.support is not PitSupport.SUPPORTED and self.time_param:
            raise ValueError(f"{self.path}: {self.support.value} 인데 time_param 이 있다")
        if not self.note.strip():
            raise ValueError(f"{self.path}: note 없는 선언은 선언이 아니다")


# ---------------------------------------------------------------------------
# 선언 (실측 2026-08-03)
# ---------------------------------------------------------------------------
#
# ▶ 이 표를 고칠 때는 코드를 먼저 고친다. 반대로 하면 자체 점검이 막는다.

MANIFEST: tuple[ToolCapability, ...] = (
    # ── research-api ────────────────────────────────────────────────────────
    ToolCapability("research-api", "/health", PitSupport.NOT_APPLICABLE, None,
                   "생존 확인. 시장 데이터를 돌려주지 않는다"),
    ToolCapability("research-api", "/evidence/news", PitSupport.SUPPORTED, "as_of",
                   "observed_at <= as_of. 가중치도 View 의 now() 가 아니라 as_of 기준"),
    ToolCapability("research-api", "/evidence/disclosures", PitSupport.SUPPORTED, "as_of",
                   "공시 observed_at 컷오프"),
    ToolCapability("research-api", "/evidence/financials", PitSupport.SUPPORTED, "as_of",
                   "DISTINCT ON 으로 as_of 시점의 최신 revision - 정정본이 원본을 덮지 않는다"),
    ToolCapability("research-api", "/universe/restrictions", PitSupport.SUPPORTED, "as_of",
                   "거래정지·관리종목 상태의 KST 날짜 기준"),
    ToolCapability("research-api", "/macro/observations", PitSupport.SUPPORTED, "as_of",
                   "vintage_date 로 '그때 발표돼 있던 값' 을 준다"),
    ToolCapability("research-api", "/evidence/search", PitSupport.SUPPORTED, "as_of",
                   "⚠ as_of 가 선택 인자라 미지정 시 필터가 사라진다. Replay 는 반드시 명시"),
    ToolCapability("research-api", "/evidence/stories", PitSupport.SUPPORTED, "as_of",
                   "Story Cluster 구성 시점 컷오프"),

    # ── market-api ──────────────────────────────────────────────────────────
    ToolCapability("market-api", "/health", PitSupport.NOT_APPLICABLE, None,
                   "생존 확인"),
    ToolCapability("market-api", "/bars/{symbol}", PitSupport.SUPPORTED, "to",
                   "`to` 로 끝 시각을 자른다. 다만 evidence/bundle.py 는 아직 안 쓴다 - "
                   "Replay 배선 시 반드시 넘겨야 한다"),
    ToolCapability("market-api", "/snapshot/{symbol}", PitSupport.UNSUPPORTED, None,
                   "최신 스냅샷 1행. 시간 인자가 없다"),
    # 2026-08-04: 금지 -> 파라미터. market_breadth 는 event_time 으로 이력이
    # 원래 다 있었고 컷오프만 없었다. 금지는 도구를 없애지만 파라미터는 도구를
    # 살린다 - **에이전트가 값을 하는지 증명하려면 과거로 돌려볼 수 있어야 한다.**
    ToolCapability("market-api", "/breadth", PitSupport.SUPPORTED, "as_of",
                   "as_of 시각까지의 Breadth. 없으면 최신"),
    # 2026-08-04 신설. **Replay 에서 금지다** - 오늘의 방법 성적은 Replay 시점
    # 이후에 일어난 결과까지 포함한다. 6월 판단에 "이 기법이 잘 맞는다" 를
    # 쓰면 그건 그때 몰랐던 것을 아는 셈이고, 정확히 look-ahead 다.
    # 학습 되먹임과 백테스트는 이 지점에서 반드시 갈라져야 한다.
    ToolCapability("research-api", "/methods/performance", PitSupport.UNSUPPORTED,
                   None, "누적 성적은 Replay 시점 이후 결과를 포함한다"),
    # 2026-08-04 신설. /dq/summary 와 같은 이유 - 그때 몰랐던 품질 결함을
    # 아는 셈이 된다.
    ToolCapability("market-api", "/dq/windows", PitSupport.UNSUPPORTED, None,
                   "현재 품질 감사. Replay 근거로 쓰면 그때 몰랐던 결함을 아는 셈"),
    ToolCapability("market-api", "/dq/summary", PitSupport.UNSUPPORTED, None,
                   "현재 데이터 품질. Replay 판단 근거로 쓰면 그때 몰랐던 결함을 아는 셈"),
    # 2026-08-04: 같은 이유로 해제. 일봉 bucket_time 으로 계산하므로 이력이
    # 있다. RES-07 이 Replay 에서 가장 위험한 경로였는데, 이제 그 경로가
    # **막힌 것이 아니라 시점이 지정된다.**
    ToolCapability("market-api", "/regime/daily", PitSupport.SUPPORTED, "as_of",
                   "as_of 거래일까지의 레짐. 없으면 최신"),
    ToolCapability("market-api", "/microstructure/{symbol}", PitSupport.UNSUPPORTED, None,
                   "최신 미시구조 집계. RES-03 이 쓴다"),
)

_BY_PATH = {(c.service, c.path): c for c in MANIFEST}


def capability(service: str, path: str) -> ToolCapability:
    """선언을 찾는다. **없으면 예외** - 모르는 Tool 을 안전하다고 보지 않는다."""
    cap = _BY_PATH.get((service, path))
    if cap is None:
        raise PitViolation(
            f"{service}{path} 는 PIT Manifest 에 선언이 없다 - 새 엔드포인트를 "
            f"만들었으면 evidence/pit_manifest.py 의 MANIFEST 에 먼저 등록한다")
    return cap


def ensure_callable(service: str, path: str, *, run_mode: RunMode | str) -> ToolCapability:
    """이 실행 모드에서 이 Tool 을 불러도 되는가. 안 되면 예외.

    LIVE 에서는 전부 통과한다 - 실시간에서는 '지금' 이 곧 컷오프이므로
    UNSUPPORTED 여도 거짓이 아니다. REPLAY 에서만 막는다.
    """
    mode = RunMode(run_mode)
    cap = capability(service, path)
    if mode is RunMode.REPLAY and cap.support is PitSupport.UNSUPPORTED:
        raise PitViolation(
            f"REPLAY 에서 {service}{path} 를 부를 수 없다 - {cap.note}. "
            f"이 Tool 은 언제 불러도 지금 값을 돌려주므로 과거 재현에 쓰면 "
            f"look-ahead 가 된다. 시간 축을 넣거나, 이 근거 없이 Packet 을 만든다")
    return cap


def replay_blocked() -> tuple[ToolCapability, ...]:
    """Replay 에서 막히는 Tool 목록. 리포트·계획이 이 목록을 인용한다."""
    return tuple(c for c in MANIFEST if c.support is PitSupport.UNSUPPORTED)


def coverage_summary() -> dict:
    """선언 요약. '몇 개 중 몇 개가 PIT 인가' 를 숨기지 않는다."""
    counts: dict[str, int] = {}
    for c in MANIFEST:
        counts[c.support.value] = counts.get(c.support.value, 0) + 1
    return {
        "total": len(MANIFEST),
        "by_support": counts,
        "replay_blocked": [f"{c.service}{c.path}" for c in replay_blocked()],
    }


# ---------------------------------------------------------------------------
# 정적 대조 - 선언이 코드와 어긋나면 잡는다
# ---------------------------------------------------------------------------

def _endpoints_in_source(py: Path) -> dict[str, set[str]]:
    """FastAPI 소스에서 (경로 -> 인자 이름 집합) 를 뽑는다. 실행하지 않는다."""
    tree = ast.parse(py.read_text(encoding="utf-8"))
    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            if dec.func.attr not in ("get", "post", "put", "delete"):
                continue
            if not (dec.args and isinstance(dec.args[0], ast.Constant)):
                continue
            args = {a.arg for a in node.args.args + node.args.kwonlyargs}
            found[dec.args[0].value] = args
    return found


def audit_against_source() -> list[str]:
    """선언 vs 실제 코드. 반환이 빈 목록이면 일치한다."""
    problems: list[str] = []
    for service, filename in (("research-api", "main.py"), ("market-api", "market_api.py")):
        src = _API_DIR / filename
        if not src.exists():          # 소스가 없으면 대조를 조용히 건너뛰지 않는다
            problems.append(f"{service}: 소스를 찾을 수 없다 ({src})")
            continue
        actual = _endpoints_in_source(src)
        declared = {c.path: c for c in MANIFEST if c.service == service}

        for path in sorted(set(actual) - set(declared)):
            problems.append(f"{service}{path}: 코드에 있는데 Manifest 에 선언이 없다")
        for path in sorted(set(declared) - set(actual)):
            problems.append(f"{service}{path}: Manifest 에 있는데 코드에 없다(경로 변경?)")

        for path, cap in declared.items():
            if path not in actual:
                continue
            if cap.support is PitSupport.SUPPORTED and cap.time_param not in actual[path]:
                problems.append(
                    f"{service}{path}: SUPPORTED 로 선언했는데 함수에 "
                    f"'{cap.time_param}' 인자가 없다 - 선언이 거짓이다")
            if cap.support is PitSupport.UNSUPPORTED:
                # 시간 인자가 생겼는데 선언을 안 고친 경우 - 과소 선언도 결함이다
                for candidate in ("as_of", "as_known_at", "to"):
                    if candidate in actual[path]:
                        problems.append(
                            f"{service}{path}: UNSUPPORTED 인데 함수에 "
                            f"'{candidate}' 가 생겼다 - Manifest 를 갱신한다")
    return problems


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·DB 없음
# ---------------------------------------------------------------------------

def _check_declaration_invariants():
    seen = set()
    for c in MANIFEST:
        key = (c.service, c.path)
        assert key not in seen, f"중복 선언 {key}"
        seen.add(key)
    # dataclass __post_init__ 이 실제로 막는가
    for bad, why in (
        (dict(service="x", path="/p", support=PitSupport.SUPPORTED,
              time_param=None, note="n"), "SUPPORTED 인데 time_param 없음"),
        (dict(service="x", path="/p", support=PitSupport.UNSUPPORTED,
              time_param="as_of", note="n"), "UNSUPPORTED 인데 time_param 있음"),
        (dict(service="x", path="/p", support=PitSupport.NOT_APPLICABLE,
              time_param=None, note="  "), "note 없음"),
    ):
        try:
            ToolCapability(**bad)
            raise AssertionError(f"불량 선언이 통과했다: {why}")
        except ValueError:
            pass
    print("  선언 불변식              OK")


def _check_replay_fail_closed():
    """RQF-0 완료 기준 그 자체 - 미지원 Tool 이 Replay 에서 막히는가."""
    # LIVE 에서는 전부 통과한다
    for c in MANIFEST:
        ensure_callable(c.service, c.path, run_mode=RunMode.LIVE)

    blocked = replay_blocked()
    assert blocked, "UNSUPPORTED 가 하나도 없다 - 실측(2026-08-03)과 다르다"
    for c in blocked:
        try:
            ensure_callable(c.service, c.path, run_mode=RunMode.REPLAY)
            raise AssertionError(f"REPLAY 에서 {c.path} 가 통과했다")
        except PitViolation as e:
            assert "look-ahead" in str(e), str(e)

    # SUPPORTED 는 REPLAY 에서도 통과한다
    for c in MANIFEST:
        if c.support is not PitSupport.UNSUPPORTED:
            ensure_callable(c.service, c.path, run_mode=RunMode.REPLAY)

    # 모르는 Tool 은 안전하다고 보지 않는다 (fail-closed)
    try:
        ensure_callable("market-api", "/새엔드포인트", run_mode=RunMode.LIVE)
        raise AssertionError("선언 없는 Tool 이 통과했다")
    except PitViolation as e:
        assert "선언이 없다" in str(e)
    print("  Replay Fail-closed       OK")


def _check_matches_source():
    """선언이 실제 API 코드와 일치하는가 - 이게 이 모듈의 존재 이유다."""
    problems = audit_against_source()
    assert not problems, "선언과 코드 불일치:\n  - " + "\n  - ".join(problems)
    print("  선언 vs 코드 정적 대조   OK")


def _check_summary_hides_nothing():
    s = coverage_summary()
    assert s["total"] == len(MANIFEST)
    assert sum(s["by_support"].values()) == s["total"], "합계가 안 맞는다"
    # 막히는 목록을 빈 값으로 숨기지 않는다
    assert s["replay_blocked"], "막히는 Tool 이 있는데 요약이 비었다"
    assert len(s["replay_blocked"]) == len(replay_blocked())
    print("  요약이 숨기지 않는가     OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_declaration_invariants()
    _check_replay_fail_closed()
    _check_matches_source()
    _check_summary_hides_nothing()

    s = coverage_summary()
    print(f"PIT Manifest 4개 영역 통과. Tool {s['total']}개 중 "
          f"{s['by_support'].get('SUPPORTED', 0)}개 PIT, "
          f"{s['by_support'].get('UNSUPPORTED', 0)}개 Replay 금지")
    for p in s["replay_blocked"]:
        print(f"    Replay 금지: {p}")
