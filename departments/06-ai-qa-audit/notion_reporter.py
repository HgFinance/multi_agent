#!/usr/bin/env python3
"""QA본부 감사 결과를 Notion QA DB(NOTION_QA_DB)에 올리는 Reporter Node의 업로드 로직.

담당: 동규. docs/06-integrations/notion/NOTION_DEPARTMENT_DB_DESIGN.md 4.2 스펙 그대로 옮긴다 -
속성명·Select 값은 코드 출력(run_qa_department 반환 형태)을 그대로 쓴다, 새로 만들지 않는다.

자격증명은 root .env가 아니라 ai-office/.dev.vars 에서 읽는다(03-risk/notion_reporter.py와 동일
근거 - .env.example 18-24행). Notion은 Projection일 뿐이다 - 이 모듈이 실패해도 QAState의
바인딩 판정은 절대 바뀌지 않는다. 모든 실패를 흡수하고 {"ok": False, ...}로만 기록한다.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_DEV_VARS = Path(__file__).resolve().parent.parent.parent / "ai-office" / ".dev.vars"
_NOTION_VERSION = "2022-06-28"


def _load_dev_vars() -> dict:
    if not _DEV_VARS.exists():
        return {}
    env = {}
    for line in _DEV_VARS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
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


def _rich_text(s) -> dict:
    return {"rich_text": [{"text": {"content": (s or "")[:2000]}}]}


def upload_case(artifact: dict, decision_time: str, out: dict, *, report_md: str = "", env: dict | None = None) -> dict:
    """out(run_qa_department 반환 형태)을 Notion QA DB에 1건 업로드한다. 절대 예외를 던지지 않는다."""
    env = env if env is not None else _load_dev_vars()
    token, db_id = env.get("NOTION_TOKEN"), env.get("NOTION_QA_DB")
    if not token or not db_id:
        return {"ok": False, "reason": "NOTION_TOKEN/NOTION_QA_DB 미설정 - 업로드 생략"}

    props = {
        "제목": {"title": [{"text": {"content": f"qa_decision_id: {out['qa_decision_id']}"}}]},
        "trade_case_id": _rich_text(artifact.get("trace_id")),
        "판정": {"select": {"name": out["verdict"]}},
        "reason_codes": {"multi_select": [{"name": c} for c in out.get("reason_codes", [])]},
        "escalate": {"checkbox": bool(out.get("escalate", False))},
        "input_hash": _rich_text(out.get("input_hash")),
        "calculation_version": _rich_text(out.get("calculation_version")),
        "claim_checks": _rich_text(json.dumps(out.get("claim_checks", []), ensure_ascii=False)),
        "findings": _rich_text(json.dumps(out.get("findings", []), ensure_ascii=False)),
        "claim_narrative": _rich_text(out.get("claim_narrative")),
        "원본 리포트": _rich_text(report_md),
        "생성 시각": {"date": {"start": datetime.now(timezone.utc).isoformat()}},
    }

    try:
        status, body = _post("pages", {"parent": {"database_id": db_id}, "properties": props}, token)
    except Exception as e:  # 네트워크 오류 등 - 절대 파이프라인을 죽이지 않는다
        return {"ok": False, "reason": f"업로드 예외: {e}"}
    if status == 200:
        return {"ok": True, "url": body.get("url")}
    return {"ok": False, "status": status, "error": body.get("message", body)}


# ── 자체 점검 (네트워크 없음) ──────────────────────────────────────────────
def _check_missing_config_skips_without_network():
    def _boom(*a, **k):
        raise AssertionError("설정 없는데 네트워크 호출을 시도했다")
    orig = _post
    globals()["_post"] = _boom
    try:
        result = upload_case({}, "x", {"qa_decision_id": "d1", "verdict": "PASS"}, env={})
        assert result == {"ok": False, "reason": "NOTION_TOKEN/NOTION_QA_DB 미설정 - 업로드 생략"}
    finally:
        globals()["_post"] = orig
    print("  미설정 시 네트워크 미호출   OK")


def _check_payload_shape():
    captured = {}

    def _fake_post(path, body, token):
        captured["path"], captured["body"], captured["token"] = path, body, token
        return 200, {"url": "https://notion.so/fake"}

    orig = _post
    globals()["_post"] = _fake_post
    try:
        out = {"qa_decision_id": "d1", "verdict": "PASS", "reason_codes": [],
               "claim_checks": [], "findings": [], "calculation_version": "v1",
               "input_hash": "h1", "claim_narrative": "n", "escalate": False}
        result = upload_case({"trace_id": "t1"}, "2026-08-01", out,
                              env={"NOTION_TOKEN": "tok", "NOTION_QA_DB": "db1"})
        assert result == {"ok": True, "url": "https://notion.so/fake"}
        assert captured["body"]["parent"]["database_id"] == "db1"
        assert captured["body"]["properties"]["판정"]["select"]["name"] == "PASS"
    finally:
        globals()["_post"] = orig
    print("  업로드 Payload 구성        OK")


if __name__ == "__main__":
    print("qa notion_reporter 자체 점검 (네트워크 없음)")
    _check_missing_config_skips_without_network()
    _check_payload_shape()
    print("notion_reporter 2개 영역 통과")
