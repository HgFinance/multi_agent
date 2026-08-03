#!/usr/bin/env python3
"""부서 Hermes Profile 계약 검사 - 8개 config.yaml 이 서로·문서와 어긋나지 않는가.

담당: 재일 (리서치·퀀트) — 2026-08-02
근거: 재일님 지시 "잘 작동하는지 실험 시작, 부족한 거 전부 개선".

▶ 왜 필요한가 (실측에서 나온 문제)
  Hermes 컨테이너에 붙여보니 우리가 넣은 키 중 상당수를 **Hermes 가 아예
  모른다**(tool_allowlist / forbidden_tools / hiring_priority / agentic_rag).
  즉 그 키들은 런타임 강제가 아니라 저장소 계약이다. 계약이라면 누군가
  검사해야 한다 - 안 그러면 페르소나를 추가하고 allowlist 를 빠뜨려도 아무도
  모른다. 이 스크립트가 그 검사다.

▶ 검사 항목
  1. YAML 파싱 + Hermes 가 실제로 읽는 키 존재 (model/agent.personalities/skills)
  2. env 키 배정이 CLAUDE.md 규칙과 일치 (부서마다 다르다 - 아무 키나 넣지 않는다)
  3. 페르소나 ↔ tool_allowlist ↔ hiring_priority 정합 (선언한 부서에 한해)
  4. 금지 도구가 '남의 본부 권한' 을 실제로 담고 있는가 (권한 경계 문구 검사)
  5. 모델 통일 (8개가 같은 provider/default - CLAUDE.md)

실행: python scripts/check_hermes_profiles.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHECK_VERSION = "hermes-profile-contract-check-v2"

# CLAUDE.md "env: 가 부서마다 다르다" - 아무 키나 넣지 않는다
ANTHROPIC = {"ceo-agent", "research-department", "qa-department",
             "quant-backtest-department"}
OPENAI = {"trading-department", "risk-management",
          "accounting-portfolio-department", "hr-department"}

# 부서별 기대 모델. 2026-08-02 재일님 결정: "내 부서는 내가 원하는 모델 쓰면 된다".
# 모델 선택은 **권한 경계가 아니라 소유자 재량**이다 - 리스크 거부권·주문 제출
# 같은 통제 경계와 층이 다르므로 부서마다 달라도 된다. 다만 "달라도 된다"가
# "아무거나 돼도 된다"는 아니므로, 소유자가 선언한 값과 다르면 이 검사가 잡는다
# (전역 동일성 강제를 여기서 이 표로 바꿨다 - 우연한 표류는 여전히 걸린다).
# Historical snapshot (2026-08-02): retained only to explain prior checker output.
LEGACY_EXPECTED_MODELS = {
    "research-department": "nous/poolside/laguna-s-2.1:free",     # 재일
    "quant-backtest-department": "nous/poolside/laguna-s-2.1:free",  # 재일
    "ceo-agent": "nous/poolside/laguna-s-2.1:free",              # 영주
    "hr-department": "nous/poolside/laguna-s-2.1:free",          # 영주
    # 도현: 2026-08-03 팀 합의대로 Sonnet. 단 api.anthropic.com 직접이 아니라
    # scripts/claude_code_proxy.py 를 거쳐 구독 플랜 한도로 부른다(각 config.yaml 주석).
    "trading-department": "anthropic/sonnet",                    # 도현
    "accounting-portfolio-department": "anthropic/sonnet",
    "risk-management": "nous/poolside/laguna-s-2.1:free",        # 동규
    "qa-department": "nous/poolside/laguna-s-2.1:free",
}

# Current runtime contract: every Hermes Head defaults to Codex/Luna.
# The legacy table above is retained only as historical audit context.
EXPECTED_MODELS = {
    "ceo-agent": "openai-codex/gpt-5.6-luna",
    "research-department": "openai-codex/gpt-5.6-luna",
    "trading-department": "openai-codex/gpt-5.6-luna",
    "risk-management": "openai-codex/gpt-5.6-luna",
    "quant-backtest-department": "openai-codex/gpt-5.6-luna",
    "accounting-portfolio-department": "openai-codex/gpt-5.6-luna",
    "qa-department": "openai-codex/gpt-5.6-luna",
    "hr-department": "openai-codex/gpt-5.6-luna",
}

# Worker model is a separate contract from the Hermes Head model above.
# This is intentionally a temporary low-memory test default; do not change
# EXPECTED_MODELS when switching employee Workers.
EXPECTED_WORKER_MODEL = "qwen3:1.7b"

DEPARTMENTS = {
    "ceo-agent": "00-ceo-office",
    "research-department": "01-research",
    "trading-department": "02-trading",
    "risk-management": "03-risk",
    "quant-backtest-department": "04-quant-backtest",
    "accounting-portfolio-department": "05-accounting-portfolio",
    "qa-department": "06-ai-qa-audit",
    "hr-department": "07-agent-workforce",
}

# 컨테이너 실측(2026-08-02): Hermes 코드가 인식하는 키 / 우리 저장소 전용 키
HERMES_KNOWN = ("model", "agent", "skills", "env")
REPO_ONLY = ("tool_allowlist", "forbidden_tools", "hiring_priority", "agentic_rag")

# 다른 본부 전속 권한 - 리서치·퀀트가 이걸 허용목록에 넣으면 경계 침범이다
CROSS_DEPT_TOOLS = ("oms.submit", "ledger.write", "risk.decision.write",
                    "risk.trading_state.write", "strategy.promote")


def load(dept: str) -> dict:
    p = ROOT / "departments" / DEPARTMENTS[dept] / "hermes" / "config.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def check_required_keys(dept: str, cfg: dict) -> list[str]:
    errs = []
    for k in ("model", "agent", "skills"):
        if k not in cfg:
            errs.append(f"{dept}: Hermes 가 읽는 필수 키 '{k}' 없음")
    model = cfg.get("model") or {}
    if not model.get("provider") or not model.get("default"):
        errs.append(f"{dept}: model.provider/default 미지정")
    if not (cfg.get("agent") or {}).get("personalities"):
        errs.append(f"{dept}: agent.personalities 없음 - 페르소나가 로드되지 않는다")
    return errs


def check_env_assignment(dept: str, cfg: dict) -> list[str]:
    env = cfg.get("env") or {}
    keys = set(env)
    want = "ANTHROPIC_API_KEY" if dept in ANTHROPIC else "OPENAI_API_KEY"
    other = "OPENAI_API_KEY" if dept in ANTHROPIC else "ANTHROPIC_API_KEY"
    errs = []
    if want not in keys:
        errs.append(f"{dept}: env 에 {want} 가 없다 (CLAUDE.md 배정)")
    if other in keys:
        errs.append(f"{dept}: env 에 {other} 가 있다 - 배정이 아닌 키다")
    return errs


def get_allowlist(cfg: dict):
    """tool_allowlist 위치가 부서마다 다르다 - 동규님 리스크본부는 agent: 아래,
    재일 리서치는 최상위였다(2026-08-02 이 검사가 잡았다). Hermes 가 읽지 않는
    키라 둘 다 동작은 같지만, 계약 문서가 부서마다 다른 모양이면 검사·리뷰가
    새므로 **읽을 때 둘 다 본다**(관용) + 아래에서 위치 불일치를 경고한다."""
    return (cfg.get("agent") or {}).get("tool_allowlist") or cfg.get("tool_allowlist")


def check_persona_consistency(dept: str, cfg: dict) -> list[str]:
    personas = set((cfg.get("agent") or {}).get("personalities") or {})
    errs = []
    allow = get_allowlist(cfg)
    if allow is not None:
        missing = personas - set(allow)
        extra = set(allow) - personas
        if missing:
            errs.append(f"{dept}: 허용목록 없는 페르소나 {sorted(missing)} - "
                        f"권한이 정의되지 않은 직원이다")
        if extra:
            errs.append(f"{dept}: 존재하지 않는 페르소나의 허용목록 {sorted(extra)}")
    hp = cfg.get("hiring_priority")
    if hp is not None:
        ghost = set(hp) - personas
        if ghost:
            errs.append(f"{dept}: hiring_priority 에만 있는 페르소나 {sorted(ghost)}")
    return errs


def check_boundary(dept: str, cfg: dict) -> list[str]:
    """남의 본부 전속 권한을 허용목록에 넣지 않았는가."""
    allow = get_allowlist(cfg) or {}
    errs = []
    for persona, tools in allow.items():
        for t in tools or []:
            if t in CROSS_DEPT_TOOLS:
                errs.append(f"{dept}/{persona}: 타 본부 전속 권한 '{t}' 를 "
                            f"허용목록에 넣었다 - 권한 이전 금지(CLAUDE.md)")
    return errs


def check_worker_model(dept: str, cfg: dict) -> list[str]:
    """Verify the employee Worker model without changing the Head contract."""
    runtime = cfg.get("employee_runtime") or {}
    model_default = runtime.get("model_default")
    active_model = (runtime.get("model_selection") or {}).get("active_model")
    errors: list[str] = []
    if model_default != EXPECTED_WORKER_MODEL:
        errors.append(
            f"{dept}: employee_runtime.model_default expected "
            f"{EXPECTED_WORKER_MODEL}, got {model_default}"
        )
    if active_model != EXPECTED_WORKER_MODEL:
        errors.append(
            f"{dept}: employee_runtime.model_selection.active_model expected "
            f"{EXPECTED_WORKER_MODEL}, got {active_model}"
        )
    return errors


def audit() -> tuple[list[str], list[str], dict]:
    errors: list[str] = []
    warns: list[str] = []
    models: dict[str, str] = {}
    for dept in DEPARTMENTS:
        try:
            cfg = load(dept)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{dept}: config.yaml 읽기 실패 {type(e).__name__} {e}")
            continue
        errors += check_required_keys(dept, cfg)
        errors += check_env_assignment(dept, cfg)
        errors += check_persona_consistency(dept, cfg)
        errors += check_boundary(dept, cfg)
        errors += check_worker_model(dept, cfg)
        m = cfg.get("model") or {}
        models[dept] = f"{m.get('provider')}/{m.get('default')}"
        if get_allowlist(cfg) is None:
            warns.append(f"{dept}: tool_allowlist 미선언 - 권한 경계가 문서에 없다")
        elif cfg.get("tool_allowlist") is not None:
            warns.append(f"{dept}: tool_allowlist 가 최상위에 있다 - 선행 사례"
                         f"(03-risk)는 agent: 아래다. 위치를 맞추면 리뷰가 쉽다")
    for dept, got in sorted(models.items()):
        want = EXPECTED_MODELS.get(dept)
        if want and got != want:
            errors.append(f"{dept}: 모델이 선언과 다르다 - 기대 {want}, 실제 {got}. "
                          f"의도한 변경이면 EXPECTED_MODELS 를 함께 고친다")
    return errors, warns, models


# ---------------------------------------------------------------------------
# 자체 점검 - 파일 없이 규칙만
# ---------------------------------------------------------------------------

def _check_rules():
    ok = {"model": {"provider": "nous", "default": "x"},
          "agent": {"personalities": {"a": "p", "b": "p"}},
          "skills": ["hermes-agent"],
          "env": {"ANTHROPIC_API_KEY": "${X}"},
          "tool_allowlist": {"a": ["r.read"], "b": ["r.read"]}}
    assert not check_required_keys("research-department", ok)
    assert not check_env_assignment("research-department", ok)
    assert not check_persona_consistency("research-department", ok)
    assert not check_boundary("research-department", ok)

    # 페르소나를 늘리고 허용목록을 빠뜨린 경우 - 실제로 흔한 실수
    grew = {**ok, "agent": {"personalities": {"a": "p", "b": "p", "c": "p"}}}
    assert any("허용목록 없는 페르소나" in e
               for e in check_persona_consistency("research-department", grew))
    # 배정 아닌 키
    wrong = {**ok, "env": {"OPENAI_API_KEY": "${X}"}}
    errs = check_env_assignment("research-department", wrong)
    assert len(errs) == 2, errs
    # 권한 경계 침범
    bad = {**ok, "tool_allowlist": {"a": ["oms.submit"], "b": []}}
    assert any("타 본부 전속 권한" in e
               for e in check_boundary("research-department", bad))
    print("  규칙 자체 점검           OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"{CHECK_VERSION}")
    _check_rules()

    errors, warns, models = audit()
    print(f"\n부서 {len(DEPARTMENTS)}개 검사 / 모델 {set(models.values())}")
    for w in warns:
        print(f"  ⚠ {w}")
    if errors:
        print(f"\n✗ 위반 {len(errors)}건")
        for e in errors:
            print(f"  - {e}")
        raise SystemExit(1)
    print("\n✓ Profile 계약 위반 없음")
    print("참고: tool_allowlist·forbidden_tools·hiring_priority·agentic_rag 는 "
          "Hermes 가 읽지 않는 저장소 전용 키다(2026-08-02 컨테이너 실측). "
          "런타임 강제는 research-api/market-api 쪽 Tool Gateway 가 필요하다.")
