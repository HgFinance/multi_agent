#!/usr/bin/env python3
"""회계본부 마감 결과를 Notion Accounting DB(NOTION_ACCOUNTING_DB)에 올리는 업로드 로직.

담당: 도현 (회계/포트폴리오본부)
형식 근거: departments/03-risk/notion_reporter.py, departments/02-trading/notion_reporter.py
설계 근거: docs/06-integrations/notion/NOTION_DEPARTMENT_DB_DESIGN.md 3절 공통 속성 +
      5절 `05 · 회계·포트폴리오본부` — 제목 후보 journal_id, severity 4개 전체,
      match_method 5개 전체, "Ledger 원장은 append-only라 Notion에서 절대 수정 유도 UI를
      두지 않는다".

**이 DB 행은 공식 회계 수치가 아니다.**
`판정` Select 는 NAV 상태(`PRELIMINARY` / `BLOCKED`)이고 `is_official` 은 항상 false 다.
config.yaml fund-accounting-agent 페르소나: "Preliminary and Official NAV are different
things — never present the former as the latter, and Official NAV requires independent
approval you do not hold." Official NAV 는 이 경로로 절대 나가지 않는다.

**금액은 전부 Rich text(문자열)로 올린다. Number 속성을 쓰지 않는다.**
Notion Number 는 IEEE754 double 이라 Decimal 이 조용히 깨진다 - 원장 금액이 Notion 에서
반올림돼 보이면 그 화면을 근거로 누가 수치를 인용한다. `daily_report.to_dict()` 와
`ui_read_model` 이 같은 이유로 문자열 계약을 쓴다(팀 가이드 decimal 규칙).
Number 를 쓰는 속성은 **건수뿐**이다(Break 개수 - 정수라 안전).

설계 문서 5절이 제목 후보로 journal_id 를 들었지만, 마감 팩은 분개 한 건이 아니라 하루치
마감이다. 그래서 제목은 `<회계일> · <close_id>` 이고 journal_id 는 넣지 않는다 - 없는 값을
있는 것처럼 채우지 않는다. 분개 단위 행이 필요해지면 별도 DB 를 만든다.

자격증명 Source: root .env 가 정본(2026-08-03 도현님 확정). ai-office/.dev.vars 는
Cloudflare Worker 가 아직 NOTION_TOKEN/NOTION_BRIEFING_DB 를 쓰고 있어 후순위로 읽는다.
  ponytail: .dev.vars 쪽 Notion 키가 정리되면 _ENV_FILES 를 root .env 단일 경로로 줄인다.

**속성 생성 후 절차 (이 순서를 지킨다):**
  1. Notion Accounting DB 에 REQUIRED_PROPERTIES 의 속성을 만든다. 이름·타입이 정확히
     같아야 한다 - Notion 은 이름으로 매칭하고 없으면 400 을 준다.
  2. Select 옵션을 미리 만든다. Notion 은 없는 옵션을 자동 생성하지 않는다:
       판정          PRELIMINARY / BLOCKED
       severity      none / low / medium / high / material   (Severity 4개 + none)
  3. Integration 을 해당 DB 에 연결한다(페이지 우상단 ... > Connections).
     토큰만 있고 연결이 없으면 404 다 - 미설정과 구분이 안 되니 빼먹지 않는다.
  4. `python departments/05-accounting-portfolio/scripts.py --run` 으로 1건 올리고
     notion_upload.ok 가 True 인지 확인한다.
  5. 실패하면 status 를 그대로 읽는다 - 400 속성 불일치, 404 연결 누락, 401 토큰.
     ok:False 를 성공으로 넘기지 않는다.
  6. 통과하면 이 문단을 "속성 생성 완료(날짜)"로 갱신하고 REQUIRED_PROPERTIES 를 실제 DB 와
     다시 대조한다. 속성을 추가·개명할 때마다 이 목록을 같이 고친다.

Notion 은 Projection 일 뿐이다 - 이 모듈이 실패해도 마감의 nav_status/escalate 는 절대
바뀌지 않는다. 모든 실패를 흡수하고 {"ok": False, ...} 로만 기록한다.

점검: python departments/05-accounting-portfolio/notion_reporter.py    # 네트워크 없음
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_BASE = Path(__file__).resolve().parent
_REPO_ROOT = _BASE.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from departments.notion_markdown import markdown_to_notion_blocks
from orchestration.adapters.notion_idempotency import NotionIdempotency

# 앞에 오는 파일이 이긴다. root .env 가 정본(모듈 상단 참고).
_ENV_FILES = (_REPO_ROOT / ".env", _REPO_ROOT / "ai-office" / ".dev.vars")
_NOTION_VERSION = "2022-06-28"
_CHUNK = 1900   # Notion rich_text 블록 상한 2000자. 여유를 둔다.

# Notion Accounting DB 에 있어야 하는 속성. upload_close 의 props 와 1:1 이며 이름·타입이
# 다르면 Notion 이 400 을 준다. 속성을 늘리거나 개명하면 여기도 같이 고친다.
REQUIRED_PROPERTIES: dict[str, str] = {
    # 공통 (설계 문서 3절)
    "제목": "Title",
    "trade_case_id": "Rich text",
    "판정": "Select (PRELIMINARY / BLOCKED)",
    "escalate": "Checkbox",
    "서술": "Rich text",
    "calculation_version": "Rich text",
    "input_hash": "Rich text",
    "생성 시각": "Date",
    # 회계본부 고유
    "회계일": "Date",
    "공식 여부": "Checkbox",          # 항상 false — 화면에서 바로 보이게 둔다
    "NAV": "Rich text",               # 금액은 문자열. Number 금지(Decimal 손상)
    "현금": "Rich text",
    "증권평가액": "Rich text",
    "미실현손익": "Rich text",
    "NAV 항등식": "Rich text",
    "Break 건수": "Number",
    "Material Break": "Number",
    "severity": "Select (none / low / medium / high / material)",  # 팀원이 이미 만든 속성 재사용
    "대사 결과": "Rich text",
    "Projection 대조": "Rich text",
    "차단 사유": "Rich text",
    "fund_id": "Rich text",
    "book_id": "Rich text",
    "trace_id": "Rich text",
    "model": "Rich text",
    "prompt": "Rich text",
}

# Severity 는 reconciliation.py 의 StrEnum 값이다. 코드에 없는 옵션을 Notion 에 만들지 않는다.
_SEVERITY_ORDER = ("none", "low", "medium", "high", "material")


def _load_env() -> dict:
    """root .env(정본) 를 먼저 읽고, 없는 키만 ai-office/.dev.vars 에서 채운다."""
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
    redis_url = os.getenv("NOTION_IDEMPOTENCY_REDIS_URL") or os.getenv("REDIS_URL")
    if redis_url:
        env.setdefault("NOTION_IDEMPOTENCY_REDIS_URL", redis_url)
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


def _report_path(close_id) -> Path:
    """scripts.py --run 이 저장하는 MD 경로 규칙. Notion 본문 첫 줄에 원본 위치를 남긴다."""
    return _BASE / "reports" / f"accounting_close_{close_id}.md"


def top_severity(out: dict) -> str:
    """Break 중 가장 높은 심각도. 없으면 none. reconciliation.Severity 값만 쓴다."""
    seen = {str(b.get("severity", "")).lower() for b in (out.get("breaks") or [])}
    for level in reversed(_SEVERITY_ORDER):
        if level in seen:
            return level
    return "none"


def blocking_reasons(out: dict) -> list[str]:
    """NAV 를 막은 결정론 사유. LLM 서술이 아니라 코드가 센 것이다."""
    reasons: list[str] = []
    proj = out.get("projection_check") or {}
    if out.get("material_break_count"):
        reasons.append(f"Material Break {out['material_break_count']}건")
    if out.get("snapshot") is None:
        reasons.append("평가 불가 (Mark 없음/낡음)")
    elif (out.get("nav_identity") or {}).get("ok") is False:
        reasons.append("NAV 항등식 불일치")
    if proj.get("trial_balance_balanced") is False:
        reasons.append("차대 불균형")
    if proj.get("rebuild_deterministic") is False:
        reasons.append("rebuild 재현성 실패")
    if proj.get("drift"):
        reasons.append(f"projection 불일치 {len(proj['drift'])}건")
    if out.get("fallbacks"):
        reasons.append(f"파이프라인 실패 {len(out['fallbacks'])}건")
    return reasons


def recon_summary_text(out: dict) -> str:
    """대사 결과 한 줄. 미수행을 '통과'로 적지 않는다."""
    recon = out.get("recon") or {}
    if not recon or "_none" in recon:
        return "미수행 (브로커 명세서 없음)"
    return " / ".join(f"{k}:{v.get('result')}" for k, v in recon.items())


def projection_text(out: dict) -> str:
    proj = out.get("projection_check") or {}
    if not proj:
        return "미수행"
    parts = [
        f"재현성 {'통과' if proj.get('rebuild_deterministic') else '실패'}",
        f"차대 {'균형' if proj.get('trial_balance_balanced') else '불균형'}",
    ]
    parts.append(f"저장본 대조 {'수행' if proj.get('stored_projection_compared') else '미수행'}")
    parts.append(f"불일치 {len(proj.get('drift') or [])}건")
    return " / ".join(parts)


def upload_close(out: dict, *, report_md: str = "", env: dict | None = None) -> dict:
    """out(run_accounting_close 반환 형태)을 Notion Accounting DB 에 1건 올린다.
    **절대 예외를 던지지 않는다** - 실패는 전부 {"ok": False, ...} 로만 나온다."""
    env = env if env is not None else _load_env()
    token, db_id = env.get("NOTION_TOKEN"), env.get("NOTION_ACCOUNTING_DB")
    if not token or not db_id:
        return {"ok": False, "reason": "NOTION_TOKEN/NOTION_ACCOUNTING_DB 미설정 - 업로드 생략"}

    snap = out.get("snapshot") or {}
    ident = out.get("nav_identity") or {}
    versions = out.get("agent_versions") or {}
    acc_date = out.get("accounting_date")
    reasons = blocking_reasons(out)

    props = {
        "제목": {"title": [{"text": {"content":
                 f"{acc_date or '?'} · {out.get('close_id') or 'close'}"}}]},
        "trade_case_id": _rich_text(out.get("trade_case_id")),
        # 투자 판정이 아니라 NAV 상태다. OFFICIAL 은 이 경로로 나가지 않는다.
        "판정": {"select": {"name": out.get("nav_status") or "BLOCKED"}},
        "escalate": {"checkbox": bool(out.get("escalate", True))},
        "서술": _rich_text(out.get("narrative")),
        "calculation_version": _rich_text(out.get("pipeline_version")),
        "input_hash": _rich_text(out.get("input_hash")),
        "생성 시각": {"date": {"start": datetime.now(timezone.utc).isoformat()}},
        "회계일": {"date": {"start": acc_date}} if acc_date else {"date": None},
        # is_official 은 코드가 항상 False 로 낸다. 화면에서도 체크가 꺼져 보여야 한다.
        "공식 여부": {"checkbox": bool(out.get("is_official", False))},
        # 금액은 문자열. Notion Number 는 double 이라 Decimal 이 깨진다.
        "NAV": _rich_text(snap.get("nav")),
        "현금": _rich_text(snap.get("cash")),
        "증권평가액": _rich_text(snap.get("securities_value")),
        "미실현손익": _rich_text(snap.get("unrealized_pnl")),
        "NAV 항등식": _rich_text(
            "미검산" if not ident.get("checked")
            else ("일치" if ident.get("ok") else f"불일치 (차이 {ident.get('difference')})")),
        "Break 건수": {"number": len(out.get("breaks") or [])},
        "Material Break": {"number": int(out.get("material_break_count") or 0)},
        "severity": {"select": {"name": top_severity(out)}},
        "대사 결과": _rich_text(recon_summary_text(out)),
        "Projection 대조": _rich_text(projection_text(out)),
        "차단 사유": _rich_text(", ".join(reasons) or "없음"),
        "fund_id": _rich_text(out.get("fund_id")),
        "book_id": _rich_text(out.get("book_id")),
        "trace_id": _rich_text(out.get("trace_id")),
        "model": _rich_text(versions.get("model")),
        "prompt": _rich_text(f"{versions.get('supervisor_prompt')} / "
                             f"{versions.get('reconciliation_prompt')}"),
    }

    try:
        payload = {"parent": {"database_id": db_id}, "properties": props}
        if report_md:
            # 리포트를 rich_text 속성에 밀어넣지 않고 페이지 본문 블록으로 렌더링한다
            # (공용 departments/notion_markdown.py, risk/qa/trading 과 같은 패턴).
            intro = (f"**결정론적 MD 리포트 저장:** `{_report_path(out.get('close_id'))}`\n\n"
                     f"{report_md}")
            payload["children"] = markdown_to_notion_blocks(intro)
        title = f"{acc_date or '?'} · {out.get('close_id') or 'close'}"
        idempotency = NotionIdempotency(env, namespace="accounting-reporter")

        def lookup():
            query_status, query_body = _post(
                f"databases/{db_id}/query",
                {
                    "filter": {
                        "property": "제목",
                        "title": {"equals": title},
                    },
                    "page_size": 1,
                },
                token,
            )
            if query_status != 200:
                raise RuntimeError(f"notion_query_failed:{query_status}")
            return query_body.get("results") or []

        def create():
            create_status, create_body = _post("pages", payload, token)
            if create_status != 200:
                raise RuntimeError(f"notion_create_failed:{create_status}:{create_body}")
            return create_body

        result = idempotency.execute(
            db_id,
            title,
            lookup=lookup,
            create=create,
        )
        if result.duplicate:
            return {"ok": True, "duplicate": True}
        body = result.page if isinstance(result.page, dict) else {}
    except Exception as e:   # 네트워크 오류 등 - 절대 파이프라인을 죽이지 않는다  # noqa: BLE001 - intentional fallback boundary
        return {"ok": False, "reason": f"업로드 예외: {type(e).__name__}"}
    return {"ok": True, "url": body.get("url")}


# ── 자체 점검 (네트워크 없음) ──────────────────────────────────────────────
_OUT = {
    "pipeline_version": "accounting-close-pipeline-v1", "close_id": "cls-abc",
    "accounting_date": "2026-08-03", "input_hash": "h1", "trace_id": "tr-1",
    "fund_id": "f1", "book_id": "b1",
    "recon": {"position": {"result": "matched"}, "cash": {"result": "matched"}},
    "breaks": [], "material_break_count": 0,
    "projection_check": {"rebuild_deterministic": True, "trial_balance_balanced": True,
                         "stored_projection_compared": True, "drift": []},
    "snapshot": {"nav": "99998950", "cash": "92998950", "securities_value": "7000000",
                 "unrealized_pnl": "0"},
    "nav_identity": {"checked": True, "ok": True, "difference": "0"},
    "nav_status": "PRELIMINARY", "narrative": "정상 마감", "escalate": False,
    "fallbacks": [], "is_official": False,
    "agent_versions": {"model": "m", "supervisor_prompt": "sp", "reconciliation_prompt": "rp"},
}


def _check_missing_config_skips_without_network():
    def _boom(*a, **k):
        raise AssertionError("설정이 없는데 네트워크 호출을 시도했다")

    orig, globals()["_post"] = _post, _boom
    try:
        assert upload_close(_OUT, env={}) == {
            "ok": False, "reason": "NOTION_TOKEN/NOTION_ACCOUNTING_DB 미설정 - 업로드 생략"}
        assert upload_close(_OUT, env={"NOTION_TOKEN": "t"})["ok"] is False
    finally:
        globals()["_post"] = orig
    print("  미설정 시 네트워크 미호출  OK")


def _capture_props(out, **env_extra):
    captured = {}

    def _fake(path, body, token):
        captured["body"] = body
        return 200, {"url": "https://notion.so/fake"}

    orig, globals()["_post"] = _post, _fake
    try:
        result = upload_close(out, report_md="# 마감\n\n| a | b |\n|---|---|\n| 1 | 2 |",
                              env={"NOTION_TOKEN": "tok", "NOTION_ACCOUNTING_DB": "db1",
                                   **env_extra})
    finally:
        globals()["_post"] = orig
    return result, captured["body"]


def _check_payload_shape():
    result, body = _capture_props(_OUT)
    assert result == {"ok": True, "url": "https://notion.so/fake"}
    props = body["properties"]
    assert body["parent"]["database_id"] == "db1"
    assert props["판정"]["select"]["name"] == "PRELIMINARY"
    assert props["공식 여부"]["checkbox"] is False
    assert props["회계일"]["date"]["start"] == "2026-08-03"
    # 리포트는 속성이 아니라 페이지 본문 블록으로 간다
    assert "원본 리포트" not in props
    blocks = body["children"]
    assert any(b["type"] == "table" for b in blocks), [b["type"] for b in blocks]
    print("  업로드 Payload 구성        OK")


def _check_amounts_are_strings():
    """금액을 Notion Number 로 올리면 Decimal 이 double 로 깨진다. 문자열이어야 한다."""
    _, body = _capture_props(_OUT)
    props = body["properties"]
    for name in ("NAV", "현금", "증권평가액", "미실현손익"):
        assert "rich_text" in props[name], f"{name} 이 Number 로 나간다 - Decimal 손상"
        assert props[name]["rich_text"][0]["text"]["content"] == _OUT["snapshot"][
            {"NAV": "nav", "현금": "cash", "증권평가액": "securities_value",
             "미실현손익": "unrealized_pnl"}[name]]
    # Number 를 쓰는 건 건수뿐이다(정수라 안전)
    numbers = {k for k, v in props.items() if "number" in v}
    assert numbers == {"Break 건수", "Material Break"}, numbers
    print("  금액 문자열 계약           OK")


def _check_official_never_leaks():
    """LLM 이 뭐라 썼든 공식 NAV 로 나가지 않는다."""
    lying = {**_OUT, "narrative": "Official NAV 확정 완료"}
    _, body = _capture_props(lying)
    props = body["properties"]
    assert props["공식 여부"]["checkbox"] is False
    assert props["판정"]["select"]["name"] in ("PRELIMINARY", "BLOCKED")
    # is_official 이 True 로 와도 Notion 은 코드가 준 값을 그대로 쓴다 - 위조 감지용
    forced = {**_OUT, "is_official": True}
    _, body2 = _capture_props(forced)
    assert body2["properties"]["공식 여부"]["checkbox"] is True, \
        "상류가 is_official=True 를 보내면 Notion 에도 그대로 보여야 한다(은폐 금지)"
    print("  공식 NAV 비노출            OK")


def _check_blocked_close():
    blocked = {**_OUT, "nav_status": "BLOCKED", "snapshot": None, "escalate": True,
               "material_break_count": 2,
               "breaks": [{"severity": "material"}, {"severity": "low"}],
               "nav_identity": {"checked": False},
               "recon": {"_none": {"result": "not_reconciled"}},
               "projection_check": {"rebuild_deterministic": True,
                                    "trial_balance_balanced": False,
                                    "stored_projection_compared": False, "drift": []}}
    _, body = _capture_props(blocked)
    props = body["properties"]
    assert props["판정"]["select"]["name"] == "BLOCKED"
    assert props["escalate"]["checkbox"] is True
    assert props["severity"]["select"]["name"] == "material"
    assert props["NAV"]["rich_text"][0]["text"]["content"] == ""
    reasons = props["차단 사유"]["rich_text"][0]["text"]["content"]
    assert "Material Break 2건" in reasons and "차대 불균형" in reasons, reasons
    # 미수행 대사를 통과로 적지 않는다
    assert "미수행" in props["대사 결과"]["rich_text"][0]["text"]["content"]
    print("  차단 마감 표기             OK")


def _check_severity_and_reasons():
    assert top_severity({"breaks": []}) == "none"
    assert top_severity({"breaks": [{"severity": "low"}, {"severity": "high"}]}) == "high"
    assert top_severity({"breaks": [{"severity": "material"}, {"severity": "high"}]}) == "material"
    assert blocking_reasons(_OUT) == []
    r = blocking_reasons({**_OUT, "snapshot": None})
    assert r == ["평가 불가 (Mark 없음/낡음)"], r
    print("  심각도·차단사유 집계       OK")


def _check_required_properties_match_payload():
    _, body = _capture_props(_OUT)
    sent, listed = set(body["properties"]), set(REQUIRED_PROPERTIES)
    assert sent == listed, f"목록에만 있음={listed - sent}, payload 에만 있음={sent - listed}"
    for name in ("판정", "severity"):
        assert REQUIRED_PROPERTIES[name].startswith("Select ("), REQUIRED_PROPERTIES[name]
    print("  속성 목록 ↔ payload 일치   OK")


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
        result = upload_close(_OUT, env={"NOTION_TOKEN": "t", "NOTION_ACCOUNTING_DB": "d"})
        assert result["ok"] is False and "RuntimeError" in result["reason"]
    finally:
        globals()["_post"] = orig
    print("  예외 흡수 (Projection)     OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("accounting notion_reporter 자체 점검 (네트워크 없음)")
    _check_missing_config_skips_without_network()
    _check_payload_shape()
    _check_amounts_are_strings()
    _check_official_never_leaks()
    _check_blocked_close()
    _check_severity_and_reasons()
    _check_required_properties_match_payload()
    _check_long_report_chunked()
    _check_upload_never_raises()
    print("notion_reporter 9개 영역 통과")
