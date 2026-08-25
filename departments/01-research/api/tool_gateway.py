#!/usr/bin/env python3
"""Tool Gateway - 허용목록을 선언에서 **실제 강제**로 바꾼다.

소유: 재일 (리서치본부)
근거: 재일님 지시 2026-08-02 "발생한 모든 문제들 해결해놔" 중 ④.
      2026-08-02 컨테이너 실측: Hermes 는 tool_allowlist·forbidden_tools 를
      **읽지도 않는다**(grep 0건). 즉 그 목록은 지금까지 계약 문서였을 뿐
      런타임에서 아무것도 막지 못했다. 강제는 도구가 실제로 있는 곳,
      즉 조회면(research-api)에서 해야 성립한다.

▶ 계약
  호출자는 `X-Agent-Persona: <페르소나>` 헤더로 자기를 밝힌다. 게이트웨이는
  부서 config.yaml 의 agent.tool_allowlist 를 읽어 그 페르소나가 이 엔드포인트의
  scope 를 가졌는지 본다. 없으면 403.

▶ 설계에서 지킨 것
  1. **선언의 단일 출처는 config.yaml 이다.** 여기에 목록을 복제하지 않는다 -
     복제하면 둘이 어긋나고, 어긋난 쪽이 조용히 이긴다.
  2. **엔드포인트 -> scope 매핑은 코드에 명시**한다. 매핑이 없는 엔드포인트는
     '보호되지 않음'이 아니라 **설정 오류**로 본다(fail-closed).
  3. **모르는 페르소나는 거부**한다. 헤더가 없으면 거부한다 - '누군지 모르면
     통과'는 허용목록을 무의미하게 만든다.
  4. forbidden_tools 는 허용목록보다 강하다. 둘 다에 있으면 거부한다.
  5. 이행기 배려: ENFORCE=false 면 위반을 **거부 대신 기록**한다. 기존 호출자
     (분석가 코드)가 헤더를 아직 안 붙이므로, 먼저 관측하고 나중에 조인다.
     다만 기본값은 '기록'이고 그 사실이 응답 헤더로 드러난다 - 조용한 무강제 금지.

실행: python api/tool_gateway.py    # 자체 점검 (네트워크·DB 없음)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

GATEWAY_VERSION = "research-tool-gateway-v1"
_CONFIG = Path(__file__).resolve().parent.parent / "hermes" / "config.yaml"

PERSONA_HEADER = "x-agent-persona"

# 엔드포인트 경로 -> 필요한 scope. **매핑 없는 경로는 설정 오류다.**
# 표기는 AGENT_EMPLOYEE_PROFILES.md 2.2절 <domain>.<resource>.<verb>.
ENDPOINT_SCOPES: dict[str, str] = {
    "/evidence/news": "research.evidence.news.read",
    "/evidence/disclosures": "research.evidence.disclosures.read",
    "/evidence/financials": "research.evidence.financials.read",
    "/evidence/search": "research.evidence.search",
    "/evidence/stories": "research.evidence.stories.read",
    "/macro/observations": "research.macro.read",
    "/universe/restrictions": "research.universe.read",
    # market-api 면 (2026-08-02 추가). 여기 없으면 scope_for 가 예외를 내므로
    # market-api 에 게이트웨이를 붙이는 순간 전 경로가 500 이 된다 - 붙이기
    # 전에 매핑이 먼저 있어야 한다. config.yaml 이 이미 market.* 를 선언해
    # 놓고도 **어디서도 강제되지 않던** 구멍을 메운다.
    "/snapshot": "market.snapshot.read",
    "/bars": "market.bars.read",
    # /levels 는 일봉에서 계산한 파생물이라 같은 자료·같은 민감도다.
    # 새 scope 를 만들면 부서 config.yaml 에 선언이 없어 강제 모드에서
    # gateway_misconfigured(500) 로 죽는다.
    "/levels": "market.bars.read",
    "/breadth": "market.breadth.read",
    "/dq/summary": "market.dq.read",
    "/regime/daily": "market.regime.read",
    "/microstructure": "market.microstructure.read",
    # 2026-08-04 추가. **엔드포인트만 만들고 여기를 안 고치면 강제 모드에서
    # 500 이다** - 게이트웨이가 fail-closed 라 모르는 경로를 통과시키지
    # 않는다(실측: /methods/performance 가 gateway_misconfigured 로 막혔다).
    # 새 라우트를 낼 때마다 이 표가 같이 커져야 한다.
    "/dq/windows": "market.dq.read",
    "/dq/bar_freshness": "market.dq.read",
    "/calendar/sessions_since": "research.universe.read",
    "/methods/performance": "research.methods.read",
}
# 인증 없이 열어두는 경로 - 상태 확인은 권한 판단의 대상이 아니다.
# /docs/oauth2-redirect 는 FastAPI 가 자동으로 다는 라우트다. 빼먹으면
# scope_for 가 예외를 내 **강제 모드에서 500** 이 된다(2026-08-02 자체 점검이
# 적발). 문서 표면은 통째로 열어두는 게 맞다.
OPEN_PATHS = ("/health", "/health/ready", "/docs", "/docs/oauth2-redirect",
              "/openapi.json", "/redoc")


class GatewayConfigError(RuntimeError):
    """게이트웨이 설정 오류. 조용히 통과시키지 않는다."""


def load_allowlist(config_path: Path | None = None) -> tuple[dict, list]:
    """(페르소나 -> scope 목록, 금지 도구). 단일 출처는 config.yaml 이다."""
    p = config_path or _CONFIG
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    agent = cfg.get("agent") or {}
    allow = agent.get("tool_allowlist") or cfg.get("tool_allowlist") or {}
    forbid = agent.get("forbidden_tools") or cfg.get("forbidden_tools") or []
    return allow, list(forbid)


def scope_for(path: str) -> str:
    """경로 -> scope. 모르는 경로는 예외 - '보호 안 됨'으로 새지 않는다."""
    base = path.split("?", 1)[0].rstrip("/") or "/"
    for prefix, scope in ENDPOINT_SCOPES.items():
        if base == prefix or base.startswith(prefix + "/"):
            return scope
    raise GatewayConfigError(
        f"scope 매핑이 없는 경로: {base} - ENDPOINT_SCOPES 에 추가할 것")


# 위반 코드. **HTTP 헤더는 latin-1 만 담는다**(2026-08-02 실측: 한국어 사유를
# 헤더에 넣었더니 UnicodeEncodeError 로 500 이 났다). 헤더에는 ASCII 코드만
# 싣고, 사람이 읽는 한국어 사유는 본문·로그로 보낸다.
NO_PERSONA = "no_persona"
UNKNOWN_PERSONA = "unknown_persona"
FORBIDDEN_TOOL = "forbidden_tool"
SCOPE_DENIED = "scope_denied"


def check_access(persona: str | None, path: str, allow: dict,
                 forbid: list) -> tuple[bool, str, str]:
    """(허용 여부, 코드, 한국어 사유). 순수 함수라 자체 점검이 전부 검사한다."""
    scope = scope_for(path)          # 매핑 없으면 여기서 예외(설정 오류)
    if not persona:
        return False, NO_PERSONA, f"페르소나 헤더({PERSONA_HEADER}) 없음 - 익명 호출 거부"
    if persona not in allow:
        return False, UNKNOWN_PERSONA, f"모르는 페르소나 '{persona}' - 허용목록에 없다"
    if scope in forbid:
        return False, FORBIDDEN_TOOL, f"'{scope}' 는 부서 금지 도구다(허용목록보다 강하다)"
    if scope not in (allow.get(persona) or []):
        return False, SCOPE_DENIED, (f"페르소나 '{persona}' 에게 '{scope}' 권한이 "
                                     f"없다 - 보유: {sorted(allow.get(persona) or [])}")
    return True, "ok", "ok"


def is_open_path(path: str) -> bool:
    base = path.split("?", 1)[0].rstrip("/") or "/"
    return base in OPEN_PATHS or base == ""


ENFORCE_ENV = "TOOL_GATEWAY_ENFORCE"                  # research-api
ENFORCE_ENV_MARKET = "TOOL_GATEWAY_ENFORCE_MARKET"    # market-api (별도 스위치)


def enforcing(env_var: str = ENFORCE_ENV) -> bool:
    """강제 모드. 기본은 기록만 - 기존 호출자가 헤더를 아직 안 붙인다.

    끄고 켜는 스위치를 둔 이유: 강제부터 켜면 파이프라인이 통째로 멈춘다.
    다만 **기본이 무강제라는 사실이 응답 헤더로 드러나야** 한다(아래 미들웨어).

    ▶ 서비스마다 스위치가 따로다 (2026-08-02)
      research-api 는 호출자가 전부 우리 부서 코드라 강제로 올렸다. market-api
      는 다르다 - 퀀트본부와 **팀원이 8036 을 직접 조회**한다. 같은 스위치를
      쓰면 research 를 강제로 올리는 순간 팀원 조회가 403 이 된다. 그래서
      market-api 는 자기 변수로 따로 켠다(기본 관찰).
    """
    return os.environ.get(env_var, "").lower() in ("1", "true", "yes")


def install(app, *, config_path: Path | None = None,
            env_var: str = ENFORCE_ENV) -> None:
    """FastAPI 앱에 게이트웨이를 붙인다."""
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    allow, forbid = load_allowlist(config_path)
    enforce = enforcing(env_var)

    class _Gateway(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            path = request.url.path
            if is_open_path(path):
                return await call_next(request)
            persona = request.headers.get(PERSONA_HEADER)
            try:
                ok, code, why = check_access(persona, path, allow, forbid)
            except GatewayConfigError as e:
                # 설정 오류는 통과가 아니라 500 - 보호 구멍을 조용히 두지 않는다
                return JSONResponse({"error": "gateway_misconfigured",
                                     "detail": str(e)}, status_code=500)
            if not ok:
                # **강제 모드에서도 남긴다.** 예전에는 조기 반환하며 로그를
                # 건너뛰어 "403 인데 왜인지 모르는" 상태가 됐다(2026-08-02 실측).
                # 막는 것과 왜 막았는지 알리는 것은 별개 의무다.
                print(f"[tool-gateway] {code}: {why} (path={path}, "
                      f"persona={persona!r}, mode={'enforce' if enforce else 'observe'})",
                      file=sys.stderr, flush=True)
            if not ok and enforce:
                return JSONResponse(
                    {"error": "forbidden", "code": code, "detail": why,
                     "persona": persona, "path": path}, status_code=403)
            resp = await call_next(request)
            resp.headers["X-Tool-Gateway"] = (
                "enforce" if enforce else "observe")
            if not ok:
                # 무강제 모드에서도 위반은 응답에 드러난다 - 조용한 통과 금지.
                # 헤더는 ASCII 코드만(latin-1 제약), 한국어 사유는 위에서 로그로.
                resp.headers["X-Tool-Gateway-Violation"] = code
            return resp

    app.add_middleware(_Gateway)


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·DB 없음
# ---------------------------------------------------------------------------

_ALLOW = {"geopolitical-analyst": ["research.macro.read"],
          "fundamental-analyst": ["research.evidence.financials.read",
                                  "research.evidence.search"]}
_FORBID = ["oms.submit", "research.documents.delete"]


def _check_scope_mapping():
    assert scope_for("/macro/observations") == "research.macro.read"
    assert scope_for("/evidence/news?symbol=000660") == "research.evidence.news.read"
    assert scope_for("/universe/restrictions/") == "research.universe.read"
    try:
        scope_for("/some/new/endpoint")
        raise AssertionError("매핑 없는 경로가 통과했다 - 보호 구멍이 생긴다")
    except GatewayConfigError:
        pass
    print("  경로->scope 매핑         OK")


def _check_access_rules():
    ok, _code, _ = check_access("geopolitical-analyst", "/macro/observations",
                               _ALLOW, _FORBID)
    assert ok
    # 남의 권한은 못 쓴다
    ok2, code2, why = check_access("geopolitical-analyst", "/evidence/financials",
                                   _ALLOW, _FORBID)
    assert not ok2 and code2 == SCOPE_DENIED and "권한이 없다" in why, why
    # 익명 거부
    ok3, code3, why3 = check_access(None, "/macro/observations", _ALLOW, _FORBID)
    assert not ok3 and code3 == NO_PERSONA and "익명" in why3
    # 모르는 페르소나 거부
    ok4, code4, _why4 = check_access("someone-else", "/macro/observations", _ALLOW, _FORBID)
    assert not ok4 and code4 == UNKNOWN_PERSONA
    print("  접근 판정                OK")


def _check_forbidden_wins():
    """금지가 허용보다 강하다 - 둘 다에 있으면 거부."""
    allow = {"x": ["research.macro.read"]}
    forbid = ["research.macro.read"]
    ok, code, why = check_access("x", "/macro/observations", allow, forbid)
    assert not ok and code == FORBIDDEN_TOOL, (code, why)
    # 헤더에 실리는 코드는 반드시 ASCII 다 - 한국어를 넣으면 500 이 난다(실측)
    for c in (NO_PERSONA, UNKNOWN_PERSONA, FORBIDDEN_TOOL, SCOPE_DENIED):
        c.encode("latin-1")
    print("  금지 우선                OK")


def _check_real_config_covers_endpoints():
    """부서 config 의 scope 와 엔드포인트 매핑이 실제로 맞물리는지.

    선언만 있고 아무 엔드포인트도 안 지키는 scope, 반대로 아무 페르소나도
    못 쓰는 엔드포인트를 찾아낸다 - 둘 다 '있는 줄 알았는데 없는' 상태다.
    """
    allow, forbid = load_allowlist()
    declared = {s for scopes in allow.values() for s in scopes}
    endpoint_scopes = set(ENDPOINT_SCOPES.values())

    orphan_endpoints = endpoint_scopes - declared
    assert not orphan_endpoints, \
        f"아무 페르소나도 못 쓰는 엔드포인트 scope: {sorted(orphan_endpoints)}"

    # market.* 는 market-api 소관이라 여기서 안 지킨다 - 그 사실을 명시한다
    unused = {s for s in declared - endpoint_scopes
              if not s.startswith(("market.", "case.", "research.evidence.chunk"))}
    assert not unused, f"research-api 가 지키지 않는 research scope: {sorted(unused)}"
    print(f"  실제 config 정합         OK (페르소나 {len(allow)}, "
          f"금지 {len(forbid)})")


def _check_enforce_default():
    """기본은 관측 모드지만 그 사실이 숨겨지면 안 된다."""
    old = os.environ.pop("TOOL_GATEWAY_ENFORCE", None)
    try:
        assert enforcing() is False, "기본이 강제면 기존 호출자가 전부 막힌다"
        os.environ["TOOL_GATEWAY_ENFORCE"] = "true"
        assert enforcing() is True
        os.environ["TOOL_GATEWAY_ENFORCE"] = "no"
        assert enforcing() is False
    finally:
        os.environ.pop("TOOL_GATEWAY_ENFORCE", None)
        if old is not None:
            os.environ["TOOL_GATEWAY_ENFORCE"] = old
    print("  강제 모드 기본값         OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"{GATEWAY_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_scope_mapping()
    _check_access_rules()
    _check_forbidden_wins()
    _check_real_config_covers_endpoints()
    _check_enforce_default()
    print("Tool Gateway 5개 영역 통과")
