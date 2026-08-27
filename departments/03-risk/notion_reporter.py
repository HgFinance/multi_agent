#!/usr/bin/env python3
"""리스크본부 Case 결과를 Notion Risk DB(NOTION_RISK_DB)에 올리는 Reporter Node의 업로드 로직.

담당: 동규. docs/06-integrations/notion/NOTION_DEPARTMENT_DB_DESIGN.md 4.1 스펙 그대로 옮긴다 -
속성명·Select 값은 코드 출력(run_risk_department 반환 형태)을 그대로 쓴다, 새로 만들지 않는다.

자격증명은 root .env가 아니라 ai-office/.dev.vars 에서 읽는다 - .env.example 18-24행이 이미
"Notion/Discord 연동 값은 이 파일이 안 다룬다, ai-office/.dev.vars.example 이 Source다"라고
명시했으므로 같은 값을 root .env에 중복해서 두지 않는다.

Notion은 Projection일 뿐이다 - 이 모듈이 실패해도(미설정, 네트워크 오류, Select 옵션 누락 등)
RiskState의 바인딩 판정은 절대 바뀌지 않는다. 모든 실패를 흡수하고 {"ok": False, ...}로만
기록한다 - scripts.py의 notion_report 노드가 이 함수를 부른 뒤 그대로 리턴할 뿐 raise 하지 않는다.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from reporting import notion_rich_text_chunks

from departments.notion_markdown import markdown_to_notion_blocks
from departments.risk_notion_schema import RISK_PROPERTY_NAMES, risk_property_name
from orchestration.adapters.notion_http import NotionHttpError, request_json
from orchestration.adapters.notion_idempotency import NotionIdempotency
from orchestration.adapters.notion_schema_cache import BoundedNotionSchemaCache

_DEV_VARS = Path(__file__).resolve().parent.parent.parent / "ai-office" / ".dev.vars"
_NOTION_VERSION = "2022-06-28"
_SCHEMA_CACHE = BoundedNotionSchemaCache(ttl_seconds=60.0, max_entries=8)


class _SchemaReadError(RuntimeError):
    def __init__(self, status: int) -> None:
        super().__init__(f"Notion 스키마 조회 실패: HTTP {status}")
        self.status = status


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


def _get(path: str, token: str) -> tuple[int, dict]:
    try:
        return 200, dict(request_json("GET", path, token, version=_NOTION_VERSION))
    except NotionHttpError as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc)}
        return exc.status or 599, detail


def _rich_text(s) -> dict:
    return {"rich_text": notion_rich_text_chunks(s)}


def _report_path(risk_request_id: object) -> Path:
    return (
        Path(__file__).resolve().parent
        / "reports"
        / f"risk_case_report_{risk_request_id}.md"
    )


def _manager_title(
    order_intent: dict,
    context: dict,
    out: dict,
) -> str:
    """Build a stable, human-readable title without runtime identifiers.

    The request hash remains the idempotency key; it must not be exposed in the
    manager-facing title.  A point-in-time date keeps otherwise similar PAPER
    cases distinguishable while remaining stable across retries.
    """

    instrument = str(
        order_intent.get("instrument_name")
        or order_intent.get("symbol")
        or order_intent.get("ticker")
        or ""
    ).strip()
    side = {
        "BUY": "매수",
        "SELL": "매도",
    }.get(str(order_intent.get("side") or "").strip().upper(), "")
    quantity = order_intent.get("quantity")
    if instrument and side and quantity not in (None, ""):
        subject = f"{instrument} {side} {quantity}주"
    elif instrument and side:
        subject = f"{instrument} {side} 검토"
    elif instrument:
        subject = f"{instrument} 리스크 검토"
    else:
        subject = "리스크 사례"

    snapshot = context.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    as_of = (
        context.get("as_of")
        or snapshot.get("as_of")
        or out.get("created_at")
        or datetime.now(timezone.utc).date().isoformat()
    )
    date_text = str(as_of).strip()[:10]
    if len(date_text) == 10 and date_text[4] == "-" and date_text[7] == "-":
        return f"리스크 심사 · {subject} · {date_text}"
    return f"리스크 심사 · {subject}"


def upload_case(
    order_intent: dict,
    context: dict,
    out: dict,
    *,
    report_md: str = "",
    env: dict | None = None,
) -> dict:
    """out(run_risk_department 반환 형태)을 Notion Risk DB에 1건 업로드한다. 절대 예외를 던지지 않는다."""
    env = env if env is not None else _load_dev_vars()
    token, db_id = env.get("NOTION_TOKEN"), env.get("NOTION_RISK_DB")
    if not token or not db_id:
        return {
            "ok": False,
            "reason": "NOTION_TOKEN/NOTION_RISK_DB 미설정 - 업로드 생략",
        }

    schema_key = f"{db_id}:{sha256(str(token).encode()).hexdigest()[:16]}"

    def load_schema():
        schema_status, schema_body = _get(f"databases/{db_id}", token)
        if schema_status != 200:
            raise _SchemaReadError(schema_status)
        return schema_body.get("properties") or {}

    try:
        properties_schema, _ = _SCHEMA_CACHE.get(schema_key, load_schema)
    except _SchemaReadError as exc:
        return {
            "ok": False,
            "reason": f"Notion 스키마 조회 실패: HTTP {exc.status}",
        }
    except Exception as exc:  # noqa: BLE001 - Notion remains a non-binding projection.
        return {"ok": False, "reason": f"Notion 스키마 조회 예외: {exc}"}

    def prop(field: str) -> str:
        return risk_property_name(field, properties_schema)

    cp = out.get("counterparty") or {}
    title = _manager_title(order_intent, context, out)
    compliance_verdict = ((out.get("compliance") or {}).get("answer") or {}).get(
        "verdict"
    )
    approved_qty = out.get("approved_quantity")
    props = {
        prop("title"): {
            "title": [
                {
                    "text": {"content": title}
                }
            ]
        },
        prop("trade_case_id"): _rich_text(order_intent.get("trade_case_id")),
        prop("verdict"): {"select": {"name": out["verdict"]}},
        prop("trading_state"): {
            "select": {
                "name": out.get("trading_state")
                or context.get("trading_state")
                or "ENABLED"
            }
        },
        prop("approved_quantity"): {
            "number": float(approved_qty) if approved_qty is not None else None
        },
        prop("reason_codes"): {
            "multi_select": [{"name": c} for c in out.get("reason_codes", [])]
        },
        prop("escalate"): {"checkbox": bool(out.get("escalate", False))},
        prop("input_hash"): _rich_text(out.get("input_hash")),
        prop("calculation_version"): _rich_text(out.get("calculation_version")),
        prop("check_results"): _rich_text(
            json.dumps(out.get("check_results", []), ensure_ascii=False)
        ),
        prop("counterparty_narrative"): _rich_text(
            cp.get("counterparty_narrative")
        ),
        prop("narrative"): _rich_text(out.get("narrative")),
        prop("created_at"): {
            "date": {"start": datetime.now(timezone.utc).isoformat()}
        },
    }
    if compliance_verdict:
        props[prop("compliance_verdict")] = {
            "select": {"name": compliance_verdict}
        }

    try:
        payload = {"parent": {"database_id": db_id}, "properties": props}
        if report_md:
            report_path = _report_path(out["risk_request_id"])
            report_intro = (
                f"**결정론적 MD 리포트 저장:** `{report_path}`\n\n{report_md}"
            )
            payload["children"] = markdown_to_notion_blocks(report_intro)
        idempotency = NotionIdempotency(env, namespace="risk-reporter")

        def lookup():
            input_hash = str(out.get("input_hash") or "").strip()
            input_hash_property = prop("input_hash")
            if input_hash and input_hash_property in properties_schema:
                query_filter = {
                    "property": input_hash_property,
                    "rich_text": {"equals": input_hash},
                }
            else:
                query_filter = {
                    "property": prop("title"),
                    "title": {"equals": title},
                }
            query_status, query_body = _post(
                f"databases/{db_id}/query",
                {"filter": query_filter, "page_size": 1},
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
            {}, {}, {"risk_request_id": "r1", "verdict": "approve"}, env={}
        )
        assert result == {
            "ok": False,
            "reason": "NOTION_TOKEN/NOTION_RISK_DB 미설정 - 업로드 생략",
        }
    finally:
        globals()["_post"] = orig
    print("  미설정 시 네트워크 미호출   OK")


def _check_payload_shape():
    captured = {}

    def _fake_get(path, token):
        assert path == "databases/db1"
        assert token == "tok"
        return 200, {
            "properties": {
                names[0]: {}
                for names in RISK_PROPERTY_NAMES.values()
            }
        }

    def _fake_post(path, body, token):
        captured["path"], captured["body"], captured["token"] = path, body, token
        return 200, {"url": "https://notion.so/fake"}

    orig_get, orig_post = _get, _post
    globals()["_get"] = _fake_get
    globals()["_post"] = _fake_post
    try:
        out = {
            "risk_request_id": "r1",
            "verdict": "approve",
            "approved_quantity": "100",
            "reason_codes": [],
            "check_results": [],
            "calculation_version": "v1",
            "input_hash": "h1",
            "trading_state": "ENABLED",
            "escalate": False,
            "narrative": "n",
            "counterparty": None,
            "compliance": None,
        }
        result = upload_case(
            {"trade_case_id": "t1"},
            {},
            out,
            env={"NOTION_TOKEN": "tok", "NOTION_RISK_DB": "db1"},
        )
        assert result == {"ok": True, "url": "https://notion.so/fake"}
        assert captured["path"] == "pages"
        assert captured["body"]["parent"]["database_id"] == "db1"
        assert captured["body"]["properties"]["리스크 판정"]["select"]["name"] == "approve"
        assert captured["body"]["properties"]["승인 수량"]["number"] == 100.0
        title = captured["body"]["properties"]["제목"]["title"][0]["text"][
            "content"
        ]
        assert title == "리스크 심사 · 리스크 사례"
        assert "r1" not in title
        assert "법률·컴플라이언스 판정" not in captured["body"]["properties"]
    finally:
        globals()["_get"], globals()["_post"] = orig_get, orig_post
    print("  업로드 Payload 구성        OK")


if __name__ == "__main__":
    print("risk notion_reporter 자체 점검 (네트워크 없음)")
    _check_missing_config_skips_without_network()
    _check_payload_shape()
    print("notion_reporter 2개 영역 통과")
