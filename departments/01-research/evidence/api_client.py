#!/usr/bin/env python3
"""부서 조회면 호출 공용 지점 - 페르소나를 밝히고 부른다.

소유: 재일 (리서치본부)
근거: 재일님 지시 2026-08-02 "이어서 진행" - Tool Gateway 이행 1단계.
      api/tool_gateway.py 가 `X-Agent-Persona` 로 권한을 판정한다.

▶ 왜 공용 지점인가
  분석가마다 urlopen 을 직접 부르면 헤더를 빠뜨리는 곳이 생기고, 그 하나
  때문에 강제 모드를 못 켠다. 호출을 한 군데로 모아 **페르소나 없이 부르는
  길 자체를 없앤다.**

▶ 규율
  - 페르소나는 인자로 **반드시** 받는다. 기본값을 두면 아무나 그 기본값으로
    부르게 되고 허용목록이 무의미해진다.
  - 게이트웨이가 403 을 주면 그대로 올린다. 조용히 빈 결과로 바꾸면
    "권한이 없다"와 "데이터가 없다"가 섞인다 - 후자로 오해하면 분석가가
    근거 없음으로 결론 내린다.

실행: python evidence/api_client.py   # 자체 점검 (네트워크 없음)
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

CLIENT_VERSION = "research-api-client-v1"
PERSONA_HEADER = "X-Agent-Persona"


class ApiForbidden(RuntimeError):
    """게이트웨이가 막았다. 데이터 없음과 구분되어야 한다."""


def build_headers(persona: str) -> dict[str, str]:
    """페르소나 헤더. 빈 값은 거부한다 - 익명 호출 경로를 만들지 않는다."""
    p = (persona or "").strip()
    if not p:
        raise ValueError("persona 는 필수다 - 익명 호출은 허용목록을 무의미하게 한다")
    return {PERSONA_HEADER: p}


def get_json(url: str, *, persona: str, timeout: int = 25, opener=None):
    """조회면 GET. 403 은 ApiForbidden 으로 올린다(빈 결과로 바꾸지 않는다)."""
    req = urllib.request.Request(url, headers=build_headers(persona))
    try:
        with (opener or urllib.request.urlopen)(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 403:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001, S110 - intentional fallback boundary
                pass
            raise ApiForbidden(
                f"{persona} 가 {url} 을 호출할 권한이 없다(게이트웨이 403). {body}")
        raise


def getter_for(persona: str):
    """기존 `get=` 인자 자리에 그대로 넣는 호출자를 만든다.

    분석가들이 `get(url)` 시그니처를 쓰고 있어서, 페르소나를 미리 묶은 함수를
    돌려주면 호출부를 최소로 고치고도 헤더가 붙는다.
    """
    build_headers(persona)          # 잘못된 페르소나는 여기서 즉시 걸린다

    def _get(url: str, timeout: int = 25):
        return get_json(url, persona=persona, timeout=timeout)

    return _get


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크 없음
# ---------------------------------------------------------------------------

def _check_persona_required():
    assert build_headers("geopolitical-analyst") == {
        PERSONA_HEADER: "geopolitical-analyst"}
    assert build_headers("  x  ") == {PERSONA_HEADER: "x"}
    for bad in ("", "   ", None):
        try:
            build_headers(bad)
            raise AssertionError(f"익명 호출이 통과했다: {bad!r}")
        except ValueError:
            pass
    try:
        getter_for("")
        raise AssertionError("빈 페르소나로 getter 가 만들어졌다")
    except ValueError:
        pass
    print("  페르소나 필수            OK")


def _check_forbidden_not_empty():
    """403 을 빈 결과로 바꾸지 않는다 - 권한 없음과 데이터 없음은 다르다."""
    class _Resp:
        def __init__(self, code):
            self.code = code

    def opener_403(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

    try:
        get_json("http://x/evidence/news", persona="technical-analyst",
                 opener=opener_403)
        raise AssertionError("403 이 조용히 통과했다")
    except ApiForbidden as e:
        assert "권한이 없다" in str(e)

    # 403 이 아닌 오류는 그대로 올라간다(삼키지 않는다)
    def opener_500(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {}, None)

    try:
        get_json("http://x/evidence/news", persona="technical-analyst",
                 opener=opener_500)
        raise AssertionError("500 이 삼켜졌다")
    except urllib.error.HTTPError as e:
        assert e.code == 500
    print("  403 구분·오류 전파       OK")


def _check_header_matches_gateway():
    """헤더 이름이 게이트웨이와 어긋나면 아무도 인증되지 않는다."""
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "api"))
    from tool_gateway import PERSONA_HEADER as GW_HEADER

    assert PERSONA_HEADER.lower() == GW_HEADER.lower(), \
        f"헤더 불일치: client={PERSONA_HEADER} gateway={GW_HEADER}"
    print("  게이트웨이 헤더 일치     OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"{CLIENT_VERSION} 자체 점검 (네트워크 없음)")
    _check_persona_required()
    _check_forbidden_not_empty()
    _check_header_matches_gateway()
    print("조회면 클라이언트 3개 영역 통과")
