#!/usr/bin/env python3
"""P0 기업정보 보강 - DART 기업개황(2019002)으로 업종·결산월·정식명칭을 채운다.

소유: 재일 (리서치본부)
근거: docs/06-integrations/opendart/01-disclosure-information.md (2019002 company.json)
      docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 3.1 P0 "기업정보", 5.1(issuers)
      departments/01-research/collectors/opendart_collector.py (Client 재사용)

▶ 왜 지금까지 비어 있었나
  공시검색(2019001)은 corp_code 와 corp_name 만 준다. upsert_issuers 는 그래서
  industry_code/fiscal_month 를 NULL 로 두고 "없는 값을 기본값으로 채우면 그게
  사실처럼 굳는다" 고 적어뒀다. 기업개황(2019002)이 그 후속이다 - **회사별 1회
  호출** 이므로 instrument 에 연결된 발행사부터 호출 예산을 쓴다.

▶ legal_name 검증
  upsert_issuers 는 공시의 corp_name 을 legal_name 에 임시로 넣고
  metadata.legal_name_verified=false 로 표시했다. 기업개황의 corp_name 이
  "정식명칭" 이므로 이 값으로 갱신하고 verified=true 로 바꾼다.

▶ 저장하지 않는 것 - 응답에 있어도 담지 않는다
  jurir_no(법인등록번호), bizr_no(사업자등록번호), adres(주소), phn_no/fax_no,
  ceo_nm(대표자명). 수집 목적(Universe 기준정보)에 필요 없고, 식별자·연락처는
  들고 있는 것 자체가 부채다. 계약에 자리를 만들지 않는다(가이드 3.3 과 같은 원칙).

▶ 실측으로 확인할 것
  acc_mt 가 "12" 형태(MM)인지, induty_code 형식, est_dt 가 YYYYMMDD 인지.
  형식이 어긋나면 추정하지 않고 None + 카운트다.

자체 점검(호출 없음): python departments/01-research/collectors/opendart_company_collector.py
실제 보강:            python departments/01-research/collectors/opendart_company_collector.py --collect [--limit N]
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "repository"))
from opendart_collector import (
    STATUS_NO_DATA,
    OpenDartClient,
    OpenDartError,
)

COLLECTOR_VERSION = "research-opendart-company-v1"

ENDPOINT = "company.json"

# corp_cls 응답값. 문서: Y(유가), K(코스닥), N(코넥스), E(기타)
CORP_CLS = {"Y": "KOSPI", "K": "KOSDAQ", "N": "KONEX", "E": "ETC"}


class CompanyProfileError(OpenDartError):
    """기업개황 수집 실패. 빈 결과로 바꾸지 않는다."""


@dataclass(frozen=True)
class CompanyProfile:
    """기업개황 한 건. 저장할 것만 담는다 - 계약에 자리가 없으면 새지 않는다."""

    corp_code: str
    legal_name: str          # corp_name 정식명칭
    legal_name_eng: str
    stock_code: str          # 교차검증용. 저장은 metadata 에만
    corp_cls: str | None     # KOSPI/KOSDAQ/KONEX/ETC
    industry_code: str | None
    fiscal_month: int | None
    established_on: date | None
    homepage_url: str
    ir_url: str
    observed_at: datetime
    flags: tuple[str, ...] = ()

    def metadata_patch(self) -> dict:
        """issuers.metadata 에 병합할 조각. 기존 키를 최소로만 덮는다."""
        return {
            "legal_name_verified": True,
            "legal_name_source": "opendart_company_2019002",
            "legal_name_eng": self.legal_name_eng or None,
            "corp_cls": self.corp_cls,
            "established_on": self.established_on.isoformat() if self.established_on else None,
            "homepage_url": self.homepage_url or None,
            "ir_url": self.ir_url or None,
            "industry_code_scheme": "dart_induty_code",
            "profile_observed_at": self.observed_at.isoformat(),
            "profile_flags": list(self.flags),
        }


def _clean(raw: object) -> str:
    s = str(raw or "").strip()
    # DART 는 빈 값을 "-" 로 주기도 한다. 값이 아니다.
    return "" if s in ("-", "N/A") else s


def parse_profile(d: dict, *, corp_code: str, observed_at: datetime) -> CompanyProfile:
    """company.json 응답 하나를 계약으로. 형식이 어긋나면 추정 대신 None + Flag."""
    name = _clean(d.get("corp_name"))
    if not name:
        raise CompanyProfileError(f"{corp_code}: corp_name 이 비었다")

    resp_code = _clean(d.get("corp_code"))
    if resp_code and resp_code != corp_code:
        # 요청과 다른 회사가 오면 그대로 저장하는 순간 발행사 정보가 뒤섞인다.
        raise CompanyProfileError(f"corp_code 불일치: 요청 {corp_code} 응답 {resp_code}")

    flags: list[str] = []

    acc_mt = _clean(d.get("acc_mt"))
    fiscal_month: int | None = None
    if acc_mt:
        if acc_mt.isdigit() and 1 <= int(acc_mt) <= 12:
            fiscal_month = int(acc_mt)
        else:
            flags.append(f"ACC_MT_INVALID:{acc_mt[:8]}")

    est = _clean(d.get("est_dt"))
    established_on: date | None = None
    if est:
        if len(est) == 8 and est.isdigit():
            try:
                established_on = date(int(est[:4]), int(est[4:6]), int(est[6:8]))
            except ValueError:
                flags.append(f"EST_DT_INVALID:{est}")
        else:
            flags.append(f"EST_DT_INVALID:{est[:10]}")

    induty = _clean(d.get("induty_code"))
    if not induty:
        flags.append("INDUTY_MISSING")

    cls_raw = _clean(d.get("corp_cls"))
    corp_cls = CORP_CLS.get(cls_raw)
    if cls_raw and corp_cls is None:
        flags.append(f"CORP_CLS_UNKNOWN:{cls_raw}")

    return CompanyProfile(
        corp_code=corp_code,
        legal_name=name,
        legal_name_eng=_clean(d.get("corp_name_eng")),
        stock_code=_clean(d.get("stock_code")),
        corp_cls=corp_cls,
        industry_code=induty or None,
        fiscal_month=fiscal_month,
        established_on=established_on,
        homepage_url=_clean(d.get("hm_url")),
        ir_url=_clean(d.get("ir_url")),
        observed_at=observed_at,
        flags=tuple(flags),
    )


def fetch_profile(client: OpenDartClient, corp_code: str) -> CompanyProfile | None:
    """한 회사의 개황. status=013(무데이터)이면 None 이고 예외가 아니다 -
    DART 에 개황이 없는 회사(청산 등)는 실제로 있다. 그 외 오류는 그대로 올린다."""
    d = client.get(ENDPOINT, {"corp_code": corp_code})
    if str(d.get("status", "")) == STATUS_NO_DATA:
        return None
    return parse_profile(d, corp_code=corp_code, observed_at=datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# 자체 점검 - 호출 없음
# ---------------------------------------------------------------------------

_SAMPLE = {
    "status": "000", "message": "정상",
    "corp_code": "00126380", "corp_name": "삼성전자(주)", "corp_name_eng": "SAMSUNG ELECTRONICS CO,.LTD",
    "stock_name": "삼성전자", "stock_code": "005930", "ceo_nm": "한종희",
    "corp_cls": "Y", "jurir_no": "1301110006246", "bizr_no": "1248100998",
    "adres": "경기도 수원시 영통구 삼성로 129 (매탄동)",
    "hm_url": "www.samsung.com/sec", "ir_url": "https://www.samsung.com/global/ir",
    "phn_no": "031-200-1114", "fax_no": "031-200-7538",
    "induty_code": "264", "est_dt": "19690113", "acc_mt": "12",
}


def _ob() -> datetime:
    return datetime(2026, 7, 31, 2, 0, tzinfo=timezone.utc)


def _check_parse():
    p = parse_profile(_SAMPLE, corp_code="00126380", observed_at=_ob())
    assert p.legal_name == "삼성전자(주)"
    assert p.industry_code == "264" and p.fiscal_month == 12
    assert p.established_on == date(1969, 1, 13)
    assert p.corp_cls == "KOSPI" and p.stock_code == "005930"
    assert p.ir_url.startswith("https://") and p.flags == ()
    m = p.metadata_patch()
    assert m["legal_name_verified"] is True
    assert m["industry_code_scheme"] == "dart_induty_code"
    print("  파싱                     OK")


def _check_privacy_not_stored():
    """법인·사업자번호, 주소, 연락처, 대표자명이 계약 어디에도 없는지."""
    p = parse_profile(_SAMPLE, corp_code="00126380", observed_at=_ob())
    fields = set(CompanyProfile.__dataclass_fields__)
    for banned in ("jurir_no", "bizr_no", "adres", "phn_no", "fax_no", "ceo_nm", "ceo"):
        assert banned not in fields, f"{banned} 가 계약에 있다"
    blob = str(p.metadata_patch())
    for value in ("1301110006246", "1248100998", "031-200", "수원시", "한종희"):
        assert value not in blob, f"{value} 가 metadata 로 샌다"
    print("  식별자·연락처 미저장     OK")


def _check_invalid_not_guessed():
    ob = _ob()
    # 결산월이 이상하면 None + Flag. 12 로 추정하지 않는다.
    p = parse_profile({**_SAMPLE, "acc_mt": "13"}, corp_code="00126380", observed_at=ob)
    assert p.fiscal_month is None and any("ACC_MT" in f for f in p.flags)
    p = parse_profile({**_SAMPLE, "acc_mt": "-"}, corp_code="00126380", observed_at=ob)
    assert p.fiscal_month is None and not any("ACC_MT" in f for f in p.flags)

    p = parse_profile({**_SAMPLE, "est_dt": "196901"}, corp_code="00126380", observed_at=ob)
    assert p.established_on is None and any("EST_DT" in f for f in p.flags)
    p = parse_profile({**_SAMPLE, "est_dt": "19691350"}, corp_code="00126380", observed_at=ob)
    assert p.established_on is None and any("EST_DT" in f for f in p.flags)

    p = parse_profile({**_SAMPLE, "induty_code": ""}, corp_code="00126380", observed_at=ob)
    assert p.industry_code is None and "INDUTY_MISSING" in p.flags

    p = parse_profile({**_SAMPLE, "corp_cls": "Z"}, corp_code="00126380", observed_at=ob)
    assert p.corp_cls is None and any("CORP_CLS" in f for f in p.flags)

    # 정식명칭이 없으면 그 회사 개황이 아니다
    try:
        parse_profile({**_SAMPLE, "corp_name": " "}, corp_code="00126380", observed_at=ob)
        raise AssertionError("빈 corp_name 이 통과했다")
    except CompanyProfileError:
        pass
    # 다른 회사가 오면 저장하는 순간 발행사 정보가 뒤섞인다
    try:
        parse_profile(_SAMPLE, corp_code="99999999", observed_at=ob)
        raise AssertionError("corp_code 불일치가 통과했다")
    except CompanyProfileError as e:
        assert "불일치" in str(e)
    print("  형식 이상 추정 금지      OK")


def _check_metadata_patch_nulls():
    """빈 문자열이 metadata 에 '' 로 남지 않는지 - 있음/없음이 갈려야 한다."""
    p = parse_profile(
        {**_SAMPLE, "corp_name_eng": "", "hm_url": "-", "ir_url": ""},
        corp_code="00126380", observed_at=_ob(),
    )
    m = p.metadata_patch()
    assert m["legal_name_eng"] is None and m["homepage_url"] is None and m["ir_url"] is None
    print("  metadata NULL 규칙       OK")


# ---------------------------------------------------------------------------
# 실제 보강
# ---------------------------------------------------------------------------

def _collect(limit: int | None) -> int:
    from reference_repository import SupabaseReferenceRepository

    ref = SupabaseReferenceRepository()
    try:
        targets = ref.issuers_needing_profile(limit=limit)
        print(f"  보강 대상 {len(targets)}개 (instrument 연결 우선)")
        if not targets:
            print("  보강할 발행사가 없다 - 이미 다 채워졌다면 정상이다")
            return 0

        client = OpenDartClient()
        profiles, no_data, flagged = [], [], 0
        mismatched_stock = 0
        for i, (_iid, corp_code) in enumerate(targets, 1):
            p = fetch_profile(client, corp_code)
            if p is None:
                no_data.append(corp_code)
                continue
            if p.flags:
                flagged += 1
            profiles.append(p)
            if i % 50 == 0:
                print(f"    {i}/{len(targets)} 조회...")

        print(f"  조회 완료: 개황 {len(profiles)} / 무데이터 {len(no_data)} / Flag {flagged}")
        if no_data:
            print(f"    무데이터 corp_code: {', '.join(no_data[:8])}"
                  + (" ..." if len(no_data) > 8 else ""))

        # 교차검증 - 개황의 stock_code 가 우리 심볼과 일치하는지 표본만 본다
        with ref._conn.cursor() as cur:
            codes = [p.corp_code for p in profiles if p.stock_code]
            cur.execute(
                """select iss.corp_code, s.symbol
                   from reference.issuers iss
                   join reference.instruments i on i.issuer_id = iss.issuer_id
                   join reference.instrument_symbols s using (instrument_id)
                   where iss.corp_code = any(%s)""",
                (codes,),
            )
            ours = {}
            for cc, sym in cur.fetchall():
                ours.setdefault(cc, set()).add(sym)
        for p in profiles:
            if p.stock_code and p.corp_code in ours and p.stock_code not in ours[p.corp_code]:
                mismatched_stock += 1
        if mismatched_stock:
            print(f"  ⚠ stock_code 불일치 {mismatched_stock}건 - 우선주/거래소 차이일 수 있어 조사 대상")

        changed, attempted = ref.apply_company_profiles(profiles)
        print(f"  issuers 반영: 실변경 {changed} / 시도 {attempted}")

        again_changed, _ = ref.apply_company_profiles(profiles)
        print(f"  멱등 재시도: 실변경 {again_changed}")
        if again_changed:
            raise CompanyProfileError("재적용이 변경을 만들었다 - IS DISTINCT FROM 가드 확인")

        with ref._conn.cursor() as cur:
            cur.execute(
                """select count(*), count(industry_code), count(fiscal_month),
                          count(*) filter (where metadata->>'legal_name_verified'='true')
                   from reference.issuers"""
            )
            total, ind, fis, ver = cur.fetchone()
        print(f"  현황: 총 {total} / 업종 {ind} / 결산월 {fis} / 정식명칭 검증 {ver}")
    finally:
        ref.close()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--collect" in sys.argv:
        lim = None
        if "--limit" in sys.argv:
            lim = int(sys.argv[sys.argv.index("--limit") + 1])
        print(f"{COLLECTOR_VERSION} 실제 보강")
        raise SystemExit(_collect(lim))

    print(f"{COLLECTOR_VERSION} 자체 점검 (호출 없음)")
    _check_parse()
    _check_privacy_not_stored()
    _check_invalid_not_guessed()
    _check_metadata_patch_nulls()
    print("기업개황 4개 영역 통과. 실제 보강은 --collect")
