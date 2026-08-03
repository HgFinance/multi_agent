#!/usr/bin/env python3
"""Evidence Bundle 조립기 - 리서치본부 파이프라인의 결정론 수집·계산 계층.

담당: 재일 (리서치/퀀트)
근거: 본부 파이프라인(scripts.py) 실측 사고 - Packet 초안에서 로컬 LLM(qwen)이
      000660 의 +27% 급등을 "하락"으로 서술했다. 원인은 등락 방향 판단을 LLM 에
      맡긴 것. 원칙(집계·계산은 결정론 코드, LLM 은 서술만)대로 등락률·수익률·
      레인지 위치를 이 모듈이 코드로 계산해 확정 수치로 프롬프트에 넘긴다.

계약 (파이프라인이 깨지면 안 된다):
  assemble_bundle(symbol) -> dict
    scripts.py assemble_evidence 의 기존 키(daily_closes_recent, last_trade,
    news_headlines, disclosures_7d)를 그대로 유지하고
    price_context, as_of 를 추가한다.

price_context 규칙 (지어내지 않는다 - 레포 핵심 원칙):
  - market-api /bars (interval=1D, source=ls_chart) 종가 시계열로만 계산한다.
  - 봉이 부족하면 해당 필드는 None, note 에 "미확인"으로 남긴다.
  - market-api 미가동/오류면 {"status": "UNAVAILABLE", "reason": ...} -
    판단 불가를 통과로 위장하지 않는다.

실행: python bundle.py   # 자체 점검 (네트워크 없음 - 가짜 응답)
"""
from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

BUNDLE_VERSION = "evidence-bundle-v1"
KST = timezone(timedelta(hours=9))

MARKET_API_DEFAULT = os.environ.get("MARKET_API_URL", "http://127.0.0.1:8036")
RESEARCH_API_DEFAULT = os.environ.get("RESEARCH_API_URL", "http://127.0.0.1:8035")

# 20거래일 수익률에 비교 기준일까지 21봉이 필요하다
PRICE_BARS_NEEDED = 21


# Evidence 조립은 **큐레이션**이다 - 뉴스·공시를 모아 다른 분석가에게 넘긴다.
# 그래서 RES-08(Evidence Curator)의 페르소나로 부른다. 총괄(RES-00)이 아니다:
# 우리 허용목록이 총괄을 "종합·위임만, 자체 조회 없음"으로 선언했고, 그 선언을
# 코드가 어기면 선언이 거짓이 된다.
# 헤더가 없으면 Tool Gateway 가 익명으로 보고 강제 모드에서 403 이다.
BUNDLE_PERSONA = "rag-librarian-evidence-curator"


def _http_get(url: str, timeout: int = 30):
    from api_client import get_json

    return get_json(url, persona=BUNDLE_PERSONA, timeout=timeout)


def _pct(cur: float, base: float | None) -> float | None:
    # 기준가 0/결측은 데이터 오류다 - 수치로 위장하지 않고 None 으로 남긴다
    if base is None or base == 0:
        return None
    return round((cur - base) / base * 100.0, 2)


# ── 결정론 가격 컨텍스트 ───────────────────────────────────────────────────
def compute_price_context(bars: list[dict]) -> dict:
    """일봉 dict 목록(bucket_time·close 필수, 정렬 무관) -> 가격 컨텍스트.

    순수 함수 - 네트워크·시계 없이 입력만으로 계산한다(자체 점검 대상).
    """
    rows = sorted((b for b in bars if b.get("close") is not None),
                  key=lambda b: str(b["bucket_time"]), reverse=True)
    if not rows:
        return {"status": "UNAVAILABLE", "reason": "일봉이 0개다 - 등락률 계산 불가"}

    closes = [float(b["close"]) for b in rows]  # 최신 -> 과거
    last = closes[0]
    prev = closes[1] if len(closes) >= 2 else None

    chg_1d = _pct(last, prev)
    ret_5d = _pct(last, closes[5]) if len(closes) >= 6 else None
    ret_20d = _pct(last, closes[20]) if len(closes) >= 21 else None

    # 20거래일 레인지는 창이 꽉 찼을 때만 - 부분 창을 전체처럼 말하지 않는다
    high_20 = max(closes[:20]) if len(closes) >= 20 else None
    low_20 = min(closes[:20]) if len(closes) >= 20 else None
    pos_20 = None
    if high_20 is not None and low_20 is not None and high_20 != low_20:
        pos_20 = round((last - low_20) / (high_20 - low_20) * 100.0, 1)

    if chg_1d is None:
        direction = "미확인"
    elif chg_1d > 0:
        direction = "상승"
    elif chg_1d < 0:
        direction = "하락"
    else:
        direction = "보합"

    missing = [k for k, v in (("change_1d_pct", chg_1d),
                              ("return_5d_pct", ret_5d),
                              ("return_20d_pct", ret_20d),
                              ("range_position_20d_pct", pos_20)) if v is None]
    ctx = {
        "status": "OK" if not missing else "PARTIAL",
        "source": "market-api /bars interval=1D source=ls_chart (코드 계산)",
        "bars_used": len(closes),
        "last_close": last,
        "last_close_date": str(rows[0]["bucket_time"])[:10],
        "prev_close": prev,
        "change_1d_pct": chg_1d,
        "direction_1d": direction,
        "return_5d_pct": ret_5d,
        "return_20d_pct": ret_20d,
        "high_20d": high_20,
        "low_20d": low_20,
        "range_position_20d_pct": pos_20,
    }
    # 종가 시계열(과거->최신). 주장의 사전 확률(evidence/forecast.py)이 여기서
    # 실현변동성을 낸다 - 요약 통계만으로는 변동성을 복원할 수 없다.
    # rows 는 최신순이므로 뒤집어 시간순으로 준다(계산 쪽 관례와 맞춘다).
    ctx["closes"] = list(reversed(closes))
    if missing:
        ctx["note"] = "봉 부족으로 미확인: " + ", ".join(missing)
    return ctx


def fetch_price_context(symbol: str, *, market_api: str | None = None,
                        get: Callable = _http_get) -> dict:
    base = (market_api or MARKET_API_DEFAULT).rstrip("/")
    try:
        bars = get(f"{base}/bars/{symbol}?interval=1D"
                   f"&limit={PRICE_BARS_NEEDED}&source=ls_chart")
    except Exception as e:  # noqa: BLE001
        # 미가동을 통과로 위장하지 않는다 - 사유를 명시하고 UNAVAILABLE 로 끝낸다
        return {"status": "UNAVAILABLE",
                "reason": f"market-api /bars 호출 실패: {type(e).__name__}: {e}"}
    return compute_price_context(bars)


# ── Bundle 조립 (scripts.py assemble_evidence 이관) ────────────────────────
def assemble_bundle(symbol: str, *, market_api: str | None = None,
                    research_api: str | None = None,
                    get: Callable = _http_get) -> dict:
    """기존 evidence 계약 유지 + price_context/as_of 확장.

    news/disclosures/snapshot 실패는 그대로 전파한다(파이프라인이 크게 실패해야
    한다) - price_context 만 UNAVAILABLE 로 자기 기술한다.
    """
    m = (market_api or MARKET_API_DEFAULT).rstrip("/")
    r = (research_api or RESEARCH_API_DEFAULT).rstrip("/")
    bars = get(f"{m}/bars/{symbol}?interval=1D&limit=5")
    snap = get(f"{m}/snapshot/{symbol}")
    news = get(f"{r}/evidence/news?symbol={symbol}&hours=24&limit=8")
    disc = get(f"{r}/evidence/disclosures?symbol={symbol}&days=7&limit=5")
    return {
        "daily_closes_recent": [(b["bucket_time"][:10], float(b["close"]))
                                for b in bars],
        "last_trade": snap.get("last_trade"),
        # ▶ Evidence ID 를 살린다 (2026-08-03, RQF-1 완료기준)
        #   예전에는 뉴스가 document_id 를 버리고 1,2,3 으로 다시 번호를 매겼고
        #   공시는 제목만 남겼다. 그래서 **주장이 인용할 ID 자체가 없었고**,
        #   "어떤 소스에 기댄 판단이 틀렸나" 를 영원히 물을 수 없었다.
        #
        #   ref 와 evidence_id 를 함께 싣는다:
        #     ref('n1','d1')  - 짧아서 LLM 이 정확히 인용한다. UUID 를 받아쓰게
        #                       하면 한 글자만 틀려도 인용이 깨진다.
        #     evidence_id     - 진짜 document_id. 코드가 ref 를 이걸로 바꿔
        #                       claim.evidence_ids 에 넣는다(resolve_refs).
        #   판정은 코드가 한다 - LLM 은 ref 를 가리키기만 한다.
        "news_headlines": [{"ref": f"n{i + 1}",
                            "evidence_id": n.get("document_id"),
                            "title": n["title"],
                            "relation": n["relation_type"],
                            # 저장 허가와 운영 근거 허가는 다른 질문이다
                            "production_authorized": bool(
                                n.get("production_authorized", False))}
                           for i, n in enumerate(news)],
        "disclosures_7d": [{"ref": f"d{i + 1}",
                            "evidence_id": d_.get("document_id"),
                            "title": d_["title"]}
                           for i, d_ in enumerate(disc)],
        "price_context": fetch_price_context(symbol, market_api=m, get=get),
        "as_of": datetime.now(KST).isoformat(timespec="seconds"),
    }


# ── 시점 창작 가드 - 숫자 가드가 못 잡는 날짜 (2026-08-03 사고) ────────────

# 연도를 잡는다. **연도 뒤 구분자를 요구하지 않는다** - 처음 만들 때
# `(19|20)(\d{2})\s*[-./년]` 로 썼더니 이 가드를 만든 계기인 사고 문장
# `"(August 3, 2023)"` 을 못 잡았다(2023 뒤가 괄호라서). 가드가 자기 사고를
# 못 잡으면 없는 것과 같다.
#
# 대신 **앞뒤로 숫자·쉼표·소수점이 붙은 것은 연도가 아니다**(1,995,000원의 995).
# 그리고 원/주/건 같은 단위가 바로 붙으면 금액·수량이지 연도가 아니다 -
# 오탐이 잦으면 사람이 가드를 무시하게 되고, 그러면 진짜를 놓친다.
_DATE_RE = re.compile(
    r"(?<![\d,.])(19|20)(\d{2})(?![\d])(?!\s*(?:원|주|건|개|명|회|대|bp|%))")

# Evidence 가 담는 시점보다 얼마나 과거까지를 정상으로 볼 것인가.
# 재무는 직전 회계연도를 인용하고 GPR 은 지연이 있어 넉넉히 잡는다 - 좁게 잡으면
# 정상 인용을 창작으로 오탐하고, 그러면 가드가 무시된다.
MAX_NARRATIVE_AGE_DAYS = 800


def verify_narrative_dates(text: str, *, as_known_at: datetime,
                           max_age_days: int = MAX_NARRATIVE_AGE_DAYS) -> dict:
    """서술 속 연도가 Evidence 가 담을 수 있는 시점 범위 안인가.

    ▶ 왜 필요한가 (실측 2026-08-03)
      총괄이 005380 Packet 의 facts 4건을 **2023-11-02/03** 날짜로 쓰고 출처까지
      달았다. 우리가 준 적 없는 기사다. 그런데 verify_narrative_numbers 를
      통과했다 - % 수치가 아니라 날짜라서 검사 대상이 아니었다.

    ▶ 판정
      미래(as_known_at 이후) 연도  -> 창작. 아직 오지 않은 일을 사실로 쓸 수 없다
      max_age_days 보다 과거       -> 창작 의심. Evidence 창 밖이다
      **모르는 것을 단정하지 않는다** - 연도 없는 날짜 표현은 판정하지 않는다.
    """
    now = as_known_at.astimezone(timezone.utc)
    oldest = now - timedelta(days=max_age_days)
    bad_future, bad_old = [], []
    for m in _DATE_RE.finditer(text or ""):
        year = int(m.group(1) + m.group(2))
        if year > now.year:
            bad_future.append(year)
        elif year < oldest.year:
            bad_old.append(year)
    ok = not bad_future and not bad_old
    return {
        "ok": ok,
        "future_years": sorted(set(bad_future)),
        "too_old_years": sorted(set(bad_old)),
        "window": f"{oldest.year}~{now.year}",
        "rule": (f"서술의 연도는 as_known_at({now.date()}) 이하이고 "
                 f"{max_age_days}일 이내여야 한다. 연도 없는 표현은 판정하지 않는다."),
    }


# ── 인용 해석 - ref -> 진짜 Evidence ID (RQF-1 완료기준) ──────────────────

class CitationError(ValueError):
    """존재하지 않는 근거를 인용했다. 조용히 버리지 않는다."""


def evidence_index(bundle: dict) -> dict[str, dict]:
    """Bundle 안의 인용 가능한 근거. ref -> {evidence_id, title, kind, ...}.

    evidence_id 가 없는 항목은 **색인에 넣지 않는다** - 인용할 수 없는 것을
    인용 가능한 것처럼 보여주면 하류가 빈 ID 를 근거로 삼는다.
    """
    idx: dict[str, dict] = {}
    for kind, key in (("news", "news_headlines"), ("disclosure", "disclosures_7d")):
        for item in bundle.get(key) or []:
            if not isinstance(item, dict):
                continue
            ref, ev = item.get("ref"), item.get("evidence_id")
            if not ref or not ev:
                continue
            idx[ref] = {"evidence_id": str(ev), "kind": kind,
                        "title": item.get("title"),
                        "production_authorized": bool(
                            item.get("production_authorized", kind == "disclosure"))}
    return idx


def resolve_refs(refs, bundle: dict, *, strict: bool = True) -> tuple[str, ...]:
    """LLM 이 가리킨 ref 를 진짜 evidence_id 로 바꾼다.

    strict=True 면 없는 ref 는 **예외**다. 조용히 빼면 "근거 3건" 이라고 쓴
    주장이 근거 1건으로 저장되고, 그 차이를 아무도 모른다. 근거를 못 찾았으면
    그건 fact 주장이 아니라 inference 다 - 그 판정은 호출자가 한다.
    """
    idx = evidence_index(bundle)
    out, missing = [], []
    for r in refs or ():
        hit = idx.get(str(r).strip())
        if hit is None:
            missing.append(str(r))
            continue
        if hit["evidence_id"] not in out:      # 같은 근거를 두 번 세지 않는다
            out.append(hit["evidence_id"])
    if missing and strict:
        raise CitationError(
            f"Bundle 에 없는 근거를 인용했다: {missing} - "
            f"가능한 ref: {sorted(idx)[:12]}{'...' if len(idx) > 12 else ''}")
    return tuple(out)


def authorized_only(refs, bundle: dict) -> tuple[str, ...]:
    """운영 근거로 써도 되는 것만 남긴다 (production_authorized).

    저장 허가와 운영 허가는 다른 질문이다(마이그레이션 002500). 미승인 소스를
    주문 판단의 근거로 흘려보내지 않는다 - fail-closed 라 기본은 제외다.
    """
    idx = evidence_index(bundle)
    return tuple(idx[r]["evidence_id"] for r in (refs or ())
                 if r in idx and idx[r]["production_authorized"])


# ── 서술 수치 재대조 - 라벨 오서술·수치 창작 가드 (RES-00 Packet 검증용) ──

# ▶ 자릿수 상한을 두지 않는다 (2026-08-03 실측 - 불일치의 진짜 원인)
#   `\d{1,3}(?:\.\d{1,2})?` 였다. 그래서 "-29.8837%" 를 **"837"** 로,
#   "74.353%" 를 "353" 으로 읽었다 - **꼬리만 잘라 읽고** 그 값이 확정치에
#   없으니 창작이라고 표시했다. 실측 불일치 [837.0, 353.0, 908.0] 이 전부 이것이다.
#   가드가 정상 인용을 위반으로 몰면 사람이 가드를 무시한다.
_PCT_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%")
# 셈 단위가 붙은 정수만 검사한다 - 가격("1,718,000원")·날짜("30일")까지 잡으면
# 오탐이 검사를 무력화한다. 실측 근거: Packet 이 "advancers 297개" 처럼 확정치
# (296)에 없는 종목 수를 창작했는데 % 가 아니라 통과했다 (2026-08-01).
_COUNT_RE = re.compile(r"(\d{1,7})\s*(?:개|건|종목)")



def _collect_keyed(v, out: dict, path: str = "") -> None:
    """수치를 **경로와 함께** 모은다 - 어느 값에 맞았는지 말하려면 이름이 필요하다.

    _collect_numbers 는 집합이라 이름을 잃는다. 지표가 늘어 풀이 넓어진 뒤로는
    "통과했다" 만으로 부족하다 - 무엇에 통과했는지가 있어야 우연을 가른다.
    """
    if isinstance(v, bool):
        return
    if isinstance(v, (int, float)):
        if path:
            out[path] = float(v)
    elif isinstance(v, dict):
        for k, x in v.items():
            _collect_keyed(x, out, f"{path}.{k}" if path else str(k))
    elif isinstance(v, (list, tuple)):
        for i, x in enumerate(v):
            _collect_keyed(x, out, f"{path}[{i}]")


def _collect_numbers(v, out: set) -> None:
    if isinstance(v, bool):
        return
    if isinstance(v, (int, float)):
        out.add(round(float(v), 2))
    elif isinstance(v, dict):
        for x in v.values():
            _collect_numbers(x, out)
    elif isinstance(v, (list, tuple)):
        for x in v:
            _collect_numbers(x, out)


def verify_narrative_numbers(text: str, confirmed: dict,
                             *, tolerance: float = 0.1) -> dict:
    """서술 속 % 수치가 코드 계산 확정치 집합 안에 있는지 검사한다.

    +29.95% 실측 사례의 후속 가드: 수치·부호 인용은 정확했지만 서술이 필드
    의미를 바꿔 말하는 문제가 남았다 - 최소한 **확정치에 없는 % 수치를
    창작하는 것**은 여기서 결정론으로 잡는다.

    한계(정직하게): 한국어는 "2.33% 하락"처럼 부호를 단어로 옮기므로
    절대값 일치를 허용한다. 부호 뒤집힘 자체는 이 검사가 아니라 각
    분석가의 모순 강등(verify)이 맡는다.
    """
    allowed: set = set()
    keyed: dict = {}
    _collect_numbers(confirmed, allowed)
    _collect_keyed(confirmed, keyed)
    # ▶ 근거 **제목 문자열 안의 수치**도 확정치다 (2026-08-03)
    #   _collect_numbers 는 수치형만 본다. 그런데 우리가 프롬프트에 넣은 뉴스
    #   제목("7월 판매 5.1% 감소")은 문자열이라 풀에 안 들어갔고, 총괄이 그것을
    #   정확히 인용했는데 창작으로 몰렸다. **우리가 준 것을 인용했는데 창작이라
    #   하면 가드가 거짓말을 하고, 그러면 사람이 가드를 무시한다.**
    for i, t in enumerate((confirmed or {}).get("evidence_titles") or []):
        for m in _PCT_RE.finditer(str(t)):
            allowed.add(float(m.group(1)))
            keyed[f"evidence_title[{i}]#{m.group(1)}"] = float(m.group(1))
        for m in _COUNT_RE.finditer(str(t)):
            allowed.add(float(m.group(1)))
            keyed[f"evidence_title[{i}]#{m.group(1)}"] = float(m.group(1))
    nums = [float(m.group(1)) for m in _PCT_RE.finditer(text or "")]
    unmatched = [n for n in nums
                 if not any(abs(abs(n) - abs(a)) <= tolerance for a in allowed)]
    # ▶ 인용 귀속 - "통과가 검증인지 우연인지" 를 가른다 (2026-08-03)
    #   지표 확장으로 풀이 넓어지면 창작 수치가 우연히 맞는 일이 생긴다.
    #   어느 키에 맞았는지를 세어 모호 비율을 드러낸다. **판정은 바꾸지 않는다** -
    #   통과 여부는 위 unmatched 가 그대로 정한다.
    # 호출부가 evidence/ 를 sys.path 에 넣었는지에 의존하지 않는다 -
    # scripts.py 는 `from evidence.bundle import ...` 로 부르고 agents/ 는
    # evidence/ 를 직접 넣는다. 두 경로 모두에서 되게 한다.
    try:
        from number_guard import quote_quality, trace_quoted
    except ModuleNotFoundError:  # pragma: no cover - 패키지 경로로 들어온 경우
        from evidence.number_guard import quote_quality, trace_quoted

    quality = quote_quality(trace_quoted(text or "", keyed, tolerance=tolerance))
    # 셈 단위 정수는 정확 일치만 - 종목/기사/공시 개수는 반올림 여지가 없다
    counts = [int(m.group(1)) for m in _COUNT_RE.finditer(text or "")]
    unmatched_counts = [c for c in counts if float(c) not in allowed]
    return {"checked": len(nums), "unmatched": unmatched,
            "checked_counts": len(counts), "unmatched_counts": unmatched_counts,
            "ok": not unmatched and not unmatched_counts,
            # 통과가 검증인지 우연인지 - 모호 비율이 높으면 풀이 헐거워진 것이다
            "quote_quality": quality,
            "pool_size": len(keyed)}


# ── 자체 점검 (네트워크 없음) ──────────────────────────────────────────────
def _fake_bars(closes_latest_first: list[float]) -> list[dict]:
    d0 = datetime(2026, 7, 30, 15, tzinfo=timezone.utc)
    return [{"bucket_time": (d0 - timedelta(days=i)).isoformat(),
             "close": c, "source": "ls_chart", "is_final": True}
            for i, c in enumerate(closes_latest_first)]


def _check_surge_plus27():
    # 실측 사고 재현 - +27% 급등이 부호 그대로 +27.0 / "상승"으로 나와야 한다
    ctx = compute_price_context(_fake_bars([1270.0, 1000.0] + [1000.0] * 19))
    assert ctx["status"] == "OK", ctx
    assert ctx["change_1d_pct"] == 27.0 and ctx["change_1d_pct"] > 0, ctx
    assert ctx["direction_1d"] == "상승", ctx
    assert ctx["return_5d_pct"] == 27.0 and ctx["return_20d_pct"] == 27.0, ctx
    assert ctx["high_20d"] == 1270.0 and ctx["low_20d"] == 1000.0, ctx
    assert ctx["range_position_20d_pct"] == 100.0, ctx
    # 정렬 무관 - 과거->최신 순으로 넣어도 같은 결과
    asc = compute_price_context(list(reversed(_fake_bars([1270.0] + [1000.0] * 20))))
    assert asc["change_1d_pct"] == 27.0 and asc["direction_1d"] == "상승", asc
    print("  급등 +27% 부호/방향      OK")


def _check_drop():
    ctx = compute_price_context(_fake_bars([730.0, 1000.0] + [1000.0] * 19))
    assert ctx["change_1d_pct"] == -27.0 and ctx["change_1d_pct"] < 0, ctx
    assert ctx["direction_1d"] == "하락", ctx
    flat = compute_price_context(_fake_bars([1000.0, 1000.0] + [1000.0] * 19))
    assert flat["change_1d_pct"] == 0.0 and flat["direction_1d"] == "보합", flat
    print("  하락 부호 / 보합         OK")


def _check_insufficient_bars():
    # 3봉 - 5/20일 수익률·레인지는 None 이고 "미확인"으로 남아야 한다
    ctx = compute_price_context(_fake_bars([1100.0, 1000.0, 900.0]))
    assert ctx["status"] == "PARTIAL", ctx
    assert ctx["change_1d_pct"] == 10.0, ctx
    assert ctx["return_5d_pct"] is None and ctx["return_20d_pct"] is None, ctx
    assert ctx["high_20d"] is None and ctx["range_position_20d_pct"] is None, ctx
    assert "미확인" in ctx["note"], ctx
    # 1봉 - 전일이 없으니 등락률도 미확인
    one = compute_price_context(_fake_bars([1100.0]))
    assert one["change_1d_pct"] is None and one["direction_1d"] == "미확인", one
    # 0봉 - 계산 자체가 불가
    assert compute_price_context([])["status"] == "UNAVAILABLE"
    # 기준가 0 - 등락률을 지어내지 않는다
    zero = compute_price_context(_fake_bars([1100.0, 0.0]))
    assert zero["change_1d_pct"] is None and zero["direction_1d"] == "미확인", zero
    print("  봉 부족/결측 None 처리   OK")


def _check_api_unavailable():
    def down(url: str):
        raise OSError("connection refused")
    ctx = fetch_price_context("000660", market_api="http://127.0.0.1:1", get=down)
    assert ctx["status"] == "UNAVAILABLE", ctx
    assert "OSError" in ctx["reason"] and "connection refused" in ctx["reason"], ctx
    print("  API 불가 UNAVAILABLE     OK")


def _check_bundle_contract():
    d0 = _fake_bars([1270.0, 1000.0] + [1000.0] * 19)

    def fake_get(url: str):
        if "/bars/" in url and "source=ls_chart" in url:
            return d0
        if "/bars/" in url:
            return d0[:5]
        if "/snapshot/" in url:
            return {"symbol": "000660", "last_trade": {"price": "1270.0"}}
        if "/evidence/news" in url:
            return [{"document_id": "doc-news-1", "title": "급등 뉴스",
                     "relation_type": "direct", "production_authorized": False},
                    {"document_id": "doc-news-2", "title": "승인된 기사",
                     "relation_type": "direct", "production_authorized": True}]
        if "/evidence/disclosures" in url:
            return [{"document_id": "doc-disc-1", "title": "공시 1"}]
        raise AssertionError(f"예상 밖 URL: {url}")

    b = assemble_bundle("000660", market_api="http://x", research_api="http://y",
                        get=fake_get)
    # 기존 소비자(draft_packet)가 쓰던 키가 전부 살아 있어야 한다
    for k in ("daily_closes_recent", "last_trade", "news_headlines",
              "disclosures_7d", "price_context", "as_of"):
        assert k in b, f"{k} 누락"
    assert len(b["daily_closes_recent"]) == 5
    assert b["news_headlines"][0] == {
        "ref": "n1", "evidence_id": "doc-news-1", "title": "급등 뉴스",
        "relation": "direct", "production_authorized": False}, b["news_headlines"][0]
    # 공시도 이제 인용할 ID 를 갖는다 (예전엔 제목 문자열뿐이었다)
    assert b["disclosures_7d"][0] == {
        "ref": "d1", "evidence_id": "doc-disc-1", "title": "공시 1"}
    assert b["price_context"]["change_1d_pct"] == 27.0
    assert b["price_context"]["direction_1d"] == "상승"
    print("  Bundle 계약 유지+확장    OK")


def _check_citation_resolution():
    """ref -> 진짜 evidence_id. 없는 인용을 조용히 버리지 않는가."""
    bundle = {
        "news_headlines": [
            {"ref": "n1", "evidence_id": "doc-news-1", "title": "a",
             "production_authorized": False},
            {"ref": "n2", "evidence_id": "doc-news-2", "title": "b",
             "production_authorized": True},
            {"ref": "n3", "title": "id 없는 항목"},          # 색인에 안 들어간다
        ],
        "disclosures_7d": [{"ref": "d1", "evidence_id": "doc-disc-1", "title": "c"}],
    }
    idx = evidence_index(bundle)
    assert set(idx) == {"n1", "n2", "d1"}, set(idx)
    assert idx["d1"]["kind"] == "disclosure"

    assert resolve_refs(("n1", "d1"), bundle) == ("doc-news-1", "doc-disc-1")
    # 같은 근거를 두 번 세지 않는다
    assert resolve_refs(("n1", "n1"), bundle) == ("doc-news-1",)
    # 없는 ref 는 예외다 - 조용히 빼면 '근거 3건' 이 1건으로 저장된다
    for bad in (("n9",), ("n1", "없음")):
        try:
            resolve_refs(bad, bundle)
            raise AssertionError(f"없는 ref 가 통과했다: {bad}")
        except CitationError as e:
            assert "없는 근거를 인용했다" in str(e)
    # evidence_id 가 없는 항목도 인용 대상이 아니다
    try:
        resolve_refs(("n3",), bundle)
        raise AssertionError("id 없는 항목이 인용됐다")
    except CitationError:
        pass
    # 관대 모드는 빼되, 그건 호출자가 명시해야 한다
    assert resolve_refs(("n1", "n9"), bundle, strict=False) == ("doc-news-1",)

    # 운영 근거 허가 필터 - 기본은 제외(fail-closed), 공시는 승인으로 본다
    assert authorized_only(("n1", "n2", "d1"), bundle) == ("doc-news-2", "doc-disc-1")
    assert authorized_only(("n1",), bundle) == ()
    print("  인용 해석·미승인 차단    OK")


def _check_date_guard():
    """시점 창작 - 숫자 가드가 못 잡던 것 (2026-08-03 사고 재현)."""
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    # 실제 사고 문장 그대로
    bad = ("KOSPI closed at 2,485.30 on 2023-11-03, following a drop on 2023-11-02.")
    r = verify_narrative_dates(bad, as_known_at=now)
    assert r["ok"] is False and r["too_old_years"] == [2023], r

    # 미래 연도도 창작이다
    f = verify_narrative_dates("2027년 실적이 개선됐다", as_known_at=now)
    assert f["ok"] is False and f["future_years"] == [2027], f

    # ▶ 이 가드를 만든 계기인 실제 사고 문장 - 연도 뒤가 괄호다
    for incident in ("Samsung shares dropped 7% (August 3, 2023)",
                     "Q3 2023 sales of 1.16 million vehicles",
                     "timestamp: 2023-10-15T14:30:00Z"):
        r2 = verify_narrative_dates(incident, as_known_at=now)
        assert r2["ok"] is False, f"사고 문장을 못 잡았다: {incident}"

    # 정상 인용은 통과한다 - 직전 회계연도를 쓰는 것은 펀더멘털의 정상 동작
    for good in ("2025-12-31 기준 매출액", "2026년 8월 공시", "2026.07.27 GPR 지수",
                 "2026년 7월 판매 318,454대"):
        assert verify_narrative_dates(good, as_known_at=now)["ok"], good

    # **모르는 것을 단정하지 않는다** - 연도 없는 표현은 판정 대상이 아니다
    assert verify_narrative_dates("11월 3일 급등", as_known_at=now)["ok"]
    # 가격·수량은 연도가 아니다 (오탐하면 가드가 무시된다)
    for money in ("종가 1,718,000원 거래량 2,400주", "매출액 1,995,000원",
                  "목표가 2,000원", "발행주식 1,999,000주", "직원 2,024명"):
        assert verify_narrative_dates(money, as_known_at=now)["ok"], money
    print("  시점 창작 가드           OK")


def _check_narrative_numbers():
    """서술 수치 재대조 - 창작 수치는 잡고, 확정치 인용·절대값 표기는 통과."""
    confirmed = {"price_context": {"change_1d_pct": 29.95, "return_5d_pct": -2.33,
                                   "range_position_20d_pct": 35.9}}
    ok = verify_narrative_numbers("전일 대비 +29.95% 급등, 5일 기준 2.33% 하락", confirmed)
    assert ok["ok"] and ok["checked"] == 2, ok
    # 확정치에 없는 수치 창작 -> 적발
    bad = verify_narrative_numbers("최근 +12.5% 상승 흐름", confirmed)
    assert not bad["ok"] and bad["unmatched"] == [12.5], bad
    # 허용 오차 - 반올림 표기(29.9%)는 통과, 크게 다르면(28%) 적발
    assert verify_narrative_numbers("29.9% 상승", confirmed, tolerance=0.1)["ok"]
    assert not verify_narrative_numbers("28% 상승", confirmed)["ok"]
    # 수치 없는 서술·빈 문자열은 검사 0건으로 통과
    assert verify_narrative_numbers("추세가 강하다", confirmed)["checked"] == 0
    assert verify_narrative_numbers("", confirmed)["ok"]
    # 셈 단위 정수 - "297개" 창작 실측 사례: 확정치(296) 밖이면 적발
    conf2 = {"regime": {"latest_advancers": 296, "latest_decliners": 51}}
    good = verify_narrative_numbers("상승 296개, 하락 51종목", conf2)
    assert good["ok"] and good["checked_counts"] == 2
    bad2 = verify_narrative_numbers("상승 297개, 하락 132종목", conf2)
    assert not bad2["ok"] and bad2["unmatched_counts"] == [297, 132]
    # 단위 없는 정수(가격·날짜)는 검사하지 않는다 - 오탐 방지
    assert verify_narrative_numbers("종가 1718000원, 7월 30일", conf2)["checked_counts"] == 0
    print("  서술 수치 재대조 가드    OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{BUNDLE_VERSION} 자체 점검 (네트워크 없음)")
    _check_surge_plus27()
    _check_drop()
    _check_insufficient_bars()
    _check_api_unavailable()
    _check_bundle_contract()
    _check_citation_resolution()
    _check_date_guard()
    _check_narrative_numbers()
    print("Evidence Bundle 8개 영역 통과.")
