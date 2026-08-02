#!/usr/bin/env python3
"""게이트웨이 강제 전환 준비도 검사 - 코드가 실제로 쓰는 호출을 전수 대조.

소유: 재일 (리서치본부)
근거: 2026-08-02 실측. research-api 는 TOOL_GATEWAY_ENFORCE=false(관찰)로 돌고
      있었고, 그 상태에서는 권한이 없어도 200 이 나간다. 강제로 올리면 그
      순간 어떤 호출이 403 이 되는지를 **켜보기 전에** 알아야 한다.

▶ 왜 로그를 기다리지 않는가
  관찰 로그는 '그동안 실제로 불린 것'만 보여준다. 장중에만 도는 경로는
  휴장일 로그에 안 나오므로, 로그가 깨끗하다는 것이 안전하다는 뜻이 아니다.
  대신 **소스에서 호출 지점을 전부 뽑아** 정적으로 대조한다.

▶ 무엇을 뽑는가
  evidence/api_client.py 의 get_json(url, persona=...) 호출 지점.
  이것이 research-api 로 나가는 유일한 정문이다(직접 urlopen 하는 곳은
  Ollama·Supabase·외부 벤더라 게이트웨이 대상이 아니다).

실행
  python scripts/check_gateway_coverage.py          # 대조표 + 판정
  python scripts/check_gateway_coverage.py --strict # 위반 있으면 exit 1
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

CHECKER_VERSION = "research-gateway-coverage-v1"
_BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BASE / "api"))

# 게이트웨이가 관장하지 않는 대상 - research-api 가 아닌 곳으로 나가는 URL
_NOT_GATEWAY = ("11434", "ollama", "supabase", "MARKET_API", "market_api",
                "/regime/", "/breadth", "/bars", "/snapshot", "/dq/")


def endpoint_of(url_expr: str) -> str | None:
    """f-string URL 식 -> 게이트웨이가 보는 경로. 못 뽑으면 None.

    `f"{base}/evidence/news?symbol={s}"` 처럼 앞이 변수라 경로만 남긴다.
    """
    if any(x in url_expr for x in _NOT_GATEWAY):
        return None
    m = re.search(r"/(?:evidence|macro|universe)/[a-z_/]+", url_expr)
    return m.group(0).rstrip("/") if m else None


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """모듈 최상단의 문자열 상수 - PERSONA = "..." 같은 것을 되살린다."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = node.value.value
    return out


def personas_in(tree: ast.Module, consts: dict[str, str]) -> tuple[set[str], int]:
    """이 파일이 get_json 에 넘기는 페르소나들과, 못 알아본 호출 수.

    대부분의 분석가는 `_http_get(url)` 얇은 래퍼를 두고 URL 은 다른 함수에서
    만든다 - 그래서 호출 지점 하나만 봐서는 경로를 알 수 없다. 페르소나는
    파일 단위로 모으고 경로도 파일 단위로 모아 맞춘다.
    """
    found: set[str] = set()
    unresolved = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "get_json"):
            continue
        got = None
        for kw in node.keywords:
            if kw.arg != "persona":
                continue
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                got = kw.value.value
            elif isinstance(kw.value, ast.Name):
                got = consts.get(kw.value.id)
        if got:
            found.add(got)
        else:
            unresolved += 1
    return found, unresolved


def call_sites(root: Path) -> tuple[list[tuple[str, str, str]], list[str]]:
    """([(파일, 페르소나, 경로)], [사람이 확인할 파일 설명])."""
    out: list[tuple[str, str, str]] = []
    notes: list[str] = []
    for py in sorted(root.rglob("*.py")):
        # 서버 자신과 이 검사기는 호출자가 아니다 - 검사기의 예시 문자열이
        # '익명 호출 의심'으로 잡히면 판정이 영원히 시끄러워진다
        if "__pycache__" in py.parts or py.name == "api_client.py" \
                or py.resolve() == Path(__file__).resolve() \
                or py.parts[0] == "api":
            continue
        text = py.read_text(encoding="utf-8")
        if "get_json" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        rel = str(py.relative_to(root))
        consts = _module_constants(tree)
        personas, unresolved = personas_in(tree, consts)

        # 경로는 이 파일의 모든 문자열 리터럴에서 모은다 (f-string 조각 포함)
        paths = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                p = endpoint_of(node.value)
                if p:
                    paths.add(p)
        if not paths:
            continue
        if not personas:
            notes.append(f"{rel}: 게이트웨이 경로 {sorted(paths)} 를 쓰는데 "
                         f"페르소나를 못 찾았다 - 익명 호출 의심")
            continue
        if unresolved:
            notes.append(f"{rel}: persona 인자를 정적으로 못 읽은 호출 "
                         f"{unresolved}건 - 사람이 확인")
        for persona in sorted(personas):
            for path in sorted(paths):
                out.append((rel, persona, path))
    return out, notes


def main(strict: bool) -> int:
    from tool_gateway import GatewayConfigError, check_access, load_allowlist

    allow, forbid = load_allowlist()
    sites, notes = call_sites(_BASE)
    if not sites:
        print("호출 지점을 하나도 못 찾았다 - 추출 규칙이 깨졌다(검사 무효)")
        return 1

    violations = []
    print(f"{CHECKER_VERSION}: (페르소나, 경로) 조합 {len(sites)}건\n")
    for rel, persona, path in sites:
        try:
            ok, code, why = check_access(persona, path, allow, forbid)
        except GatewayConfigError as e:
            violations.append((rel, persona, path, f"scope 미매핑: {e}"))
            print(f"  ✗ {persona:<32}{path:<26}scope 미매핑   {rel}")
            continue
        if ok:
            print(f"    {persona:<32}{path:<26}통과           {rel}")
        else:
            violations.append((rel, persona, path, why))
            print(f"  ✗ {persona:<32}{path:<26}{code:<15}{rel}")

    print()
    if violations:
        print(f"강제 전환 시 403 이 될 호출 {len(violations)}건 - 켜기 전에 고친다:")
        for rel, persona, path, why in violations:
            print(f"  {rel}  {persona} -> {path}")
            print(f"      {why}")
    for n in notes:
        print(f"  확인 필요: {n}")
    if not violations and not notes:
        print("전 호출이 허용목록을 통과한다 - TOOL_GATEWAY_ENFORCE=true 로 올려도 "
              "지금 코드가 깨지지 않는다")
    return 1 if (strict and (violations or notes)) else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main("--strict" in sys.argv))
