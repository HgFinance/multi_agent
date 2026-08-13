#!/usr/bin/env python3
"""LS OpenAPI TR 카탈로그 빌더 - 구조화 문서 43장 -> 검색 가능한 색인 하나.

소유: 재일 (리서치본부)
근거: 재일 결정 2026-08-13 "TR 목록을 정제해서 사용자 질문에 답할 수 있게".

▶ 왜 카탈로그인가 (도구 수백 개를 다 등록하지 않는 이유)
  TR 은 수백 개인데 MCP 도구로 전부 등록하면 에이전트의 도구 선택이 무너진다
  (문헌: FinToolBench 는 도구 760개에서 '검색 후 선택' FATR 을 베이스라인으로
  제안, FinAgentBench 는 '어느 문서 -> 어느 조항' 2단계가 표준).
  그래서 실행 도구는 용례별 큐레이션 소수만 두고, 나머지는 이 카탈로그를
  `ls_tr_catalog` 검색 도구로 노출한다 - 에이전트가 "수급" 을 검색하면
  t1717 이 나오고, 큐레이션 도구가 있으면 그 이름을, 없으면 "미구현" 을
  정직하게 돌려준다(미구현 목록이 곧 다음 큐레이션 백로그다).

▶ 입력: docs/06-integrations/ls-openapi/**/*.md (2026-07-29 구조화 수집분)
▶ 출력: departments/01-research/config/ls_tr_catalog.json
  (config 디렉터리는 mcp 컨테이너들에 ro 마운트라 재빌드 없이 갱신된다)

실행: python api/ls_tr_catalog_build.py          # 빌드 + 자체 점검
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parents[1]
DOCS = _BASE.parents[1] / "docs" / "06-integrations" / "ls-openapi"
OUT = _BASE / "config" / "ls_tr_catalog.json"

_H1 = re.compile(r"^# \[(?P<cat>[^\]]+)\]\s*(?P<title>.+)$")
_KV = re.compile(r"^\|\s*(?P<k>[^|]+?)\s*\|\s*`?(?P<v>[^|`]+?)`?\s*\|$")
# TR 목록 행: | 1 | [이름](#tr-1) | `t1717` | 1 | 있음 | 있음 |
_TRROW = re.compile(
    r"^\|\s*\d+\s*\|\s*\[(?P<name>[^\]]+)\]\(#(?P<anchor>tr-\d+)\)\s*\|"
    r"\s*`(?P<code>[^`]+)`\s*\|\s*(?P<rate>[^|]+?)\s*\|")
# 필드 행: | 001.002 | 1 | `shcode` | 종목코드 | String | 6 | Y |
_FIELD = re.compile(
    r"^\|\s*[\d.]+\s*\|\s*(?P<depth>\d+)\s*\|\s*`(?P<name>[^`]+)`\s*\|"
    r"\s*(?P<desc>[^|]+?)\s*\|\s*(?P<type>[^|]+?)\s*\|\s*(?P<len>[^|]+?)\s*\|"
    r"\s*(?P<req>[^|]+?)\s*\|")


def parse_doc(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    category = title = protocol = api_path = ""
    trs: dict[str, dict] = {}          # anchor -> tr entry
    rate_by_anchor: dict[str, str] = {}

    for ln in lines:
        m = _H1.match(ln)
        if m and not category:
            category, title = m.group("cat"), m.group("title").strip()
        m = _KV.match(ln)
        if m:
            k, v = m.group("k").strip(), m.group("v").strip()
            if k == "프로토콜":
                protocol = v
            elif k == "접속 경로":
                api_path = v
        m = _TRROW.match(ln)
        if m:
            rate = m.group("rate").strip()
            trs[m.group("anchor")] = {
                "tr_code": m.group("code"), "name": m.group("name").strip(),
                "category": category, "doc_title": title,
                "protocol": protocol, "path": api_path,
                "rate_per_sec": (None if rate in ("-", "") else rate),
                "doc": str(path.relative_to(DOCS.parents[1])).replace("\\", "/"),
                "in_params": [], "out_fields": 0,
            }
            rate_by_anchor[m.group("anchor")] = rate

    # 필드 표: <a id="tr-k"> 구간 안에서 InBlock/OutBlock 을 가른다
    current = None      # 지금 어느 TR 구간인가 (anchor)
    block = None        # 지금 어느 블록의 depth-1 행을 읽는 중인가 ("in"|"out"|None)
    for ln in lines:
        am = re.search(r'<a id="(tr-\d+)"></a>', ln)
        if am:
            current, block = am.group(1), None
            continue
        if current is None or current not in trs:
            continue
        fm = _FIELD.match(ln)
        if not fm:
            continue
        if fm.group("depth") == "0":
            nm = fm.group("name")
            block = ("in" if "InBlock" in nm else
                     "out" if "OutBlock" in nm else None)
            continue
        if block == "in":
            trs[current]["in_params"].append({
                "name": fm.group("name"), "desc": fm.group("desc"),
                "type": fm.group("type"), "len": fm.group("len"),
                "required": fm.group("req") == "Y"})
        elif block == "out":
            trs[current]["out_fields"] += 1
    return list(trs.values())


def build() -> dict:
    entries: list[dict] = []
    files = sorted(p for p in DOCS.rglob("*.md") if p.name != "README.md")
    for p in files:
        entries.extend(parse_doc(p))
    # 같은 TR 이 여러 문서에 나오면 첫 것(카테고리 문서 순) 유지
    seen: dict[str, dict] = {}
    for e in entries:
        seen.setdefault(e["tr_code"], e)
    return {"built_from": str(DOCS.relative_to(DOCS.parents[1])).replace("\\", "/"),
            "doc_count": len(files), "tr_count": len(seen),
            "trs": sorted(seen.values(), key=lambda e: e["tr_code"])}


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cat = build()
    OUT.write_text(json.dumps(cat, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"문서 {cat['doc_count']}장 -> TR {cat['tr_count']}개 -> {OUT.name}")

    # 자체 점검: 수급 TR 이 제대로 파싱됐나 (이번 작업의 존재 이유)
    by = {e["tr_code"]: e for e in cat["trs"]}
    t = by.get("t1717")
    assert t, "t1717 이 카탈로그에 없다"
    assert t["path"] == "/stock/frgr-itt", t["path"]
    names = [p["name"] for p in t["in_params"]]
    assert "shcode" in names and "gubun" in names, names
    assert t["out_fields"] > 20, t["out_fields"]
    print(f"  t1717 검증 OK: path={t['path']} in={names} out={t['out_fields']}필드")
    t2 = by.get("t1602")
    assert t2 and t2["rate_per_sec"] == "1", "t1602 초당 제한 파싱 실패"
    print(f"  t1602 검증 OK: rate={t2['rate_per_sec']}/s in={len(t2['in_params'])}개")
    # 프로토콜 분포 - REST 만 실행 후보다
    from collections import Counter
    print("  프로토콜 분포:", dict(Counter(e["protocol"] for e in cat["trs"])))
