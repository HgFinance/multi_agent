#!/usr/bin/env python3
"""Sprint J2: Open DART 재무정보 수집 (다중회사 주요계정).

소유: 재일 (리서치본부)
근거: docs/06-integrations/opendart/03-periodic-report-financial-information.md (2019017)
      docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 3.1(재무), 5.2, 8.2
      supabase/migrations/20260729000300_research_quant_strategy.sql (research.financial_facts)

`fnlttMultiAcnt.json`(2019017)은 corp_code 를 콤마로 여러 개 받아 한 번에 조회한다.
315개 발행사를 회사별 1회 호출하지 않아도 된다.

▶ 결정이 필요했던 것 두 개. 근거를 남긴다.

  1. **account_code 가 응답에 없다.** 다중회사 주요계정은 `account_nm`(한글 계정명)만
     주고 표준 계정코드를 주지 않는다(단일회사 전체 재무제표 2019020 은 account_id 를
     준다). financial_facts.account_code 가 NOT NULL 이므로 값을 만들어야 한다.

     `{sj_div}:{account_nm}` 형태로 쓴다 - 예: `BS:유동자산`. 표준코드처럼 보이는 값을
     날조하지 않고, metadata.account_code_scheme 에 `dart_major_account_nm` 을 남겨
     이것이 IFRS 표준코드가 아님을 명시한다. 표준코드가 필요해지면 2019020 이나
     XBRL 택사노미(2020001)로 보강한다.

  2. **당기 + 전기(연차 한정)를 적재한다.** (2026-08-01 개정 - 원래는 당기만이었다)
     응답은 당기·전기·전전기 3개 기간을 한 번에 준다. 원래 미룬 이유(같은
     period_end 를 두 보고서가 보고할 때 개정본 구분 불가)는 upsert 가 자연키
     DO UPDATE 로 **최신 관측 수렴**임이 확인되며 해소됐다 - 정정 재무제표의
     전기 재작성값이 오히려 최신 상태다. 분기 보고서의 frmtrm 은 BS(전기말)와
     IS(누적/동기)의 의미가 갈려 **연차(11011)만** 방출한다(prior_fact_from_row).
     이로써 펀더멘털 분석가의 YoY 가 활성화된다. 전전기는 여전히 metadata 참고.

▶ PIT 한계
  published_at 을 rcept_no 앞 8자리(접수일)로 만든다. 공시와 같은 DAY 정밀도 문제이며
  Backtest 는 observed_at 을 본다(가이드 4.2).

자체 점검(호출 없음): python departments/01-research/collectors/opendart_financial.py
실제 수집:            python departments/01-research/collectors/opendart_financial.py --collect
                      (옵션: --year 2025 --reprt 11011 --limit 20)
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from datetime import time as dtime
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from opendart_collector import KST, OpenDartClient, OpenDartError

COLLECTOR_VERSION = "research-opendart-financial-v1"

ENDPOINT = "fnlttMultiAcnt.json"
# 한 번에 넣을 corp_code 수. 문서에 상한이 없어 보수적으로 잡는다.
CORP_BATCH = 50

# reprt_code -> period_type. 문서 요청 인자 설명에서 확인한 값이다.
REPRT_TO_PERIOD = {
    "11011": "ANNUAL",
    "11012": "HALF",
    "11013": "Q1",
    "11014": "Q3",
}

# fs_div -> consolidation_scope
FS_DIV_TO_SCOPE = {"CFS": "CONSOLIDATED", "OFS": "SEPARATE"}

# 금액은 원 단위 절대값이다. 통화는 currency Column 이 따로 들고 있으므로
# unit 은 스케일을 뜻하는 값으로 둔다.
UNIT_ABSOLUTE = "ABSOLUTE"

ACCOUNT_CODE_SCHEME = "dart_major_account_nm"


@dataclass(frozen=True)
class FinancialFact:
    """research.financial_facts 한 행."""

    corp_code: str
    account_code: str
    account_name: str
    period_end: date
    period_type: str
    consolidation_scope: str
    value: Decimal | None
    unit: str
    currency: str | None
    published_at: datetime
    observed_at: datetime
    rcept_no: str
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FinancialResult:
    fetched: int
    skipped: int
    facts: tuple[FinancialFact, ...]
    # DART 응답 자체에 유니크 키 중복이 있다. 값이 같은 것과 다른 것을 구분해서 센다 -
    # 전자는 무해하지만 후자는 어느 값이 맞는지 알 수 없어 조사 대상이다.
    duplicates_same_value: int = 0
    duplicates_conflicting: tuple[str, ...] = ()

    def summary(self) -> str:
        corps = len({f.corp_code for f in self.facts})
        scopes = sorted({f.consolidation_scope for f in self.facts})
        nulls = sum(1 for f in self.facts if f.value is None)
        s = (
            f"재무 {len(self.facts)}건 / 회사 {corps}개 / scope {scopes} "
            f"(값 없음 {nulls}), 제외 {self.skipped} (수신 {self.fetched})"
        )
        if self.duplicates_same_value:
            s += f", 중복(값 동일) {self.duplicates_same_value}"
        if self.duplicates_conflicting:
            s += f", ⚠ 중복(값 상충) {len(self.duplicates_conflicting)}"
        return s


def parse_amount(raw) -> Decimal | None:
    """DART 금액 문자열을 Decimal 로. 빈 값은 None, 형식 오류는 예외다.

    `247,684,612,000,000` 처럼 콤마가 들어온다. 음수는 `-` 로 온다.
    0 으로 떨어뜨리지 않는 이유 - 해당 계정이 없는 것과 값이 0 인 것은 다르다.
    """
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if not text or text == "-":
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f"금액을 숫자로 읽을 수 없다: {raw!r}") from None


def parse_period_end(raw: str) -> date:
    """`2025.12.31 현재` / `2025.01.01 ~ 2025.12.31` 에서 기간 종료일을 뽑는다."""
    text = (raw or "").strip()
    dates = re.findall(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if not dates:
        raise ValueError(f"기간에서 날짜를 찾을 수 없다: {raw!r}")
    y, m, d = dates[-1]  # 범위면 종료일이 마지막이다
    return date(int(y), int(m), int(d))


def published_at_from_rcept(rcept_no: str) -> datetime:
    """접수번호 앞 8자리가 접수일이다. 시각은 없으므로 DAY 정밀도다."""
    text = (rcept_no or "").strip()
    if len(text) < 8 or not text[:8].isdigit():
        raise ValueError(f"rcept_no 에서 접수일을 읽을 수 없다: {rcept_no!r}")
    d = date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
    return datetime.combine(d, dtime(0, 0), tzinfo=KST)


def to_fact(row: dict, *, observed_at: datetime) -> FinancialFact:
    corp_code = str(row.get("corp_code", "")).strip()
    if not corp_code:
        raise ValueError("corp_code 가 없다")
    account_nm = re.sub(r"\s+", " ", str(row.get("account_nm", "")).strip())
    if not account_nm:
        raise ValueError(f"{corp_code}: account_nm 이 없다")

    sj_div = str(row.get("sj_div", "")).strip()
    fs_div = str(row.get("fs_div", "")).strip()
    scope = FS_DIV_TO_SCOPE.get(fs_div)
    if scope is None:
        raise ValueError(f"{corp_code}: 알 수 없는 fs_div={fs_div!r}")

    reprt = str(row.get("reprt_code", "")).strip()
    period_type = REPRT_TO_PERIOD.get(reprt)
    if period_type is None:
        raise ValueError(f"{corp_code}: 알 수 없는 reprt_code={reprt!r}")

    rcept_no = str(row.get("rcept_no", "")).strip()
    return FinancialFact(
        corp_code=corp_code,
        # 표준코드가 아님을 이름과 metadata 로 함께 명시한다
        account_code=f"{sj_div}:{account_nm}",
        account_name=account_nm,
        period_end=parse_period_end(row.get("thstrm_dt", "")),
        period_type=period_type,
        consolidation_scope=scope,
        value=parse_amount(row.get("thstrm_amount")),
        unit=UNIT_ABSOLUTE,
        currency=str(row.get("currency", "")).strip() or None,
        published_at=published_at_from_rcept(rcept_no),
        observed_at=observed_at,
        rcept_no=rcept_no,
        metadata={
            "source": "opendart:2019017",
            "account_code_scheme": ACCOUNT_CODE_SCHEME,
            "account_code_note": (
                "다중회사 주요계정은 표준 계정코드를 주지 않는다. sj_div:account_nm 조합이며 "
                "IFRS 표준코드가 아니다. 표준코드가 필요하면 2019020 또는 2020001 로 보강"
            ),
            "published_at_precision": "DAY",
            "bsns_year": str(row.get("bsns_year", "")).strip(),
            "reprt_code": reprt,
            "fs_div": fs_div,
            "fs_nm": str(row.get("fs_nm", "")).strip(),
            "sj_div": sj_div,
            "sj_nm": str(row.get("sj_nm", "")).strip(),
            "stock_code": str(row.get("stock_code", "")).strip(),
            "thstrm_dt": str(row.get("thstrm_dt", "")).strip(),
            # 전기·전전기는 적재하지 않고 참고로만 남긴다(모듈 docstring 2번 참고)
            "prior_periods": {
                "frmtrm": {"dt": row.get("frmtrm_dt"), "amount": row.get("frmtrm_amount")},
                "bfefrmtrm": {"dt": row.get("bfefrmtrm_dt"), "amount": row.get("bfefrmtrm_amount")},
            },
        },
    )


def prior_fact_from_row(row: dict, current: FinancialFact) -> FinancialFact | None:
    """전기(frmtrm) 값을 별도 행으로 방출한다 - YoY 활성화 (2026-08-01, 결정 2 개정).

    ▶ 왜 이제 안전한가 (원래 결정 2 가 미룬 이유의 해소)
      - 키 충돌 우려: upsert 가 (계정, 기간, scope, revision) 자연키에 DO UPDATE
        라 같은 기간을 두 보고서가 보고해도 **최신 관측으로 수렴**한다. 정정
        재무제표의 전기 재작성값이 오히려 최신 상태이므로 이 방향이 맞다.
      - 범위 한정: **연차보고서(11011)만** 방출한다 - 분기 보고서의 frmtrm 은
        재무상태표(전기말)와 손익(누적/동기)의 의미가 갈려 오적재 위험이 있다.
    published_at 은 이 보고서의 게시일 그대로다 - 전기 수치도 이 보고서로
    관측된 것이므로 PIT 가 정확하다. 금액·일자 결측/파싱 불가면 None(방출 안 함).
    """
    if str(current.metadata.get("reprt_code", "")) != "11011":
        return None
    amt = parse_amount(row.get("frmtrm_amount"))
    dt_raw = str(row.get("frmtrm_dt") or "").strip()
    if amt is None or not dt_raw:
        return None
    try:
        pend = parse_period_end(dt_raw)
    except (ValueError, AttributeError):
        return None
    return FinancialFact(
        corp_code=current.corp_code, account_code=current.account_code,
        account_name=current.account_name, period_end=pend,
        period_type=current.period_type,
        consolidation_scope=current.consolidation_scope,
        value=amt, unit=current.unit, currency=current.currency,
        published_at=current.published_at, observed_at=current.observed_at,
        rcept_no=current.rcept_no,
        metadata={
            "source": current.metadata.get("source"),
            "account_code_scheme": current.metadata.get("account_code_scheme"),
            "published_at_precision": current.metadata.get("published_at_precision"),
            "reprt_code": current.metadata.get("reprt_code"),
            "origin": f"frmtrm of {current.rcept_no}",   # 전기 참고값 출처 표식
            "frmtrm_dt": dt_raw,
        })


def collect_financials(
    client: OpenDartClient,
    corp_codes: list[str],
    *,
    bsns_year: str,
    reprt_code: str,
    observed_at: datetime | None = None,
) -> FinancialResult:
    if reprt_code not in REPRT_TO_PERIOD:
        raise ValueError(f"reprt_code 는 {sorted(REPRT_TO_PERIOD)} 중 하나다: {reprt_code!r}")
    if not re.fullmatch(r"\d{4}", bsns_year):
        raise ValueError(f"bsns_year 는 4자리 연도다: {bsns_year!r}")

    codes = [c.strip() for c in corp_codes if (c or "").strip()]
    if not codes:
        raise ValueError("corp_code 가 비었다")

    obs = observed_at or datetime.now(timezone.utc)
    facts: list[FinancialFact] = []
    fetched = 0
    skipped = 0

    for i in range(0, len(codes), CORP_BATCH):
        batch = codes[i : i + CORP_BATCH]
        try:
            d = client.get(
                ENDPOINT,
                {"corp_code": ",".join(batch), "bsns_year": bsns_year, "reprt_code": reprt_code},
            )
        except OpenDartError:
            # 배치 하나가 실패해도 나머지를 포기하지 않되, 조용히 넘기지 않고 센다.
            skipped += len(batch)
            continue
        rows = d.get("list") or []
        fetched += len(rows)
        for row in rows:
            try:
                cur_fact = to_fact(row, observed_at=obs)
                facts.append(cur_fact)
            except ValueError:
                skipped += 1
                continue
            prior = prior_fact_from_row(row, cur_fact)
            if prior is not None:
                facts.append(prior)

    # DART 응답에 유니크 키 중복이 있다(실측: `IS:당기순이익(손실)` 이 값까지 같은 채로
    # 두 번 온다). ON CONFLICT DO UPDATE 는 같은 명령에서 같은 행을 두 번 건드릴 수 없어
    # 여기서 걸러야 한다. 값이 같으면 정보 손실이 없으므로 첫 건만 쓰고 세기만 하되,
    # 값이 다르면 어느 것이 맞는지 알 수 없으므로 따로 보고한다.
    deduped: dict[tuple, FinancialFact] = {}
    same_value = 0
    conflicting: list[str] = []
    for f in facts:
        k = (f.corp_code, f.account_code, f.period_end, f.period_type, f.consolidation_scope)
        prev = deduped.get(k)
        if prev is None:
            deduped[k] = f
            continue
        if prev.value == f.value:
            same_value += 1
        else:
            conflicting.append(
                f"{f.corp_code} {f.account_code} {f.consolidation_scope} "
                f"{prev.value} != {f.value}"
            )

    return FinancialResult(
        fetched=fetched,
        skipped=skipped,
        facts=tuple(deduped.values()),
        duplicates_same_value=same_value,
        duplicates_conflicting=tuple(conflicting),
    )


# ---------------------------------------------------------------------------
# 자체 점검 - 외부 호출 없음
# ---------------------------------------------------------------------------

_ROW = {
    "corp_code": "00126380", "stock_code": "005930", "rcept_no": "20260310002820",
    "bsns_year": "2025", "reprt_code": "11011", "fs_div": "CFS", "fs_nm": "연결재무제표",
    "sj_div": "BS", "sj_nm": "재무상태표", "account_nm": "유동자산",
    "thstrm_nm": "제 57 기", "thstrm_dt": "2025.12.31 현재",
    "thstrm_amount": "247,684,612,000,000",
    "frmtrm_dt": "2024.12.31 현재", "frmtrm_amount": "227,062,266,000,000",
    "bfefrmtrm_dt": "2023.12.31 현재", "bfefrmtrm_amount": "195,936,557,000,000",
    "currency": "KRW", "ord": "1",
}
_OBS = datetime(2026, 7, 30, 5, 0, tzinfo=timezone.utc)


def _check_amount_parsing():
    assert parse_amount("247,684,612,000,000") == Decimal(247684612000000)
    assert parse_amount("-1,234") == Decimal(-1234)
    assert parse_amount("0") == Decimal(0)
    # 해당 계정 없음과 값 0 을 구분한다
    assert parse_amount("") is None
    assert parse_amount("-") is None
    assert parse_amount(None) is None
    try:
        parse_amount("일억원")
        raise AssertionError("숫자 아닌 금액이 통과했다")
    except ValueError:
        pass
    print("  금액 파싱                   OK")


def _check_period_parsing():
    assert parse_period_end("2025.12.31 현재") == date(2025, 12, 31)
    assert parse_period_end("2025.01.01 ~ 2025.12.31") == date(2025, 12, 31)
    assert parse_period_end("2025-06-30 현재") == date(2025, 6, 30)
    for bad in ("", "제 57 기", "2025년"):
        try:
            parse_period_end(bad)
            raise AssertionError(f"{bad!r} 가 통과했다")
        except ValueError:
            pass

    assert published_at_from_rcept("20260310002820").astimezone(KST).date() == date(2026, 3, 10)
    for bad in ("", "2026", "abcdefgh12"):
        try:
            published_at_from_rcept(bad)
            raise AssertionError(f"{bad!r} 가 통과했다")
        except ValueError:
            pass
    print("  기간·접수일 파싱            OK")


def _check_fact_mapping():
    f = to_fact(_ROW, observed_at=_OBS)
    assert f.corp_code == "00126380"
    assert f.account_code == "BS:유동자산", f.account_code
    assert f.account_name == "유동자산"
    assert f.period_end == date(2025, 12, 31)
    assert f.period_type == "ANNUAL"
    assert f.consolidation_scope == "CONSOLIDATED"
    assert f.value == Decimal(247684612000000)
    assert f.unit == UNIT_ABSOLUTE and f.currency == "KRW"
    assert f.published_at.astimezone(KST).date() == date(2026, 3, 10)
    assert f.observed_at == _OBS

    # 표준코드가 아님이 metadata 에 남아야 한다
    assert f.metadata["account_code_scheme"] == ACCOUNT_CODE_SCHEME
    assert "IFRS 표준코드가 아니다" in f.metadata["account_code_note"]
    assert f.metadata["published_at_precision"] == "DAY"
    # 전기·전전기는 적재 대상이 아니라 참고다
    assert f.metadata["prior_periods"]["frmtrm"]["amount"] == "227,062,266,000,000"

    # 별도재무제표
    sep = to_fact({**_ROW, "fs_div": "OFS"}, observed_at=_OBS)
    assert sep.consolidation_scope == "SEPARATE"
    # 분기
    q1 = to_fact({**_ROW, "reprt_code": "11013"}, observed_at=_OBS)
    assert q1.period_type == "Q1"
    print("  재무 -> financial_facts     OK")


def _check_prior_period_emission():
    """전기(frmtrm) 방출 - 연차 한정·PIT 유지·결측 미방출 (결정 2 개정 검증)."""
    cur = to_fact(_ROW, observed_at=_OBS)
    p = prior_fact_from_row(_ROW, cur)
    assert p is not None and p.period_end == date(2024, 12, 31)
    assert p.value == Decimal(227062266000000)
    assert p.published_at == cur.published_at, "전기도 이 보고서로 관측 - PIT 게시일 유지"
    assert p.account_code == cur.account_code and p.consolidation_scope == cur.consolidation_scope
    assert p.metadata["origin"].startswith("frmtrm of ")
    # 연차(11011)가 아니면 방출하지 않는다 - 분기 frmtrm 은 의미가 갈린다
    q = to_fact({**_ROW, "reprt_code": "11013"}, observed_at=_OBS)
    assert prior_fact_from_row({**_ROW, "reprt_code": "11013"}, q) is None
    # 금액·일자 결측이면 방출하지 않는다 (지어내지 않는다)
    assert prior_fact_from_row({**_ROW, "frmtrm_amount": ""}, cur) is None
    assert prior_fact_from_row({**_ROW, "frmtrm_dt": ""}, cur) is None
    assert prior_fact_from_row({**_ROW, "frmtrm_dt": "이상한 형식"}, cur) is None
    print("  전기 방출 (YoY 활성화)   OK")


def _check_fail_closed():
    for bad, why in (
        ({**_ROW, "corp_code": ""}, "corp_code 없음"),
        ({**_ROW, "account_nm": " "}, "account_nm 없음"),
        ({**_ROW, "fs_div": "ZZZ"}, "알 수 없는 fs_div"),
        ({**_ROW, "reprt_code": "99999"}, "알 수 없는 reprt_code"),
        ({**_ROW, "thstrm_dt": "제 57 기"}, "기간 파싱 실패"),
        ({**_ROW, "rcept_no": ""}, "접수일 없음"),
    ):
        try:
            to_fact(bad, observed_at=_OBS)
            raise AssertionError(f"{why} 가 통과했다")
        except ValueError:
            pass

    class _C:
        def get(self, e, p): return {"status": "000", "list": []}

    for kwargs, why in (
        ({"bsns_year": "25", "reprt_code": "11011"}, "연도 형식"),
        ({"bsns_year": "2025", "reprt_code": "11015"}, "reprt_code"),
    ):
        try:
            collect_financials(_C(), ["00126380"], **kwargs)
            raise AssertionError(f"{why} 가 통과했다")
        except ValueError:
            pass
    try:
        collect_financials(_C(), [], bsns_year="2025", reprt_code="11011")
        raise AssertionError("빈 corp_code 가 통과했다")
    except ValueError:
        pass
    print("  fail-closed                 OK")


def _check_batching_and_error_counting():
    class _Batched:
        def __init__(self): self.batches = []
        def get(self, e, p):
            codes = p["corp_code"].split(",")
            self.batches.append(len(codes))
            if len(self.batches) == 2:
                raise OpenDartError("일시 실패")
            return {"status": "000",
                    "list": [{**_ROW, "corp_code": c} for c in codes]}

    c = _Batched()
    codes = [f"{i:08d}" for i in range(120)]
    r = collect_financials(c, codes, bsns_year="2025", reprt_code="11011")
    assert c.batches == [50, 50, 20], c.batches
    # 2번째 배치가 실패했으므로 50건이 skipped 로 잡혀야 한다
    # 70건의 당기 + 70건의 전기(frmtrm 방출, 2026-08-01 결정 2 개정) = 140
    assert len(r.facts) == 140 and r.skipped == 50, (len(r.facts), r.skipped)
    print("  배치/실패 집계              OK")


def _check_duplicate_handling():
    """DART 가 같은 계정을 두 번 주는 경우. 값이 같으면 세고, 다르면 보고한다."""
    class _Dup:
        def __init__(self, second_amount): self.second = second_amount
        def get(self, e, p):
            return {"status": "000", "list": [
                _ROW,
                {**_ROW, "thstrm_amount": self.second},  # 같은 유니크 키
            ]}

    same = collect_financials(_Dup("247,684,612,000,000"), ["00126380"],
                             bsns_year="2025", reprt_code="11011")
    # 당기 1 + 전기 1 (frmtrm 방출, 결정 2 개정). 당기·전기 각각 중복 1회 흡수
    assert len(same.facts) == 2, "중복이 걸러지지 않았다"
    assert same.duplicates_same_value == 2 and same.duplicates_conflicting == ()

    diff = collect_financials(_Dup("999,999"), ["00126380"],
                              bsns_year="2025", reprt_code="11011")
    assert len(diff.facts) == 2          # 당기 1(상충 보고) + 전기 1(값 동일 흡수)
    assert diff.duplicates_same_value == 1
    assert len(diff.duplicates_conflicting) == 1, "값 상충이 보고되지 않았다"
    assert "!=" in diff.duplicates_conflicting[0]
    assert "중복(값 상충)" in diff.summary()
    print("  중복 구분 처리              OK")


def _collect_and_report(year: str, reprt: str, limit: int) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repository"))
    from reference_repository import SupabaseReferenceRepository

    repo = SupabaseReferenceRepository()
    try:
        with repo._conn.cursor() as cur:
            cur.execute(
                "select corp_code from reference.issuers where corp_code is not null "
                "order by corp_code limit %s", (limit,)
            )
            codes = [r[0] for r in cur.fetchall()]
        print(f"  대상 발행사 {len(codes)}개 (reference.issuers 에서)")

        res = collect_financials(OpenDartClient(), codes, bsns_year=year, reprt_code=reprt)
        print(f"  {res.summary()}")

        _, _, src = repo.sync_data_sources()
        n, u, unlinked = repo.upsert_financial_facts(
            list(res.facts), source_id=src["opendart"]
        )
        print(f"  적재: {n} 신규 / {u} 갱신, issuer 미연결 {unlinked}")
        n2, u2, _ = repo.upsert_financial_facts(list(res.facts), source_id=src["opendart"])
        print(f"  재적재 멱등: {n2} 신규 / {u2} 갱신")

        with repo._conn.cursor() as cur:
            cur.execute(
                "select consolidation_scope, count(*) from research.financial_facts "
                "group by 1 order by 1"
            )
            print("  scope 분포:", dict(cur.fetchall()))
            cur.execute(
                "select account_code, value from research.financial_facts "
                "where account_code like 'BS:%' order by value desc nulls last limit 3"
            )
            for a, v in cur.fetchall():
                print(f"    {a:22} {v}")
        return 0
    finally:
        repo.close()


if __name__ == "__main__":
    if "--collect" in sys.argv:
        a = sys.argv
        def opt(n, d): return a[a.index(n) + 1] if n in a else d
        y, rp, lim = opt("--year", "2025"), opt("--reprt", "11011"), int(opt("--limit", "20"))
        print(f"{COLLECTOR_VERSION} 실제 수집 year={y} reprt={rp} limit={lim}")
        raise SystemExit(_collect_and_report(y, rp, lim))

    print(f"{COLLECTOR_VERSION} 자체 점검 (외부 호출 없음)")
    _check_amount_parsing()
    _check_period_parsing()
    _check_fact_mapping()
    _check_fail_closed()
    _check_batching_and_error_counting()
    _check_duplicate_handling()
    _check_prior_period_emission()
    print("재무 7개 영역 통과. 실제 수집은 --collect")
