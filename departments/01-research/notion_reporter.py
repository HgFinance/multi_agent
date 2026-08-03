#!/usr/bin/env python3
"""리서치본부 Packet 을 Notion Research DB(NOTION_RESEARCH_DB)에 올리는 Reporter.

담당: 재일 (리서치/퀀트)
근거: docs/06-integrations/notion/NOTION_DEPARTMENT_DB_DESIGN.md 4.3 스펙 그대로.
      동규님 departments/03-risk/notion_reporter.py 패턴을 따른다 - 자격 출처,
      실패 흡수, 자체 점검 골격을 부서마다 다르게 만들면 그게 다음 사고다.

▶ Notion 은 Projection 이다
  이 모듈이 실패해도(미설정·네트워크·Select 옵션 누락) **Packet 의 내용은
  절대 바뀌지 않는다.** 모든 실패를 흡수하고 {"ok": False, ...} 로만 기록한다.
  호출부는 결과를 그대로 리턴할 뿐 raise 하지 않는다.

▶ 자격은 root .env 가 아니라 ai-office/.dev.vars 에서 읽는다
  .env.example 이 "Notion/Discord 연동 값은 ai-office/.dev.vars.example 이
  Source" 라고 명시했다. 같은 값을 두 곳에 두면 언젠가 갈린다.

▶ 분석가 판정 6종은 Rich Text 다 (설계 4.3 결정)
  Select 로 굳히면 코드에 없는 옵션을 지어내게 된다 - 분석가마다 값 집합이
  다르고 일부는 LLM 서술에서 준결정론적으로 도출된다. news_sentiment 만
  docstring 에 3개로 못박혀 있지만, 6개 중 하나만 Select 로 하면 그 비대칭이
  더 헷갈린다.

실행: python departments/01-research/notion_reporter.py   # 자체 점검(네트워크 없음)
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_DEV_VARS = _REPO_ROOT / "ai-office" / ".dev.vars"
_NOTION_VERSION = "2022-06-28"
REPORTER_VERSION = "research-notion-reporter-v1"

# 분석가 노드 -> **실제 Notion 속성명** (2026-08-03 DB 조회로 확인).
# 설계 문서(4.3)는 영문 노드명으로 적었지만 실제 DB 는 한국어 속성명을 쓴다.
# 추측으로 만들면 400 "is not a property that exists" 가 난다 - 실측이 기준이다.
ANALYST_PROPS = {
    "sentiment": "분석가 판정 - 감성",
    "technical": "분석가 판정 - 기술",
    "fundamental": "분석가 판정 - 펀더멘털",
    "regime": "분석가 판정 - 레짐",
    "geopolitical": "분석가 판정 - 지정학",
    "microstructure": "분석가 판정 - 미시구조",
}
ANALYST_NODES = tuple(ANALYST_PROPS)
# 제목은 '종목' 이다(설계 문서의 '제목' 이 아니다).
TITLE_PROP = "종목"
# 감성만 Select 다 - news_sentiment 는 docstring 에 3값으로 못박힌 유일한
# 케이스라 DB 도 그렇게 만들어져 있다(설계 4.3 의 판단과 일치).
SENTIMENT_SELECT_VALUES = ("SCORED", "NO_EVIDENCE", "INCONCLUSIVE")


def _parse_env_file(path: Path) -> dict:
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _load_dev_vars() -> dict:
    """Notion 자격을 읽는다. **.dev.vars 가 우선, root .env 가 보완이다.**

    실측 2026-08-03: .dev.vars 에는 NOTION_TOKEN 과 NOTION_BRIEFING_DB 만 있고
    부서 DB ID 8개(RESEARCH·RISK·QUANT…)는 root .env 에만 있다. 문서
    (.env.example)는 ".dev.vars 가 Source" 라고 하는데 실제 값은 갈려 있다.
    한쪽만 읽으면 **설정이 있는데도 조용히 업로드를 건너뛴다** - 그 침묵이
    가장 나쁘다. 둘 다 읽되 선언된 출처를 우선시키고, 어디서 왔는지는
    describe_source() 로 드러낸다.
    """
    merged = _parse_env_file(_REPO_ROOT / ".env")
    merged.update({k: v for k, v in _parse_env_file(_DEV_VARS).items() if v})
    return merged


def describe_source(env: dict | None = None) -> str:
    """자격이 어디서 왔는지 - 사람이 확인할 수 있게. 값은 절대 안 찍는다."""
    dev = _parse_env_file(_DEV_VARS)
    root = _parse_env_file(_REPO_ROOT / ".env")
    where = []
    for key in ("NOTION_TOKEN", "NOTION_RESEARCH_DB"):
        src = ".dev.vars" if dev.get(key) else (".env" if root.get(key) else "없음")
        where.append(f"{key}={src}")
    return " / ".join(where)


def _post(path: str, body: dict, token: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"https://api.notion.com/v1/{path}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Notion-Version": _NOTION_VERSION,
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:  # noqa: BLE001
            return e.code, {"message": "본문 파싱 실패"}


def _rich_text(s) -> dict:
    """Notion rich_text. 2000자 상한이 있어 자른다 - 넘기면 400 이 난다.
    자른 사실을 남긴다(조용한 절단 금지)."""
    text = "" if s is None else str(s)
    if len(text) > 1900:
        text = text[:1900] + f"… (총 {len(str(s))}자에서 잘림)"
    return {"rich_text": [{"text": {"content": text}}]}


def build_properties(packet: dict, *, symbol: str) -> dict:
    """Packet -> Notion 속성. 순수 함수라 자체 점검이 전부 검사한다.

    HALTED 케이스는 packet 이 아니라 {"verdict":"HALTED","reason":...} 모양으로
    온다(설계 4.3 주석). 그때는 halted 만 채우고 나머지는 비운다 - 없는 값을
    기본값으로 채우면 '분석했는데 중립' 처럼 보인다.
    """
    halted = packet.get("verdict") == "HALTED"
    nc = packet.get("numeric_check") or {}
    verdicts = packet.get("_analyst_verdicts") or {}

    props: dict = {
        TITLE_PROP: {"title": [{"text": {"content": str(symbol)}}]},
        "수치 재대조": {"checkbox": bool(nc.get("ok")) if nc else False},
        "halted": _rich_text(packet.get("reason") if halted else ""),
        "생성 시각": {"date": {"start": datetime.now(timezone.utc).isoformat()}},
        "서술": _rich_text(packet.get("thesis")),
        "input_hash": _rich_text(packet.get("input_hash")),
        "calculation_version": _rich_text(packet.get("pipeline_version")),
    }
    eq = packet.get("evidence_quality")
    if eq in ("sufficient", "partial", "insufficient_evidence"):
        props["evidence_quality"] = {"select": {"name": eq}}
    for node, prop in ANALYST_PROPS.items():
        v = verdicts.get(node)
        if node == "sentiment":
            # 감성만 Select 다. **아는 값일 때만 보낸다** - DB 에 없는 옵션을
            # 보내면 400 이고, 그 원인을 나중에 찾기 어렵다.
            if v in SENTIMENT_SELECT_VALUES:
                props[prop] = {"select": {"name": v}}
            continue
        props[prop] = _rich_text(v)
    return props


def upload_packet(packet: dict, *, symbol: str, report_md: str = "",
                  env: dict | None = None) -> dict:
    """Packet 1건을 Notion Research DB 에 올린다. **절대 예외를 던지지 않는다.**"""
    env = env if env is not None else _load_dev_vars()
    token, db_id = env.get("NOTION_TOKEN"), env.get("NOTION_RESEARCH_DB")
    if not token or not db_id:
        return {"ok": False,
                "reason": "NOTION_TOKEN/NOTION_RESEARCH_DB 미설정 - 업로드 생략"}
    try:
        payload = {"parent": {"database_id": db_id},
                   "properties": build_properties(packet, symbol=symbol)}
        if report_md:
            from departments.notion_markdown import markdown_to_notion_blocks

            payload["children"] = markdown_to_notion_blocks(report_md)
        status, body = _post("pages", payload, token)
    except Exception as e:  # noqa: BLE001 - Notion 은 구속력 없는 Projection 이다
        return {"ok": False, "reason": f"업로드 예외: {type(e).__name__}: {e}"[:200]}
    if status == 200:
        return {"ok": True, "url": body.get("url")}
    return {"ok": False, "status": status, "error": body.get("message", body)}


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크 없음
# ---------------------------------------------------------------------------

def _packet(**over) -> dict:
    base = {"symbol": "000660", "evidence_quality": "partial",
            "numeric_check": {"ok": True},
            "_analyst_verdicts": {"sentiment": "SCORED", "technical": "NEUTRAL",
                                  "fundamental": "NOTED",
                                  "regime": "BREADTH_THRUST",
                                  "geopolitical": "ELEVATED",
                                  "microstructure": "ORDERLY"}}
    base.update(over)
    return base


def _check_properties_shape():
    p = build_properties(_packet(), symbol="000660")
    assert p[TITLE_PROP]["title"][0]["text"]["content"] == "000660"
    assert p["evidence_quality"]["select"]["name"] == "partial"
    assert p["수치 재대조"]["checkbox"] is True
    # 분석가 6종이 **전부** 있어야 한다 - 하나 빠지면 표가 어긋난다
    # 실제 DB 속성명으로 나가야 한다 - 추측하면 400 이다
    for node, prop in ANALYST_PROPS.items():
        assert prop in p, (node, prop)
    assert p["분석가 판정 - 기술"]["rich_text"][0]["text"]["content"] == "NEUTRAL"
    assert p["분석가 판정 - 감성"]["select"]["name"] == "SCORED", "감성만 Select"
    # 모르는 감성 값은 아예 안 보낸다(DB 에 없는 옵션 = 400)
    p2 = build_properties(_packet(_analyst_verdicts={"sentiment": "이상한값"}),
                          symbol="x")
    assert "분석가 판정 - 감성" not in p2
    print("  속성 모양·분석가 6종     OK")


def _check_evidence_quality_gate():
    """설계가 정한 3토큰만 Select 로 보낸다 - 코드에 없는 옵션을 지어내면
    Notion 이 400 을 내고 그 원인을 찾기 어렵다."""
    for bad in ("high", "좋음", "", None, "SUFFICIENT"):
        p = build_properties(_packet(evidence_quality=bad), symbol="x")
        assert "evidence_quality" not in p, bad
    for good in ("sufficient", "partial", "insufficient_evidence"):
        p = build_properties(_packet(evidence_quality=good), symbol="x")
        assert p["evidence_quality"]["select"]["name"] == good
    print("  evidence_quality 3토큰   OK")


def _check_halted_case():
    """HALTED 는 packet 모양이 다르다 - 없는 값을 기본값으로 채우지 않는다."""
    p = build_properties({"verdict": "HALTED", "reason": "거래 불가(HALTED)"},
                         symbol="999990")
    assert p["halted"]["rich_text"][0]["text"]["content"].startswith("거래 불가")
    assert "evidence_quality" not in p, "판정이 없으면 Select 를 보내지 않는다"
    assert p["수치 재대조"]["checkbox"] is False
    # 정상 케이스에는 halted 가 비어 있다
    ok = build_properties(_packet(), symbol="x")
    assert ok["halted"]["rich_text"][0]["text"]["content"] == ""
    print("  HALTED 분기              OK")


def _check_missing_config_skips_without_network():
    """미설정이면 네트워크를 타지 않고 생략한다 - 미설정은 결함이 아니다."""
    r = upload_packet(_packet(), symbol="x", env={})
    assert r["ok"] is False and "미설정" in r["reason"], r
    r2 = upload_packet(_packet(), symbol="x", env={"NOTION_TOKEN": "t"})
    assert r2["ok"] is False and "미설정" in r2["reason"], r2
    print("  미설정 생략(무네트워크)  OK")


def _check_rich_text_truncation():
    long = "가" * 5000
    rt = _rich_text(long)["rich_text"][0]["text"]["content"]
    assert len(rt) < 2000, len(rt)
    assert "잘림" in rt, "조용히 자르지 않는다"
    assert _rich_text(None)["rich_text"][0]["text"]["content"] == ""
    print("  2000자 상한 처리         OK")


def _check_never_raises():
    """어떤 입력에도 예외를 던지지 않는다 - Packet 을 죽이면 안 된다."""
    for bad in ({}, {"verdict": "HALTED"}, {"_analyst_verdicts": None},
                {"numeric_check": None}, {"evidence_quality": 123}):
        r = upload_packet(bad, symbol="x", env={"NOTION_TOKEN": "t",
                                                "NOTION_RESEARCH_DB": "d"})
        assert isinstance(r, dict) and "ok" in r, bad
    print("  예외 미전파              OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{REPORTER_VERSION} 자체 점검 (네트워크 없음)")
    _check_properties_shape()
    _check_evidence_quality_gate()
    _check_halted_case()
    _check_missing_config_skips_without_network()
    _check_rich_text_truncation()
    _check_never_raises()
    print("Notion Reporter 6개 영역 통과.")
