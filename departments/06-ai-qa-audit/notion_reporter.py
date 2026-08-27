#!/usr/bin/env python3
"""QA본부 감사 결과를 Notion QA DB(NOTION_QA_DB)에 올리는 Reporter Node의 업로드 로직.

담당: 동규. 결정론적 QA 결과를 관리자용 한국어 요약으로 Notion QA DB에 기록한다.
페이지 제목·본문·관리자용 속성에는 내부 변수명을 노출하지 않는다. 기술 식별값은
LangSmith와 Kanban의 운영 추적 영역에서만 연결한다.

자격증명은 root .env가 아니라 ai-office/.dev.vars 에서 읽는다(03-risk/notion_reporter.py와 동일
근거 - .env.example 18-24행). Notion은 Projection일 뿐이다 - 이 모듈이 실패해도 QAState의
바인딩 판정은 절대 바뀌지 않는다. 모든 실패를 흡수하고 {"ok": False, ...}로만 기록한다.
"""

# The standalone reporter bootstraps the repository path before importing
# shared modules; those imports intentionally follow the path setup.
# ruff: noqa: E402
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from reporting import notion_rich_text_chunks

from departments.notion_markdown import markdown_to_notion_blocks
from orchestration.adapters.department_notion_projection import (
    build_qa_notion_properties,
)
from orchestration.adapters.notion_idempotency import NotionIdempotency
from orchestration.adapters.notion_http import NotionHttpError, request_json

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
    redis_url = os.getenv("NOTION_IDEMPOTENCY_REDIS_URL") or os.getenv("REDIS_URL")
    if redis_url:
        env.setdefault("NOTION_IDEMPOTENCY_REDIS_URL", redis_url)
    return env


def _post(path: str, body: dict, token: str) -> tuple[int, dict]:
    try:
        return 200, dict(
            request_json(
                "POST", path, token, body=body, version=_NOTION_VERSION
            )
        )
    except NotionHttpError as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc)}
        return exc.status or 599, detail


def _rich_text(s) -> dict:
    return {"rich_text": notion_rich_text_chunks(s)}


def _notion_title(artifact: dict, out: dict) -> str:
    """Return a readable, stable title without exposing a field name."""

    identifier = artifact.get("trace_id") or artifact.get("instrument_id")
    identifier = str(identifier or out.get("qa_decision_id") or "검토").strip()
    return f"QA 감사 결과 · 검토 번호 {identifier}"


def upload_case(
    artifact: dict,
    decision_time: str,
    out: dict,
    *,
    report_md: str = "",
    env: dict | None = None,
) -> dict:
    """out(run_qa_department 반환 형태)을 Notion QA DB에 1건 업로드한다. 절대 예외를 던지지 않는다."""
    env = env if env is not None else _load_dev_vars()
    token, db_id = env.get("NOTION_TOKEN"), env.get("NOTION_QA_DB")
    if not token or not db_id:
        return {"ok": False, "reason": "NOTION_TOKEN/NOTION_QA_DB 미설정 - 업로드 생략"}

    title = _notion_title(artifact, out)
    props = build_qa_notion_properties(
        {},
        title=title,
        verdict=out.get("verdict"),
        findings=out.get("findings") or (),
        checks=out.get("claim_checks") or (),
        claim_narrative=out.get("claim_narrative") or report_md,
        input_hash=out.get("input_hash"),
        calculation_version=out.get("calculation_version"),
        reason_codes=out.get("reason_codes") or (),
        escalate=out.get("escalate"),
        created_at=decision_time or datetime.now(timezone.utc).isoformat(),
        original_report=report_md or "QA 검토 결과",
    )

    try:
        payload = {"parent": {"database_id": db_id}, "properties": props}
        if report_md:
            payload["children"] = markdown_to_notion_blocks(report_md)
        idempotency = NotionIdempotency(env, namespace="qa-reporter")

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
    except Exception as e:  # noqa: BLE001 - Notion is a non-binding projection.
        return {"ok": False, "reason": f"업로드 예외: {e}"}
    return {"ok": True, "url": body.get("url")}


# ── 자체 점검 (네트워크 없음) ──────────────────────────────────────────────
def _check_missing_config_skips_without_network():
    def _boom(*a, **k):
        raise AssertionError("설정 없는데 네트워크 호출을 시도했다")

    orig = _post
    globals()["_post"] = _boom
    try:
        result = upload_case(
            {}, "x", {"qa_decision_id": "d1", "verdict": "PASS"}, env={}
        )
        assert result == {
            "ok": False,
            "reason": "NOTION_TOKEN/NOTION_QA_DB 미설정 - 업로드 생략",
        }
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
        out = {
            "qa_decision_id": "d1",
            "verdict": "PASS",
            "reason_codes": [],
            "claim_checks": [],
            "findings": [],
            "calculation_version": "v1",
            "input_hash": "h1",
            "claim_narrative": "n",
            "escalate": False,
        }
        result = upload_case(
            {"trace_id": "t1"},
            "2026-08-01",
            out,
            env={"NOTION_TOKEN": "tok", "NOTION_QA_DB": "db1"},
        )
        assert result == {"ok": True, "url": "https://notion.so/fake"}
        assert captured["body"]["parent"]["database_id"] == "db1"
        assert captured["body"]["properties"]["판정"]["select"]["name"] == "PASS"
        title = captured["body"]["properties"]["제목"]["title"][0]["text"]["content"]
        assert title.startswith("QA 감사 결과 · 검토 번호 ")
        assert "qa_decision_id" not in title
        properties = captured["body"]["properties"]
        assert "findings" in properties
        assert "claim_checks" in properties
        assert "claim_narrative" in properties
        assert properties["input_hash"]["rich_text"][0]["text"]["content"] == "h1"
        assert properties["calculation_version"]["rich_text"][0]["text"]["content"] == "v1"
        assert properties["escalate"]["checkbox"] is False
        assert "trade_case_id" not in properties
    finally:
        globals()["_post"] = orig
    print("  업로드 Payload 구성        OK")


if __name__ == "__main__":
    print("qa notion_reporter 자체 점검 (네트워크 없음)")
    _check_missing_config_skips_without_network()
    _check_payload_shape()
    print("notion_reporter 2개 영역 통과")
