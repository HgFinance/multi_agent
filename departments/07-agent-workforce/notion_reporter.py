#!/usr/bin/env python3
"""Workforce Scorecard를 Notion HR DB(NOTION_HR_DB)에 올리는 Reporter Node의 업로드 로직.

담당: 영주. departments/03-risk/notion_reporter.py, departments/06-ai-qa-audit/notion_reporter.py와
같은 패턴 - 속성명·Select 값은 코드 출력(run_hr_department 반환 형태)을 그대로 쓴다.

docs/06-integrations/notion/NOTION_DEPARTMENT_DB_DESIGN.md 5절은 07번 DB를 "채용 후보" 스키마로
스케치했지만, 채용 판정 엔진이 아직 없다(hiring_requests/candidates는 스키마만 있고 로직 없음).
이미 구현된 F27 Workforce Scorecard(scorecard/cost.py의 build_department_scorecard)를 대신
올린다 - NOTION_HR_DB 환경변수 이름은 그 문서가 이미 정해둔 것을 그대로 쓴다.

자격증명은 root .env가 아니라 ai-office/.dev.vars에서 읽는다(Risk/QA와 동일 근거).

Notion은 Projection일 뿐이다 - 이 모듈이 실패해도(미설정, 네트워크 오류 등) Scorecard 판정은
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


def upload_scorecard(out: dict, *, report_md: str = "", env: dict | None = None) -> dict:
    """out(run_hr_department 반환 형태)을 Notion HR DB에 1건 업로드한다. 절대 예외를 던지지 않는다."""
    env = env if env is not None else _load_dev_vars()
    token, db_id = env.get("NOTION_TOKEN"), env.get("NOTION_HR_DB")
    if not token or not db_id:
        return {"ok": False, "reason": "NOTION_TOKEN/NOTION_HR_DB 미설정 - 업로드 생략"}

    cost = out.get("cost") or {}
    capacity = out.get("capacity") or {}
    quality = out.get("quality") or {}
    window = out.get("window") or {}
    props = {
        "제목": {"title": [{"text": {"content": f"{out.get('department_code')} {window.get('window_start', '')}"}}]},
        "department_code": _rich_text(out.get("department_code")),
        # build_department_scorecard()에는 department 단위 budget 판정(Select)이 없다 - assess_budget은
        # Agent 단위라 여기 억지로 끼워 넣지 않는다(Notion 설계서 원칙: 코드에 없는 판정을 만들지 않는다).
        "case_count": {"number": cost.get("case_count")},
        "model_cost": _rich_text(cost.get("model_cost")),
        "input_tokens": {"number": cost.get("input_tokens")},
        "output_tokens": {"number": cost.get("output_tokens")},
        "arrivals": {"number": capacity.get("arrivals")},
        "finding_count": {"number": quality.get("finding_count")},
        "escalate": {"checkbox": bool(out.get("escalate", False))},
        "서술": _rich_text(out.get("narrative")),
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
        result = upload_scorecard({"department_code": "07-agent-workforce"}, env={})
        assert result == {"ok": False, "reason": "NOTION_TOKEN/NOTION_HR_DB 미설정 - 업로드 생략"}
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
        out = {"department_code": "07-agent-workforce",
               "window": {"window_start": "2026-07-24T00:00:00Z"},
               "cost": {"case_count": 5, "model_cost": "1.5", "input_tokens": 100, "output_tokens": 50},
               "capacity": {"arrivals": 10}, "quality": {"finding_count": 0}, "escalate": False,
               "narrative": "n"}
        result = upload_scorecard(out, env={"NOTION_TOKEN": "tok", "NOTION_HR_DB": "db1"})
        assert result == {"ok": True, "url": "https://notion.so/fake"}
        assert captured["body"]["parent"]["database_id"] == "db1"
        assert captured["body"]["properties"]["case_count"]["number"] == 5
    finally:
        globals()["_post"] = orig
    print("  업로드 Payload 구성        OK")


if __name__ == "__main__":
    print("agent-workforce notion_reporter 자체 점검 (네트워크 없음)")
    _check_missing_config_skips_without_network()
    _check_payload_shape()
    print("notion_reporter 2개 영역 통과")
