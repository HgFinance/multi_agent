#!/usr/bin/env python3
"""CEO Office Daily Report를 Notion CEO DB(NOTION_CEO_DB)에 올리는 Reporter Node의 업로드 로직.

담당: 영주. departments/03-risk/notion_reporter.py, departments/06-ai-qa-audit/notion_reporter.py와
같은 패턴 — 속성명·Select 값은 코드 출력(run_ceo_department 반환 형태)을 그대로 쓴다.

docs/06-integrations/notion/NOTION_DEPARTMENT_DB_DESIGN.md는 00번을 "기존 김비서 일일 브리핑
DB(NOTION_BRIEFING_DB) 재사용"으로 적어뒀지만, 그 DB는 별도 TS Worker(ai-office/worker/report.ts)가
쓰는 자유 형식 브리핑 스키마다. 이 파이프라인이 만드는 건 report_runs 계약을 따르는 구조화 Report라
같은 DB에 섞지 않고 NOTION_CEO_DB를 새로 쓴다 — 스키마가 다른 두 용도를 한 DB에 밀어넣지 않는다.

자격증명은 root .env가 아니라 ai-office/.dev.vars에서 읽는다(Risk/QA와 동일 근거 -
.env.example이 이미 "Notion/Discord 연동 값은 이 파일이 안 다룬다"고 명시).

Notion은 Projection일 뿐이다 - 이 모듈이 실패해도(미설정, 네트워크 오류 등) report_runs의 status는
절대 바뀌지 않는다. 모든 실패를 흡수하고 {"ok": False, ...}로만 기록한다.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from reporting import notion_rich_text_chunks

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
    return {"rich_text": notion_rich_text_chunks(s)}


def upload_report(out: dict, *, report_md: str = "", env: dict | None = None) -> dict:
    """out(run_ceo_department 반환 형태)을 Notion CEO DB에 1건 업로드한다. 절대 예외를 던지지 않는다."""
    env = env if env is not None else _load_dev_vars()
    token, db_id = env.get("NOTION_TOKEN"), env.get("NOTION_CEO_DB")
    if not token or not db_id:
        return {"ok": False, "reason": "NOTION_TOKEN/NOTION_CEO_DB 미설정 - 업로드 생략"}

    props = {
        "제목": {"title": [{"text": {"content": f"{out.get('report_type', 'DAILY')} {out.get('as_of')}"}}]},
        "fund_id": _rich_text(out.get("fund_id")),
        "판정": {"select": {"name": out["status"]}},
        "missing_required": {"multi_select": [{"name": m} for m in out.get("missing_required", [])]},
        "source_snapshot_ids": _rich_text(json.dumps(out.get("source_snapshot_ids", []), ensure_ascii=False)),
        "template_version": _rich_text(out.get("template_version")),
        "content_hash": _rich_text(out.get("content_hash")),
        "escalate": {"checkbox": bool(out.get("escalate", False))},
        "서술": _rich_text(out.get("narrative")),
        "생성 시각": {"date": {"start": datetime.now(timezone.utc).isoformat()}},
    }

    try:
        from departments.notion_markdown import markdown_to_notion_blocks

        payload = {"parent": {"database_id": db_id}, "properties": props}
        if report_md:
            payload["children"] = markdown_to_notion_blocks(report_md)
        status, body = _post("pages", payload, token)
    except Exception as e:  # noqa: BLE001 - Notion은 비바인딩 Projection이라 오류를 흡수한다.
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
        result = upload_report({"status": "QUEUED", "as_of": "2026-08-01"}, env={})
        assert result == {"ok": False, "reason": "NOTION_TOKEN/NOTION_CEO_DB 미설정 - 업로드 생략"}
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
        out = {"fund_id": "f1", "report_type": "DAILY", "as_of": "2026-08-01",
               "status": "QUEUED", "missing_required": [], "source_snapshot_ids": ["s1", "s2"],
               "template_version": "v1", "content_hash": "h1", "escalate": False,
               "narrative": "n"}
        result = upload_report(
            out,
            report_md="# CEO Summary\n\n- worker context",
            env={"NOTION_TOKEN": "tok", "NOTION_CEO_DB": "db1"},
        )
        assert result == {"ok": True, "url": "https://notion.so/fake"}
        assert captured["body"]["parent"]["database_id"] == "db1"
        assert captured["body"]["properties"]["판정"]["select"]["name"] == "QUEUED"
        assert "원본 리포트" not in captured["body"]["properties"]
        assert captured["body"]["children"]
    finally:
        globals()["_post"] = orig
    print("  업로드 Payload 구성        OK")


if __name__ == "__main__":
    print("ceo-office notion_reporter 자체 점검 (네트워크 없음)")
    _check_missing_config_skips_without_network()
    _check_payload_shape()
    print("notion_reporter 2개 영역 통과")
