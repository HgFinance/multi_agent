#!/usr/bin/env python3
"""회계본부 마감·주간 보고를 Discord 웹훅으로 보낸다.

담당: 도현 (회계/포트폴리오본부)
형식 근거: departments/05-accounting-portfolio/notion_reporter.py (같은 Reporter 규약),
      ai-office/worker/report.ts sendDiscord (웹훅 payload 모양)

**여기 있는 수치는 전부 결정론 모듈이 확정한 값이다.** 이 파일은 `out`/`report` dict 에서
값을 꺼내 옮길 뿐 더하지도 반올림하지도 않는다. 부서장(Hermes) 서술은 `narrative`
필드 하나로만 들어가고 **수치 필드와 분리된 자리에 놓는다** - 서술과 수치가 한 문단에
섞이면 읽는 사람이 어느 쪽이 확정값인지 구분할 수 없고, 그게 팀 가이드 원칙 5
("회계 수치를 LLM 문장에서 추출해 확정하지 않는다")가 막으려는 상황이다.

**공식 수치가 아니다.** 모든 메시지 footer 에 Preliminary 와 Source of record 를 박는다.
`is_official` 은 어떤 경로로도 true 가 되지 않는다(daily_report.DailyReport 계약).

Discord 는 Projection 일 뿐이다 - 이 모듈이 실패해도(미설정, 네트워크 오류, 429 등)
마감의 nav_status 는 절대 바뀌지 않는다. 모든 실패를 흡수하고 {"ok": False, ...} 로만
기록한다. notion_reporter 와 같은 규약이다.

자격증명: `DISCORD_WEBHOOK_URL`. root .env 가 정본이고 ai-office/.dev.vars 를 후순위로
읽는다(notion_reporter._load_env 를 그대로 쓴다 - 같은 규칙을 두 벌 두면 언젠가 갈린다).

점검: python departments/05-accounting-portfolio/discord_reporter.py   # 네트워크 없음
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

_BASE = Path(__file__).resolve().parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

# 같은 부서의 Reporter 가 이미 같은 우선순위로 .env 를 읽는다. 규칙을 복제하지 않는다.
from notion_reporter import _load_env, blocking_reasons, recon_summary_text, top_severity

# Discord 상한. 넘기면 400 이라 자르는 쪽이 맞다 - 잘렸다는 사실은 말줄임표로 남긴다.
_FIELD_LIMIT = 1024
_TITLE_LIMIT = 256
_EMBED_FIELDS = 25

# 임베드 색. 판정별로 다르게 해서 채널에서 눈으로 걸러진다.
_COLOR_OK = 0x2ECC71       # PRELIMINARY - 막힌 데 없음
_COLOR_BLOCKED = 0xE74C3C  # BLOCKED - NAV 미확정
_COLOR_WEEKLY = 0x5865F2   # 주간 보고

_FOOTER = ("비공식(Preliminary) · Official NAV 는 독립 승인 필요 · "
           "Source of record: accounting.* (ledger / portfolio_snapshots)")


def _percent(value: Any, places: int = 2) -> str:
    """비율 -> 퍼센트 문자열. **금액에는 절대 쓰지 않는다.**

    Decimal 나눗셈은 28자리를 그대로 낸다(`0.006351264795869131840675371144`).
    금액은 한 자리도 줄이면 안 되지만 **비율은 파생 표시값**이고, 28자리를 그대로
    실으면 사람이 못 읽는다. 원본 비율은 `DailyReport.return_pct` 와 스냅샷에
    그대로 남아 있고 여기서 표시만 줄인다 - 계산에 쓰라고 주는 값이 아니다.
    """
    if value is None or value == "":
        return "-"
    try:
        ratio = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        # 해석 못 한 값을 0% 로 만들지 않는다. 원문을 그대로 보여준다.
        return str(value)
    quantum = Decimal(1).scaleb(-places)
    return f"{(ratio * 100).quantize(quantum, rounding=ROUND_HALF_UP)}%"


def _clip(value: Any, limit: int = _FIELD_LIMIT) -> str:
    """Discord 상한에 맞춰 자른다. 빈 값은 '-' 다 - 빈 문자열은 400 이다."""
    text = "-" if value is None or value == "" else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _post(url: str, body: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _send(embed: dict, env: dict | None) -> dict:
    """웹훅 1건 발송. **절대 예외를 던지지 않는다.**"""
    env = env if env is not None else _load_env()
    url = env.get("DISCORD_WEBHOOK_URL")
    if not url:
        return {"ok": False, "reason": "DISCORD_WEBHOOK_URL 미설정 - 발송 생략"}

    embed = {**embed, "footer": {"text": _FOOTER},
             "timestamp": datetime.now(timezone.utc).isoformat()}
    embed["fields"] = embed.get("fields", [])[:_EMBED_FIELDS]
    try:
        status, body = _post(url, {"embeds": [embed]})
    except Exception as e:  # noqa: BLE001 - Projection 이 마감을 죽이지 않는다
        return {"ok": False, "reason": f"발송 예외: {type(e).__name__}"}
    # 웹훅 성공은 204(본문 없음)다. 200 도 허용하되 그 외는 실패로 남긴다.
    if status in (200, 204):
        return {"ok": True, "status": status}
    return {"ok": False, "status": status, "error": body[:500]}


def _money_fields(figures: dict[str, Any]) -> list[dict]:
    """금액 필드는 문자열 그대로 싣는다. 여기서 포맷·반올림하지 않는다.

    Decimal 을 보기 좋게 만들려고 float 로 바꾸면 원장 금액이 조용히 깨진다
    (notion_reporter 가 Number 속성을 안 쓰는 것과 같은 이유).
    """
    return [{"name": name, "value": _clip(value), "inline": True}
            for name, value in figures.items()]


def send_close(out: dict, *, env: dict | None = None) -> dict:
    """일일 마감(run_accounting_close 반환 형태)을 채널에 올린다.

    `out["report"]` 가 있으면 손익까지 싣고, 없으면(스냅샷 부족 등) 그 사실이 그대로
    드러난다 - 없는 값을 0 으로 채우지 않는다.
    """
    snap = out.get("snapshot") or {}
    report = out.get("report") or {}
    pnl = report.get("pnl") or {}
    cost = report.get("cost") or {}
    nav = report.get("nav") or {}
    blocked = out.get("nav_status") != "PRELIMINARY"
    reasons = blocking_reasons(out)

    figures = {
        "NAV": nav.get("close") or snap.get("nav"),
        "현금": snap.get("cash"),
        "증권평가액": snap.get("securities_value"),
        "실현손익": pnl.get("realized"),
        "평가손익": pnl.get("unrealized"),
        "비용(수수료+세금)": cost.get("total"),
        "순손익": pnl.get("net"),
        # 0 이 아니면 원장·평가·자본유출입 중 어딘가 어긋난 것이다. 숨기지 않는다.
        "미설명 손익": pnl.get("unexplained"),
    }
    fields = _money_fields(figures)
    fields.append({"name": "판정", "value": _clip(out.get("nav_status")), "inline": True})
    fields.append({"name": "평가 품질",
                   "value": _clip(snap.get("quality_status")), "inline": True})
    fields.append({"name": "Break",
                   "value": _clip(f"{len(out.get('breaks') or [])}건 "
                                  f"(Material {out.get('material_break_count') or 0}, "
                                  f"최고 {top_severity(out)})"), "inline": True})
    fields.append({"name": "대사", "value": _clip(recon_summary_text(out)), "inline": False})
    if reasons:
        fields.append({"name": "차단 사유", "value": _clip(", ".join(reasons)), "inline": False})
    # 부서장 서술은 맨 아래 한 칸. 위의 수치와 같은 줄에 섞지 않는다.
    fields.append({"name": "부서장 서술 (비공식·서술 전용)",
                   "value": _clip(out.get("narrative")), "inline": False})

    return _send({
        "title": _clip(f"일일 마감 {out.get('accounting_date') or '?'} · "
                       f"{out.get('nav_status') or 'BLOCKED'}", _TITLE_LIMIT),
        "color": _COLOR_BLOCKED if blocked else _COLOR_OK,
        "fields": fields,
    }, env)


def send_weekly(report: dict, holdings: list[dict], *, narrative: str = "",
                env: dict | None = None) -> dict:
    """주간 포트폴리오 보고. `report` 는 `DailyReport.to_dict()` 의 구간 집계다.

    `holdings` 는 기말 스냅샷의 보유 종목이며 비중 순으로 정렬돼 들어온다 - 정렬과
    비중 계산은 호출자(결정론)가 하고 여기서 다시 계산하지 않는다.
    """
    pnl = report.get("pnl") or {}
    nav = report.get("nav") or {}
    cost = report.get("cost") or {}
    drawdown = report.get("drawdown") or {}

    fields = _money_fields({
        "기초 NAV": nav.get("open"),
        "기말 NAV": nav.get("close"),
        "NAV 변화": nav.get("change"),
        "실현손익": pnl.get("realized"),
        "평가손익": pnl.get("unrealized"),
        "비용": cost.get("total"),
        "순손익": pnl.get("net"),
        "최대낙폭": drawdown.get("max"),
        "미설명 손익": pnl.get("unexplained"),
    })
    # 비율 둘은 퍼센트로 줄여 싣는다. 금액과 같은 자리에서 만들면 언젠가 금액에도
    # 반올림이 붙으므로 목록을 따로 둔다.
    fields.extend({"name": name, "value": _percent(value), "inline": True}
                  for name, value in (("수익률", pnl.get("return_pct")),
                                      ("최대낙폭률", drawdown.get("max_pct"))))
    if holdings:
        lines = [f"{h.get('symbol') or h.get('instrument_id')} · "
                 f"{h.get('quantity')}주 · 평가 {h.get('market_value')} · "
                 f"비중 {_percent(h.get('weight'))}" for h in holdings]
        fields.append({"name": f"보유 종목 {len(holdings)}",
                       "value": _clip("\n".join(lines)), "inline": False})
    else:
        # 보유 0 과 "조회 못 함"은 다르다. 호출자가 빈 목록을 준 것만 여기서 말한다.
        fields.append({"name": "보유 종목", "value": "없음", "inline": False})
    fields.append({"name": "부서장 서술 (비공식·서술 전용)",
                   "value": _clip(narrative), "inline": False})

    return _send({
        "title": _clip(f"주간 포트폴리오 {report.get('period_start')} ~ "
                       f"{report.get('accounting_date')}", _TITLE_LIMIT),
        "color": _COLOR_WEEKLY,
        "fields": fields,
    }, env)


def send_notice(title: str, detail: str, *, env: dict | None = None) -> dict:
    """보고를 만들지 못했을 때 그 사실을 알린다.

    **침묵하지 않는 것이 요점이다.** 휴장이든 시세 장애든 스케줄 시각에 아무 메시지가
    없으면 둘을 구분할 수 없고, 파이프라인이 죽은 것도 조용한 성공처럼 보인다.
    """
    return _send({"title": _clip(title, _TITLE_LIMIT), "color": _COLOR_BLOCKED,
                  "fields": [{"name": "사유", "value": _clip(detail), "inline": False}]}, env)


# ── 자체 점검 (네트워크 없음) ──────────────────────────────────────────────
_OUT = {
    "accounting_date": "2026-08-10", "nav_status": "PRELIMINARY",
    "snapshot": {"nav": "100693868", "cash": "96073868",
                 "securities_value": "4620000", "quality_status": "WARN"},
    "report": {"period_start": "2026-08-10", "accounting_date": "2026-08-10",
               "nav": {"open": "99998950", "close": "100693868", "change": "694918"},
               "pnl": {"realized": "280000", "unrealized": "420000", "net": "694918",
                       "unexplained": "0", "return_pct": "0.0069"},
               "cost": {"fees": "462", "taxes": "4620", "total": "5082"},
               "drawdown": {"max": "0", "max_pct": None}},
    "recon": {"position": {"result": "matched"}},
    "breaks": [], "material_break_count": 0,
    "projection_check": {"rebuild_deterministic": True, "trial_balance_balanced": True},
    "nav_identity": {"checked": True, "ok": True},
    "narrative": "대사 일치, 평가 정상, NAV 는 Preliminary 다.",
    "fallbacks": [], "is_official": False,
}
_ENV = {"DISCORD_WEBHOOK_URL": "https://discord.test/hook"}


def _capture(fn, *args, **kwargs):
    sent = {}

    def _fake(url, body):
        sent["url"], sent["body"] = url, body
        return 204, ""

    orig, globals()["_post"] = _post, _fake
    try:
        result = fn(*args, env=_ENV, **kwargs)
    finally:
        globals()["_post"] = orig
    return result, sent


def _check_missing_config_skips_without_network():
    def _boom(*a, **k):
        raise AssertionError("설정이 없는데 네트워크 호출을 시도했다")

    orig, globals()["_post"] = _post, _boom
    try:
        assert send_close(_OUT, env={}) == {
            "ok": False, "reason": "DISCORD_WEBHOOK_URL 미설정 - 발송 생략"}
    finally:
        globals()["_post"] = orig
    print("  미설정 시 네트워크 미호출  OK")


def _check_close_payload():
    result, sent = _capture(send_close, _OUT)
    assert result == {"ok": True, "status": 204}, result
    assert sent["url"] == "https://discord.test/hook"
    embed = sent["body"]["embeds"][0]
    by_name = {f["name"]: f["value"] for f in embed["fields"]}
    # 수치는 결정론 값 그대로다 - 여기서 다시 계산하지 않는다
    assert by_name["NAV"] == "100693868", by_name["NAV"]
    assert by_name["실현손익"] == "280000" and by_name["평가손익"] == "420000"
    assert by_name["순손익"] == "694918" and by_name["미설명 손익"] == "0"
    assert by_name["판정"] == "PRELIMINARY"
    assert embed["color"] == _COLOR_OK
    print("  일일 마감 Payload          OK")


def _check_numbers_never_floats():
    """금액이 JSON number 로 나가면 Decimal 이 double 로 깨진다."""
    _, sent = _capture(send_close, _OUT)
    for field in sent["body"]["embeds"][0]["fields"]:
        assert isinstance(field["value"], str), f"{field['name']} 이 문자열이 아니다"
    raw = json.dumps(sent["body"], ensure_ascii=False)
    assert '"100693868"' in raw, "NAV 가 문자열로 직렬화되지 않았다"
    print("  금액 문자열 계약           OK")


def _check_narrative_is_separated():
    """서술과 수치가 한 필드에 섞이지 않는다(팀 가이드 원칙 5)."""
    lying = {**_OUT, "narrative": "NAV 는 999원으로 확정했다"}
    _, sent = _capture(send_close, lying)
    fields = sent["body"]["embeds"][0]["fields"]
    narrative_fields = [f for f in fields if "서술" in f["name"]]
    assert len(narrative_fields) == 1, narrative_fields
    assert "999" in narrative_fields[0]["value"], "서술이 통째로 사라졌다"
    # 서술이 뭐라 하든 수치 필드는 결정론 값 그대로다
    by_name = {f["name"]: f["value"] for f in fields}
    assert by_name["NAV"] == "100693868", "LLM 문장이 수치 필드를 바꿨다"
    print("  서술 ↔ 수치 분리           OK")


def _check_official_never_claimed():
    _, sent = _capture(send_close, _OUT)
    footer = sent["body"]["embeds"][0]["footer"]["text"]
    assert "비공식" in footer and "Preliminary" in footer, footer
    assert "독립 승인" in footer, footer
    print("  공식 NAV 비주장            OK")


def _check_blocked_close():
    blocked = {**_OUT, "nav_status": "BLOCKED", "snapshot": None, "report": None,
               "material_break_count": 2,
               "breaks": [{"severity": "material"}, {"severity": "low"}],
               "recon": {"_none": {"result": "not_reconciled"}}}
    _, sent = _capture(send_close, blocked)
    embed = sent["body"]["embeds"][0]
    by_name = {f["name"]: f["value"] for f in embed["fields"]}
    assert embed["color"] == _COLOR_BLOCKED
    assert "BLOCKED" in embed["title"]
    # 없는 수치를 0 으로 채우지 않는다
    assert by_name["NAV"] == "-" and by_name["순손익"] == "-", by_name
    assert "Material Break 2건" in by_name["차단 사유"], by_name["차단 사유"]
    assert "미수행" in by_name["대사"], by_name["대사"]
    print("  차단 마감 표기             OK")


def _check_weekly_payload():
    holdings = [{"symbol": "005930", "quantity": "60", "market_value": "4620000",
                 "weight": "0.0459"}]
    result, sent = _capture(send_weekly, {**_OUT["report"], "period_start": "2026-08-03"},
                            holdings, narrative="주간 정상")
    assert result["ok"] is True
    embed = sent["body"]["embeds"][0]
    assert "2026-08-03 ~ 2026-08-10" in embed["title"], embed["title"]
    by_name = {f["name"]: f["value"] for f in embed["fields"]}
    assert by_name["기초 NAV"] == "99998950" and by_name["기말 NAV"] == "100693868"
    # 비율은 퍼센트로 줄고 금액은 한 자리도 안 줄어든다
    assert by_name["수익률"] == "0.69%", by_name["수익률"]
    assert by_name["순손익"] == "694918", "금액에 반올림이 붙었다"
    assert "005930" in by_name["보유 종목 1"] and "4.59%" in by_name["보유 종목 1"]
    # 보유가 없는 것과 못 읽은 것을 구분한다
    _, empty = _capture(send_weekly, _OUT["report"], [])
    names = {f["name"] for f in empty["body"]["embeds"][0]["fields"]}
    assert "보유 종목" in names, names
    print("  주간 보고 Payload          OK")


def _check_ratio_display_only():
    """비율만 줄이고 금액은 안 건드린다. 해석 못 한 값을 0% 로 만들지도 않는다."""
    assert _percent("0.006351264795869131840675371144") == "0.64%"
    assert _percent("0.045") == "4.50%" and _percent("-0.012") == "-1.20%"
    assert _percent(None) == "-" and _percent("") == "-"
    assert _percent("알 수 없음") == "알 수 없음", "해석 못 한 비율이 0% 가 됐다"
    # 금액 필드는 _money_fields 로만 나간다 - 여기 반올림 경로가 없어야 한다
    fields = _money_fields({"NAV": "100693868.123456789"})
    assert fields[0]["value"] == "100693868.123456789", fields
    print("  비율만 표시 축약           OK")


def _check_notice_breaks_silence():
    result, sent = _capture(send_notice, "일일 마감 보고 불가", "스냅샷 0건")
    assert result["ok"] is True
    assert "스냅샷 0건" in sent["body"]["embeds"][0]["fields"][0]["value"]
    print("  보고 불가 통지             OK")


def _check_limits_respected():
    long_out = {**_OUT, "narrative": "가" * 5000}
    _, sent = _capture(send_close, long_out)
    embed = sent["body"]["embeds"][0]
    assert len(embed["fields"]) <= _EMBED_FIELDS
    for field in embed["fields"]:
        assert len(field["value"]) <= _FIELD_LIMIT, field["name"]
    assert len(embed["title"]) <= _TITLE_LIMIT
    narrative = next(f for f in embed["fields"] if "서술" in f["name"])
    assert narrative["value"].endswith("…"), "잘렸는데 표시가 없다"
    print("  Discord 상한 준수          OK")


def _check_send_never_raises():
    def _explode(*a, **k):
        raise RuntimeError("network down")

    orig, globals()["_post"] = _post, _explode
    try:
        result = send_close(_OUT, env=_ENV)
        assert result["ok"] is False and "RuntimeError" in result["reason"], result
    finally:
        globals()["_post"] = orig
    # 4xx 도 성공으로 넘기지 않는다
    def _rejected(url, body):
        return 400, '{"message":"bad webhook"}'

    orig, globals()["_post"] = _post, _rejected
    try:
        assert send_close(_OUT, env=_ENV)["ok"] is False
    finally:
        globals()["_post"] = orig
    print("  예외 흡수 (Projection)     OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("accounting discord_reporter 자체 점검 (네트워크 없음)")
    _check_missing_config_skips_without_network()
    _check_close_payload()
    _check_numbers_never_floats()
    _check_narrative_is_separated()
    _check_official_never_claimed()
    _check_blocked_close()
    _check_weekly_payload()
    _check_ratio_display_only()
    _check_notice_breaks_silence()
    _check_limits_respected()
    _check_send_never_raises()
    print("discord_reporter 11개 영역 통과")
