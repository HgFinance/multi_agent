#!/usr/bin/env python3
"""트레이딩본부 Bull/Bear 토론 결과를 Notion Trading DB(NOTION_TRADING_DB)에 올리는 업로드 로직.

담당: 도현 (트레이딩본부)
형식 근거: departments/03-risk/notion_reporter.py 와 같은 Reporter 형식.
설계 근거: docs/06-integrations/notion/NOTION_DEPARTMENT_DB_DESIGN.md 3절 공통 속성.

**"판정" 속성 주의 - 이 DB 행은 투자 판정이 아니다.**
설계 문서 5절은 `02 · 트레이딩본부` DB 를 "스키마만"으로 두고 판정 Select 를 "OMS 자체 상태
(Order 상태 전이가 정의되면 확정)"로 남겨놨다. 토론 파이프라인은 Order 도 OMS 상태도 만들지
않으므로 그 자리를 비워두거나 지어내지 않고, **파이프라인 결과**만 넣는다:
    GROUNDED  근거 검증까지 통과한 토론
    ESCALATE  토론은 열렸으나 인용 날조·독립성 위반·STALE 등으로 사람이 봐야 함
    BLOCKED   토론 자체가 열리지 않음 (근거 부족, PIT 위반)
매수/매도·수량·주문유형은 어느 속성에도 없다. Order 상태 전이가 확정되면(TRD-01 이후) 그때
`OMS 상태` 속성을 따로 만든다 - 이 값을 재사용하지 않는다.

"서술" 속성도 LLM 문장이 아니라 결정론적 요약이다 - 회계·판정 수치를 LLM 문장에서 뽑지
않는다는 규칙(CLAUDE.md)을 토론 산출물에도 같게 적용한다. Bull/Bear 의 실제 서술은 본문
"원본 리포트"에 원문 그대로 들어간다.

자격증명 위치가 두 곳으로 갈라져 있다 (2026-08-03 확인):
  - 규약: .env.example 24행이 "Notion / Discord 연동 값은 ai-office/.dev.vars.example 이
    Source"라고 명시한다. 실제 그 파일에는 NOTION_TOKEN 과 NOTION_BRIEFING_DB 만 있다.
  - 실제: NOTION_TRADING_DB 를 포함한 부서별 DB ID 8개는 root .env 에 들어와 있다.
  둘 다 읽고 .dev.vars 를 우선한다. 한쪽으로 합치는 건 별도 정리 대상이다.
  ponytail: 자격증명 Source 가 하나로 정해지면 _load_env 의 두 번째 경로를 지운다.

Notion 은 Projection 일 뿐이다 - 이 모듈이 실패해도(미설정, 네트워크 오류, 속성 누락 등)
토론의 grounded/escalate 는 절대 바뀌지 않는다. 모든 실패를 흡수하고 {"ok": False, ...} 로만
기록한다 - scripts.py 의 notion_report 노드가 그대로 리턴할 뿐 raise 하지 않는다.

점검: python departments/02-trading/notion_reporter.py    # 네트워크 없음
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_BASE = Path(__file__).resolve().parent
_REPO_ROOT = _BASE.parents[1]
_ENV_FILES = (_REPO_ROOT / "ai-office" / ".dev.vars", _REPO_ROOT / ".env")
_NOTION_VERSION = "2022-06-28"
# Notion rich_text 한 블록 상한은 2000자다. 여유를 두고 자른다.
_CHUNK = 1900


def _load_env() -> dict:
    """.dev.vars(규약상 Source) 를 먼저 읽고, 없는 키만 root .env 에서 채운다."""
    env: dict[str, str] = {}
    for path in _ENV_FILES:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip())   # 먼저 읽은 파일이 이긴다
    return env


def _post(path: str, body: dict, token: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"https://api.notion.com/v1/{path}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Notion-Version": _NOTION_VERSION,
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _chunks(value: Any) -> list[dict]:
    text = "" if value is None else str(value)
    if not text:
        return [{"text": {"content": ""}}]
    return [{"text": {"content": text[i:i + _CHUNK]}} for i in range(0, len(text), _CHUNK)]


def _rich_text(value: Any) -> dict:
    return {"rich_text": _chunks(value)}


def _refs(values) -> str:
    return ", ".join(values or []) or "없음"


def pipeline_outcome(out: dict) -> str:
    """Notion "판정" Select 값. 투자 판정이 아니라 파이프라인 결과다(모듈 상단 주의 참고)."""
    if not out.get("debate_opened"):
        return "BLOCKED"
    return "GROUNDED" if out.get("grounded") else "ESCALATE"


def deterministic_summary(out: dict) -> str:
    """"서술" 속성 - LLM 문장이 아니라 코드가 센 숫자다."""
    c = out.get("contested") or {}
    pit = (out.get("pit") or {}).get("status", "UNKNOWN")
    if not out.get("debate_opened"):
        blocked = (out.get("fallbacks") or [{}])[0]
        return (f"토론이 열리지 않았다 — {blocked.get('error', 'unknown')}: "
                f"{blocked.get('error_message', '')} (Packet 신선도 {pit})")
    return (f"Claim {len(out.get('claims') or {})}건 중 양측이 다툰 것 "
            f"{len(c.get('contested_refs') or [])}건, Bull 단독 {len(c.get('bull_only_refs') or [])}건, "
            f"Bear 단독 {len(c.get('bear_only_refs') or [])}건, 아무도 다루지 않은 것 "
            f"{len(c.get('untouched_refs') or [])}건. Packet 신선도 {pit}. "
            f"이 행은 투자 판정이 아니다 — 방향·수량·주문유형을 담지 않는다.")


def upload_debate(out: dict, *, report_md: str = "", env: dict | None = None) -> dict:
    """out(run_bull_bear_debate 반환 형태)을 Notion Trading DB 에 1건 올린다.
    **절대 예외를 던지지 않는다** - 실패는 전부 {"ok": False, ...} 로만 나온다."""
    env = env if env is not None else _load_env()
    token, db_id = env.get("NOTION_TOKEN"), env.get("NOTION_TRADING_DB")
    if not token or not db_id:
        return {"ok": False, "reason": "NOTION_TOKEN/NOTION_TRADING_DB 미설정 - 업로드 생략"}

    citations = out.get("citations") or {}
    contested = out.get("contested") or {}
    versions = out.get("agent_versions") or {}
    props = {
        "제목": {"title": [{"text": {"content":
                 f"{out.get('symbol') or '?'} · {out.get('debate_id') or 'debate'}"}}]},
        "trade_case_id": _rich_text(out.get("trade_case_id")),
        # 투자 판정이 아니라 파이프라인 결과다 - pipeline_outcome() docstring 참고
        "판정": {"select": {"name": pipeline_outcome(out)}},
        "escalate": {"checkbox": bool(out.get("escalate", True))},
        "서술": _rich_text(deterministic_summary(out)),
        "calculation_version": _rich_text(out.get("pipeline_version")),
        "input_hash": _rich_text(out.get("input_hash")),
        "원본 리포트": _rich_text(report_md),
        "생성 시각": {"date": {"start": datetime.now(timezone.utc).isoformat()}},
        # 트레이딩본부 고유 - 감사가 토론 품질을 한 화면에서 보게 하는 값들
        "종목": _rich_text(out.get("symbol")),
        "Packet 신선도": {"select": {"name": (out.get("pit") or {}).get("status", "UNKNOWN")}},
        "쟁점 Claim": _rich_text(_refs(contested.get("contested_refs"))),
        "미검토 Claim": _rich_text(_refs(contested.get("untouched_refs"))),
        "날조 인용": _rich_text(_refs(citations.get("unknown_refs"))),
        "독립성 위반": {"number": int((out.get("independence") or {}).get("violations", 0))},
        "model": _rich_text(versions.get("model")),
        "prompt": _rich_text(f"{versions.get('bull_prompt')} / {versions.get('bear_prompt')}"),
        "trace_id": _rich_text(out.get("trace_id")),
    }

    try:
        status, body = _post("pages", {"parent": {"database_id": db_id}, "properties": props}, token)
    except Exception as e:   # 네트워크 오류 등 - 절대 파이프라인을 죽이지 않는다
        return {"ok": False, "reason": f"업로드 예외: {type(e).__name__}"}
    if status == 200:
        return {"ok": True, "url": body.get("url")}
    # 속성이 DB 에 아직 없으면 Notion 이 400 을 준다 - 설계 문서 5절대로 이 DB 는 "스키마만"
    # 상태라 최초 1회는 속성 생성이 필요하다. 조용히 성공한 척하지 않는다.
    return {"ok": False, "status": status, "error": body.get("message", body)}


# ── 자체 점검 (네트워크 없음) ──────────────────────────────────────────────
_OUT = {"pipeline_version": "trading-debate-pipeline-v1", "debate_id": "dbt-abc", "symbol": "005930",
        "trade_case_id": "tc-1", "input_hash": "h1", "trace_id": "tr-1",
        "pit": {"status": "FRESH"}, "debate_opened": True, "grounded": True, "escalate": False,
        "claims": {"fact:0": "a", "fact:1": "b"},
        "citations": {"unknown_refs": []},
        "contested": {"contested_refs": ["fact:0"], "bull_only_refs": ["fact:1"],
                      "bear_only_refs": [], "untouched_refs": []},
        "independence": {"violations": 0},
        "agent_versions": {"model": "m", "bull_prompt": "bp", "bear_prompt": "rp"},
        "fallbacks": []}


def _check_missing_config_skips_without_network():
    def _boom(*a, **k):
        raise AssertionError("설정이 없는데 네트워크 호출을 시도했다")

    orig, globals()["_post"] = _post, _boom
    try:
        assert upload_debate(_OUT, env={}) == {
            "ok": False, "reason": "NOTION_TOKEN/NOTION_TRADING_DB 미설정 - 업로드 생략"}
        # 토큰만 있고 DB ID 가 없어도 마찬가지다
        assert upload_debate(_OUT, env={"NOTION_TOKEN": "t"})["ok"] is False
    finally:
        globals()["_post"] = orig
    print("  미설정 시 네트워크 미호출  OK")


def _check_payload_shape():
    captured = {}

    def _fake(path, body, token):
        captured.update(path=path, body=body, token=token)
        return 200, {"url": "https://notion.so/fake"}

    orig, globals()["_post"] = _post, _fake
    try:
        result = upload_debate(_OUT, report_md="# 리포트",
                               env={"NOTION_TOKEN": "tok", "NOTION_TRADING_DB": "db1"})
        assert result == {"ok": True, "url": "https://notion.so/fake"}
        props = captured["body"]["properties"]
        assert captured["body"]["parent"]["database_id"] == "db1"
        assert props["판정"]["select"]["name"] == "GROUNDED"
        assert props["독립성 위반"]["number"] == 0
        assert props["Packet 신선도"]["select"]["name"] == "FRESH"
        assert props["원본 리포트"]["rich_text"][0]["text"]["content"] == "# 리포트"
        # 방향·수량·주문유형이 어떤 속성으로도 새 나가지 않는다 (권한 분리)
        blob = json.dumps(props, ensure_ascii=False)
        for forbidden in ("BUY", "SELL", "quantity", "order_type", "limit_price"):
            assert forbidden not in blob, f"토론 행에 {forbidden} 가 들어갔다"
    finally:
        globals()["_post"] = orig
    print("  업로드 Payload 구성        OK")


def _check_outcome_and_summary():
    assert pipeline_outcome(_OUT) == "GROUNDED"
    assert pipeline_outcome({**_OUT, "grounded": False}) == "ESCALATE"
    blocked = {**_OUT, "debate_opened": False, "grounded": False,
               "fallbacks": [{"error": "InsufficientEvidence", "error_message": "근거 부족"}]}
    assert pipeline_outcome(blocked) == "BLOCKED"
    assert "근거 부족" in deterministic_summary(blocked)
    assert "투자 판정이 아니다" in deterministic_summary(_OUT)
    print("  파이프라인 결과 매핑       OK")


def _check_long_report_chunked():
    chunks = _chunks("x" * (_CHUNK * 2 + 5))
    assert len(chunks) == 3 and len(chunks[0]["text"]["content"]) == _CHUNK
    assert _chunks(None) == [{"text": {"content": ""}}]
    print("  긴 리포트 분할             OK")


def _check_upload_never_raises():
    def _explode(*a, **k):
        raise RuntimeError("network down")

    orig, globals()["_post"] = _post, _explode
    try:
        result = upload_debate(_OUT, env={"NOTION_TOKEN": "t", "NOTION_TRADING_DB": "d"})
        assert result["ok"] is False and "RuntimeError" in result["reason"]
    finally:
        globals()["_post"] = orig
    print("  예외 흡수 (Projection)     OK")


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("trading notion_reporter 자체 점검 (네트워크 없음)")
    _check_missing_config_skips_without_network()
    _check_payload_shape()
    _check_outcome_and_summary()
    _check_long_report_chunked()
    _check_upload_never_raises()
    print("notion_reporter 5개 영역 통과")
