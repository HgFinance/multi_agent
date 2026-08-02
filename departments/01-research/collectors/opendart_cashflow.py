#!/usr/bin/env python3
"""현금흐름표 수집 - F-Score 를 6/9 에서 9/9 로 올리는 마지막 재료.

소유: 재일 (리서치본부)
근거: 재일님 지시 2026-08-02 "우선순위 체크리스트 맞춰서 진행" 중 2순위.
      evidence/methods.py 의 piotroski_f_score partial_reason -
      "현금흐름표(CFO)가 DART 주요계정 API 에 없어 CFO>0·발생액 2개가 불가"

▶ 왜 별도 수집기인가
  기존 opendart_financial.py 는 **다중회사 주요계정**(2019017)을 쓴다 -
  corp_code 를 콤마로 묶어 한 번에 받아 싸지만 BS/IS 만 준다. 현금흐름은
  **단일회사 전체 재무제표**(2019020)에만 있고 회사당 1회 호출이다.
  싼 경로를 버리지 않고 비싼 경로를 필요한 만큼만 더한다.

▶ 범위를 좁힌다
  전 발행사(1,000+)를 매일 도는 것은 낭비다. **감시 바스켓**(news_watchlist,
  코스피200+코스닥150)만 받는다 - 분석가가 실제로 Packet 을 만드는 대상이다.
  범위를 넓히려면 --limit 을 올린다.

▶ 지키는 것
  - account_code 는 기존과 같은 `{sj_div}:{account_nm}` 스킴이다. 2019020 은
    account_id(표준코드)도 주지만 **기존 적재와 스킴이 갈리면 F-Score 가
    두 세계를 뒤져야 한다** - 같은 열쇠를 쓰고 표준코드는 metadata 로 남긴다.
  - CF 만 걸러 넣는다. BS/IS 는 이미 싼 경로로 들어오므로 중복 적재하지 않는다.
  - 실패한 회사는 건너뛰고 계속한다(한 회사 때문에 전체가 멈추지 않는다).
    다만 **전부 실패면 exit 1** - 조용한 0건을 성공으로 보고하지 않는다.

사용
  python collectors/opendart_cashflow.py                    # 자체 점검
  python collectors/opendart_cashflow.py --collect          # 바스켓 전체
  python collectors/opendart_cashflow.py --collect --limit 50
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(_BASE))

COLLECTOR_VERSION = "research-opendart-cashflow-v1"
ENDPOINT = "fnlttSinglAcntAll.json"          # 2019020 단일회사 전체 재무제표
SOURCE_TAG = "opendart:2019020"
CF_SJ_DIV = "CF"                              # 현금흐름표
ANNUAL_REPORT = "11011"                       # 사업보고서 - 연차만 받는다
DEFAULT_LIMIT = 400
RATE_PER_SEC = 2.0

# F-Score·Beneish 가 찾는 핵심 계정. 이름이 회사마다 조금씩 달라 부분일치로
# 본다 - 정확 일치만 쓰면 "영업활동으로 인한 현금흐름" 같은 변형을 놓친다.
CFO_HINTS = ("영업활동", "영업 활동")


def dedupe_facts(facts: list) -> tuple[list, int]:
    """멱등 키가 겹치는 행을 첫 것만 남긴다. (남긴 목록, 버린 수).

    **왜 필요한가**: 우리 계정 열쇠는 `{sj_div}:{account_nm}` 인데, 전체
    재무제표에는 같은 이름이 여러 번 나온다(세부 항목·소계). 그대로 넣으면
    같은 명령 안에서 같은 키가 두 번이라 DB 가 거부한다(실측 2026-08-02
    CardinalityViolation).

    첫 것을 남기는 이유: DART 는 `ord` 순서로 상위 항목을 먼저 준다 -
    영업활동현금흐름 같은 대분류가 세부보다 앞선다. 합계와 세부가 같은
    이름일 때 합계를 취하는 쪽이 F-Score 의 의도에 맞다.
    **버린 수를 반드시 보고한다** - 조용한 절단 금지.
    """
    seen, out, dropped = set(), [], 0
    for f in facts:
        key = (f.corp_code, f.account_code, f.period_end, f.period_type,
               f.consolidation_scope)
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        out.append(f)
    return out, dropped


def period_end_of(year: int, fiscal_month) -> str | None:
    """사업연도 + 결산월 -> 기말일 문자열(YYYY.MM.DD).

    2019020 응답은 기말**일자**를 주지 않고 `thstrm_nm`("제 57 기")만 준다.
    날짜를 지어내면 PIT 가 통째로 틀리므로 reference.issuers 의 결산월에서
    결정론으로 만든다. 결산월을 모르면 None - 12월이라고 가정하지 않는다.
    """
    import calendar

    try:
        fm = int(fiscal_month)
    except (TypeError, ValueError):
        return None
    if not 1 <= fm <= 12:
        return None
    last = calendar.monthrange(int(year), fm)[1]
    return f"{int(year)}.{fm:02d}.{last:02d}"


def is_cashflow_row(row: dict) -> bool:
    """현금흐름표 행인가. sj_div 가 유일한 근거다 - 이름으로 추측하지 않는다."""
    return str(row.get("sj_div", "")).strip().upper() == CF_SJ_DIV


def looks_like_cfo(account_nm: str) -> bool:
    """영업활동현금흐름으로 보이는가. **판정이 아니라 표시용이다** -
    실제 F-Score 계산은 저장된 계정명을 보고 하고, 여기서는 수집 로그에
    '핵심 계정을 받았는지'를 알리는 데만 쓴다."""
    nm = str(account_nm or "")
    return any(h in nm for h in CFO_HINTS) and "현금흐름" in nm


def cashflow_rows(payload: dict) -> list[dict]:
    """API 응답 -> 현금흐름 행. status 가 정상이 아니면 예외(빈 목록 금지)."""
    status = str(payload.get("status", "")).strip()
    if status == "013":          # 조회 데이터 없음 - 사실이므로 빈 목록이 맞다
        return []
    if status != "000":
        raise RuntimeError(f"DART status={status} msg={payload.get('message')}")
    return [r for r in (payload.get("list") or []) if is_cashflow_row(r)]


def collect(limit: int = DEFAULT_LIMIT) -> int:
    import urllib.parse
    import urllib.request
    import json as _json
    import time

    sys.path.insert(0, str(_BASE.parent / "repository"))
    from news_watch_service import parse_watchlist_file
    from opendart_financial import to_fact
    from reference_repository import SupabaseReferenceRepository
    from source_registry import SourceRegistry, UseScope, load_project_env

    env = load_project_env()
    SourceRegistry(env=env).check_use("opendart", UseScope.FULLTEXT_STORE)
    key = env.get("OPEN_DART_API_KEY", "")
    if not key:
        print("OPEN_DART_API_KEY 가 없다", flush=True)
        return 1

    wl = _BASE.parent / "config" / "news_watchlist.txt"
    symbols = list(parse_watchlist_file(wl.read_text(encoding="utf-8")))[:limit]

    repo = SupabaseReferenceRepository()
    try:
        # 종목코드 -> corp_code. 매핑이 없는 종목은 건너뛴다(지어내지 않는다)
        with repo._conn.cursor() as cur:
            cur.execute("""
                select isym.symbol, iss.corp_code, iss.fiscal_month
                from reference.issuers iss
                join reference.instruments i on i.issuer_id = iss.issuer_id
                join reference.instrument_symbols isym
                  on isym.instrument_id = i.instrument_id and isym.is_primary
                where isym.symbol = any(%s) and iss.corp_code is not null
            """, (symbols,))
            corp_of = {s: (c, fm) for s, c, fm in cur.fetchall()}

        year = datetime.now(timezone.utc).year - 1     # 직전 사업연도
        observed = datetime.now(timezone.utc)
        facts, ok, empty, failed, cfo_seen = [], 0, 0, 0, 0

        for sym in symbols:
            entry = corp_of.get(sym)
            if not entry:
                continue
            corp, fiscal_month = entry
            url = (f"https://opendart.fss.or.kr/api/{ENDPOINT}?crtfc_key={key}"
                   f"&corp_code={corp}&bsns_year={year}&reprt_code={ANNUAL_REPORT}"
                   f"&fs_div=CFS")
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    rows = cashflow_rows(_json.loads(r.read()))
            except Exception as e:  # noqa: BLE001 - 한 회사가 전체를 멈추지 않는다
                failed += 1
                if failed <= 3:
                    print(f"  ⚠ {sym}({corp}): {type(e).__name__} {str(e)[:70]}",
                          flush=True)
                time.sleep(1 / RATE_PER_SEC)
                continue
            if not rows:
                empty += 1
            for row in rows:
                row.setdefault("corp_code", corp)
                # 2019020 응답에는 fs_div·thstrm_dt 가 **없다**(실측 2026-08-02).
                # 지어내지 않고 확실한 근거로만 채운다:
                #  - fs_div: 우리가 CFS 로 요청했으므로 CFS 다(요청 파라미터가 근거)
                #  - 기말일: 사업연도 + 발행사 결산월의 말일(reference.issuers)
                row["fs_div"] = "CFS"
                pe = period_end_of(year, fiscal_month)
                if pe is None:
                    continue          # 결산월을 모르면 기말일을 추측하지 않는다
                row["thstrm_dt"] = pe
                try:
                    f = to_fact(row, observed_at=observed)
                except Exception:  # noqa: BLE001 - 행 하나가 회사를 버리지 않는다
                    continue
                f.metadata["source"] = SOURCE_TAG
                f.metadata["account_id"] = str(row.get("account_id", "")).strip()
                facts.append(f)
                if looks_like_cfo(row.get("account_nm", "")):
                    cfo_seen += 1
            ok += 1
            time.sleep(1 / RATE_PER_SEC)

        if not facts:
            print(f"{COLLECTOR_VERSION}: 적재할 현금흐름 0건 "
                  f"(조회 {ok} / 빈 응답 {empty} / 실패 {failed})", flush=True)
            return 1

        facts, dropped = dedupe_facts(facts)
        _, _, src = repo.sync_data_sources()
        n, u, unlinked = repo.upsert_financial_facts(facts, source_id=src["opendart"])
        print(f"{COLLECTOR_VERSION}: 회사 {ok} / 현금흐름 {len(facts)}건 "
              f"(영업활동 {cfo_seen}) -> 신규 {n} / 갱신 {u} / issuer 미연결 {unlinked} "
              f"(빈 응답 {empty}, 실패 {failed}, 동명중복 제외 {dropped})", flush=True)
        return 1 if failed > ok else 0
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·DB 없음
# ---------------------------------------------------------------------------

def _check_dedupe():
    """동명 항목은 첫 것(상위 항목)만 남기고 버린 수를 보고한다."""
    from dataclasses import dataclass
    from datetime import date as _d

    @dataclass
    class _F:
        corp_code: str; account_code: str; period_end: object
        period_type: str; consolidation_scope: str; tag: str

    pe = _d(2025, 12, 31)
    facts = [_F("A", "CF:영업활동현금흐름", pe, "ANNUAL", "CONSOLIDATED", "합계"),
             _F("A", "CF:영업활동현금흐름", pe, "ANNUAL", "CONSOLIDATED", "세부"),
             _F("A", "CF:투자활동현금흐름", pe, "ANNUAL", "CONSOLIDATED", "합계"),
             _F("B", "CF:영업활동현금흐름", pe, "ANNUAL", "CONSOLIDATED", "합계")]
    kept, dropped = dedupe_facts(facts)
    assert dropped == 1 and len(kept) == 3, (len(kept), dropped)
    assert kept[0].tag == "합계", "첫 것(상위 항목)을 남겨야 한다"
    assert dedupe_facts([]) == ([], 0)
    print("  동명 중복 제거          OK")


def _check_period_end():
    """기말일은 결산월에서 결정론으로 - 모르면 12월이라고 가정하지 않는다."""
    assert period_end_of(2025, 12) == "2025.12.31"
    assert period_end_of(2025, 3) == "2025.03.31"
    assert period_end_of(2024, 2) == "2024.02.29", "윤년 처리"
    assert period_end_of(2025, 6) == "2025.06.30"
    for bad in (None, 0, 13, "봄", ""):
        assert period_end_of(2025, bad) is None, bad
    print("  기말일 산출             OK")


def _check_cf_filter():
    """sj_div 가 유일한 근거다 - 이름으로 추측하면 BS 항목이 섞인다."""
    rows = [{"sj_div": "CF", "account_nm": "영업활동현금흐름"},
            {"sj_div": "BS", "account_nm": "현금및현금성자산"},
            {"sj_div": "cf", "account_nm": "투자활동현금흐름"},
            {"sj_div": "IS", "account_nm": "매출액"}]
    got = [r["account_nm"] for r in rows if is_cashflow_row(r)]
    assert got == ["영업활동현금흐름", "투자활동현금흐름"], got
    assert not is_cashflow_row({}), "sj_div 없는 행이 통과했다"
    print("  CF 행 판별               OK")


def _check_status_handling():
    """013(데이터 없음)은 사실이라 빈 목록, 그 외 오류는 예외."""
    assert cashflow_rows({"status": "013", "message": "없음"}) == []
    ok = cashflow_rows({"status": "000", "list": [
        {"sj_div": "CF", "account_nm": "영업활동현금흐름"},
        {"sj_div": "BS", "account_nm": "자산총계"}]})
    assert len(ok) == 1
    for bad in ("020", "800", ""):
        try:
            cashflow_rows({"status": bad, "message": "x"})
            raise AssertionError(f"status={bad} 가 빈 목록으로 통과했다")
        except RuntimeError:
            pass
    print("  status 처리              OK")


def _check_cfo_hint():
    for nm in ("영업활동현금흐름", "영업활동으로 인한 현금흐름",
               "영업 활동 현금흐름"):
        assert looks_like_cfo(nm), nm
    for nm in ("투자활동현금흐름", "재무활동현금흐름", "영업이익", ""):
        assert not looks_like_cfo(nm), nm
    print("  CFO 표시 힌트            OK")


def _check_scheme_matches_existing():
    """기존 적재와 같은 열쇠를 써야 F-Score 가 한 세계만 뒤진다."""
    from opendart_financial import ACCOUNT_CODE_SCHEME, to_fact

    row = {"corp_code": "00126380", "account_nm": "영업활동현금흐름",
           "sj_div": "CF", "fs_div": "CFS", "reprt_code": "11011",
           "rcept_no": "20260315000001", "thstrm_dt": "2025.12.31",
           "thstrm_amount": "1,234,000", "currency": "KRW"}
    f = to_fact(row, observed_at=datetime.now(timezone.utc))
    assert f.account_code == "CF:영업활동현금흐름", f.account_code
    assert f.metadata["account_code_scheme"] == ACCOUNT_CODE_SCHEME
    print("  계정 스킴 일치           OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--collect" in sys.argv:
        a = sys.argv
        lim = int(a[a.index("--limit") + 1]) if "--limit" in a else DEFAULT_LIMIT
        raise SystemExit(collect(lim))

    print(f"{COLLECTOR_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_dedupe()
    _check_period_end()
    _check_cf_filter()
    _check_status_handling()
    _check_cfo_hint()
    _check_scheme_matches_existing()
    print("현금흐름 수집 6개 영역 통과. 적재는 --collect")
