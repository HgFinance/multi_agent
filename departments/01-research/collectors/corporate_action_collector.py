#!/usr/bin/env python3
"""Sprint J2: Corporate Action 수집 (DART 주요사항보고서).

소유: 재일 (리서치본부)
근거: docs/06-integrations/opendart/05-material-events.md
      docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 3.1(Corporate Action), 8.2
      supabase/migrations/20260729000100_foundation_reference.sql (reference.corporate_actions)

▶ 엔드포인트 이름을 실측으로 확인했다 (2026-07-30)
  수집 문서에 API 상세가 없어 이름을 호출로 검증했다. 9개 전부 `000`(데이터) 또는
  `013`(데이터 없음)으로 응답하며 404 가 없다. 아래 ACTION_SPECS 의 endpoint 는 추측이
  아니라 확인된 값이다.

▶ 필드 매핑은 관측한 것만 한다
  자기주식 취득/처분은 실제 응답을 받아 필드를 확인했다. 나머지 엔드포인트는 삼성전자
  구간에 데이터가 없어 필드명을 확인하지 못했다. 그래서 **날짜·비율·금액을 추측해서
  매핑하지 않고 NULL 로 두고 payload 에 원본 전체를 보존한다.** 데이터가 관측되면
  그때 extractor 를 추가한다 - 잘못된 ex_date 는 Backtest 가격 조정을 조용히 망친다.

▶ instrument_id 문제
  corporate_actions.instrument_id 가 NOT NULL FK 인데 주요사항보고서 API 는 corp_code 만
  주고 stock_code 를 주지 않는다. corp_code -> issuer -> instrument 경로를 쓰며,
  **한 발행사가 여러 종목(보통주·우선주)을 가지면 제외한다** - 어느 종목의 Action 인지
  API 가 알려주지 않으므로 임의로 고르면 잘못된 종목에 배당·분할이 붙는다.

자체 점검(호출 없음): python departments/01-research/collectors/corporate_action_collector.py
실제 수집:            python departments/01-research/collectors/corporate_action_collector.py --collect
                      (옵션: --from 20250101 --to 20260730 --limit 60)
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from opendart_collector import KST, OpenDartClient, OpenDartError

COLLECTOR_VERSION = "research-corporate-action-v1"


@dataclass(frozen=True)
class ActionSpec:
    """수집할 Corporate Action 종류 하나. 확장은 이 목록에 한 줄 추가다."""

    endpoint: str
    action_type: str
    label: str
    # 관측으로 확인한 필드만 넣는다. None 이면 매핑하지 않고 payload 에만 남긴다.
    decision_date_field: str | None = None
    period_begin_field: str | None = None
    period_end_field: str | None = None
    shares_field: str | None = None
    amount_field: str | None = None
    fields_verified: bool = False


# endpoint 는 전부 실측 검증됐다. 필드 매핑은 관측된 것만 채웠다.
ACTION_SPECS: tuple[ActionSpec, ...] = (
    ActionSpec(
        "tsstkAqDecsn", "TREASURY_ACQUISITION", "자기주식 취득 결정",
        decision_date_field="aq_dd", period_begin_field="aqexpd_bgd",
        period_end_field="aqexpd_edd", shares_field="aqpln_stk_ostk",
        amount_field="aqpln_prc_ostk", fields_verified=True,
    ),
    ActionSpec(
        "tsstkDpDecsn", "TREASURY_DISPOSAL", "자기주식 처분 결정",
        decision_date_field="dp_dd", period_begin_field="dpprpd_bgd",
        period_end_field="dpprpd_edd", shares_field="dppln_stk_ostk",
        amount_field="dppln_prc_ostk", fields_verified=True,
    ),
    # 아래는 endpoint 만 검증됐고 필드는 미관측이다. payload 에 원본을 보존한다.
    ActionSpec("piicDecsn", "RIGHTS_OFFERING", "유상증자 결정"),
    ActionSpec("fricDecsn", "BONUS_ISSUE", "무상증자 결정"),
    ActionSpec("pifricDecsn", "RIGHTS_AND_BONUS", "유무상증자 결정"),
    ActionSpec("crDecsn", "CAPITAL_REDUCTION", "감자 결정"),
    ActionSpec("cmpMgDecsn", "MERGER", "회사합병 결정"),
    ActionSpec("cmpDvDecsn", "SPINOFF", "회사분할 결정"),
    ActionSpec("stkExtrDecsn", "SHARE_EXCHANGE", "주식교환·이전 결정"),
)


@dataclass(frozen=True)
class CorporateAction:
    """reference.corporate_actions 한 행."""

    corp_code: str
    external_id: str
    action_type: str
    announced_at: datetime | None
    effective_at: datetime | None
    ex_date: date | None
    ratio: Decimal | None
    cash_amount: Decimal | None
    currency: str | None
    status: str
    observed_at: datetime
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ActionResult:
    actions: tuple[CorporateAction, ...]
    skipped: int
    failures: tuple[str, ...] = ()

    def summary(self) -> str:
        by_type: dict[str, int] = {}
        for a in self.actions:
            by_type[a.action_type] = by_type.get(a.action_type, 0) + 1
        unmapped = sum(1 for a in self.actions if not a.payload.get("fields_verified"))
        s = f"Action {len(self.actions)}건 {by_type}, 제외 {self.skipped}"
        if unmapped:
            s += f", 필드 미검증 {unmapped}(payload 보존)"
        if self.failures:
            s += f", ⚠ 실패 {len(self.failures)}"
        return s


def parse_yyyymmdd(raw) -> date | None:
    """DART 날짜. `2026-07-30` / `20260730` / `2026년 07월 30일` 을 받는다."""
    text = str(raw or "").strip()
    if not text or text in ("-", "0"):
        return None
    digits = re.sub(r"\D", "", text)
    if len(digits) != 8:
        return None
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None


def parse_number(raw) -> Decimal | None:
    text = str(raw or "").strip().replace(",", "")
    if not text or text in ("-", ""):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def to_action(spec: ActionSpec, row: dict, *, observed_at: datetime) -> CorporateAction:
    corp_code = str(row.get("corp_code", "")).strip()
    if not corp_code:
        raise ValueError("corp_code 가 없다")
    rcept_no = str(row.get("rcept_no", "")).strip()
    if not rcept_no:
        raise ValueError(f"{corp_code}: rcept_no 가 없다")

    def pick_date(fname):
        return parse_yyyymmdd(row.get(fname)) if fname else None

    decision = pick_date(spec.decision_date_field)
    begin = pick_date(spec.period_begin_field)

    return CorporateAction(
        corp_code=corp_code,
        # 한 공시가 여러 API 에 걸릴 수 있어 endpoint 를 붙여 유일성을 만든다.
        external_id=f"{rcept_no}:{spec.endpoint}",
        action_type=spec.action_type,
        announced_at=(
            datetime.combine(decision, dtime(0, 0), tzinfo=KST) if decision else None
        ),
        # 취득·처분 예상기간 시작일을 effective 로 본다. 확정일이 아니므로 status 는 ANNOUNCED 다.
        effective_at=datetime.combine(begin, dtime(0, 0), tzinfo=KST) if begin else None,
        # ex_date 는 배당·분할에만 의미가 있고 이 API 들은 주지 않는다. 추측하지 않는다.
        ex_date=None,
        ratio=None,
        cash_amount=parse_number(row.get(spec.amount_field)) if spec.amount_field else None,
        currency="KRW" if spec.amount_field else None,
        status="ANNOUNCED",
        observed_at=observed_at,
        payload={
            "source": f"opendart:{spec.endpoint}",
            "action_label": spec.label,
            "rcept_no": rcept_no,
            "fields_verified": spec.fields_verified,
            "field_note": (
                None if spec.fields_verified else
                "이 엔드포인트의 필드명을 실제 응답으로 확인하지 못했다. 날짜·비율을 "
                "추측 매핑하지 않고 raw 에 원본을 보존한다. 데이터 관측 후 extractor 추가"
            ),
            "shares": str(row.get(spec.shares_field, "")).strip() if spec.shares_field else None,
            "corp_name": str(row.get("corp_name", "")).strip(),
            "corp_cls": str(row.get("corp_cls", "")).strip(),
            "raw": row,
        },
    )


# report_nm 에서 어느 Action 인지 알아내는 표. 공시검색으로 후보를 좁히는 데 쓴다.
# 부분 문자열 매칭이며 표기 변형이 있을 수 있어 못 찾으면 그 공시는 건너뛴다(추측 금지).
REPORT_NM_TO_ENDPOINT: tuple[tuple[str, str], ...] = (
    ("자기주식취득신탁계약", ""),        # 신탁계약은 별도 API(2020040/41). 여기선 제외
    ("자기주식취득", "tsstkAqDecsn"),
    ("자기주식처분", "tsstkDpDecsn"),
    ("유무상증자", "pifricDecsn"),
    ("유상증자", "piicDecsn"),
    ("무상증자", "fricDecsn"),
    ("감자", "crDecsn"),
    ("회사분할합병", "cmpDvmgDecsn"),
    ("회사분할", "cmpDvDecsn"),
    ("회사합병", "cmpMgDecsn"),
    ("주식교환", "stkExtrDecsn"),
    ("주식이전", "stkExtrDecsn"),
)


def endpoint_for_report(report_nm: str) -> str | None:
    """공시 제목에서 호출할 엔드포인트를 고른다. 모르면 None 이다."""
    text = re.sub(r"\[[^\]]*\]", "", str(report_nm or "")).replace(" ", "")
    for needle, ep in REPORT_NM_TO_ENDPOINT:
        if needle in text:
            return ep or None
    return None


def discover_targets(
    disclosures: list, *, specs=ACTION_SPECS
) -> dict[str, set[str]]:
    """공시 목록에서 (endpoint -> corp_code 집합)을 만든다.

    왜 필요한가 - 모든 엔드포인트를 모든 회사에 호출하면 9 x N 번이다. 627개 회사면
    5,600 회를 초당 2회 제한으로 돌려 약 47분이 걸린다. 공시검색(pblntf_ty=B 주요사항보고)
    으로 **어느 회사가 무슨 보고서를 냈는지** 먼저 알면 필요한 조합만 호출하면 된다.

    disclosures 는 opendart_collector.DocumentRecord 목록이다.
    """
    known = {s.endpoint for s in specs}
    out: dict[str, set[str]] = {}
    for d in disclosures:
        ep = endpoint_for_report(d.title)
        if ep is None or ep not in known:
            continue
        out.setdefault(ep, set()).add(d.corp_code)
    return out


def discovery_windows(start: date, end: date, *, days: int = 90) -> list[tuple[date, date]]:
    """corp_code 없는 list.json 은 검색기간 3개월 제한 - 90일 창으로 자른다.

    실측 2026-07-31: 19개월 창을 그대로 넘겨 status=100 으로 수집 전체가
    죽어 있었다(CA 4건 정체의 원인). 창 사이 공백·겹침이 없어야 한다.
    """
    if start > end:
        raise ValueError("start 가 end 보다 늦다")
    out: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        w_end = min(cursor + timedelta(days=days - 1), end)
        out.append((cursor, w_end))
        cursor = w_end + timedelta(days=1)
    return out


def collect_actions(
    client: OpenDartClient,
    corp_codes: list[str],
    *,
    start: date,
    end: date,
    specs=ACTION_SPECS,
    observed_at: datetime | None = None,
    targets: dict[str, set[str]] | None = None,
) -> ActionResult:
    if start > end:
        raise ValueError("start 가 end 보다 늦다")
    codes = [c.strip() for c in corp_codes if (c or "").strip()]
    if not codes:
        raise ValueError("corp_code 가 비었다")

    obs = observed_at or datetime.now(timezone.utc)
    actions: list[CorporateAction] = []
    failures: list[str] = []
    skipped = 0

    for spec in specs:
        # targets 가 있으면 그 엔드포인트에 해당하는 회사만 호출한다(호출 수 급감).
        if targets is not None:
            scoped = sorted(targets.get(spec.endpoint, set()) & set(codes))
            if not scoped:
                continue
        else:
            scoped = codes
        for code in scoped:
            try:
                # OpenDartClient.get 은 endpoint 를 URL 에 그대로 붙인다. DART 는 확장자를
                # 요구하며 없으면 status=101 "잘못된 URL" 이 온다(실측에서 450건 전부 실패).
                d = client.get(
                    f"{spec.endpoint}.json",
                    {"corp_code": code, "bgn_de": start.strftime("%Y%m%d"),
                     "end_de": end.strftime("%Y%m%d")},
                )
            except OpenDartError as e:
                failures.append(f"{spec.endpoint}/{code}: {e}")
                continue
            for row in d.get("list") or []:
                try:
                    actions.append(to_action(spec, row, observed_at=obs))
                except ValueError:
                    skipped += 1

    # external_id 중복 제거. 같은 공시가 두 번 오면 첫 건만 쓴다.
    deduped: dict[str, CorporateAction] = {}
    for a in actions:
        if a.external_id in deduped:
            skipped += 1
            continue
        deduped[a.external_id] = a

    return ActionResult(tuple(deduped.values()), skipped, tuple(failures))


# ---------------------------------------------------------------------------
# 자체 점검
# ---------------------------------------------------------------------------

_AQ = {
    "corp_code": "00126380", "corp_name": "삼성전자", "corp_cls": "Y",
    "rcept_no": "20260415000111", "aq_dd": "2026년 04월 15일",
    "aqexpd_bgd": "2026년 04월 16일", "aqexpd_edd": "2026년 07월 15일",
    "aqpln_stk_ostk": "1,000,000", "aqpln_prc_ostk": "70,000,000,000",
}
_OBS = datetime(2026, 7, 30, 5, 0, tzinfo=timezone.utc)
_SPEC_AQ = ACTION_SPECS[0]


def _check_date_number_parsing():
    assert parse_yyyymmdd("2026년 04월 15일") == date(2026, 4, 15)
    assert parse_yyyymmdd("2026-04-15") == date(2026, 4, 15)
    assert parse_yyyymmdd("20260415") == date(2026, 4, 15)
    # 파싱 실패는 예외가 아니라 None 이다 - 한 필드 때문에 Action 을 버리지 않는다
    for bad in ("", "-", "0", "미정", "2026년"):
        assert parse_yyyymmdd(bad) is None, bad
    assert parse_yyyymmdd("20260231") is None, "존재하지 않는 날짜"

    assert parse_number("70,000,000,000") == Decimal(70000000000)
    assert parse_number("-") is None and parse_number("") is None
    assert parse_number("해당없음") is None
    print("  날짜·숫자 파싱              OK")


def _check_verified_mapping():
    a = to_action(_SPEC_AQ, _AQ, observed_at=_OBS)
    assert a.action_type == "TREASURY_ACQUISITION"
    assert a.external_id == "20260415000111:tsstkAqDecsn"
    assert a.announced_at.astimezone(KST).date() == date(2026, 4, 15)
    assert a.effective_at.astimezone(KST).date() == date(2026, 4, 16)
    assert a.cash_amount == Decimal(70000000000) and a.currency == "KRW"
    assert a.status == "ANNOUNCED"
    # ex_date 와 ratio 는 이 API 가 주지 않는다. 추측하지 않는다
    assert a.ex_date is None and a.ratio is None
    assert a.payload["fields_verified"] is True and a.payload["field_note"] is None
    assert a.payload["shares"] == "1,000,000"
    assert a.payload["raw"] == _AQ, "원본이 보존돼야 한다"
    print("  검증된 필드 매핑            OK")


def _check_unverified_kept_raw():
    """필드 미검증 엔드포인트는 날짜를 추측하지 않고 원본만 보존한다."""
    spec = next(s for s in ACTION_SPECS if not s.fields_verified)
    row = {"corp_code": "00126380", "rcept_no": "20260501000222",
           "some_unknown_date": "2026년 05월 01일", "ratio_maybe": "0.05"}
    a = to_action(spec, row, observed_at=_OBS)
    assert a.announced_at is None and a.effective_at is None
    assert a.ratio is None and a.cash_amount is None
    assert a.payload["fields_verified"] is False
    assert "추측 매핑하지 않고" in a.payload["field_note"]
    assert a.payload["raw"]["ratio_maybe"] == "0.05", "원본은 남아야 한다"
    print("  미검증 필드 원본 보존       OK")


def _check_external_id_uniqueness():
    """같은 rcept_no 가 다른 endpoint 로 오면 다른 Action 이다."""
    a = to_action(ACTION_SPECS[0], _AQ, observed_at=_OBS)
    b = to_action(ACTION_SPECS[1], {**_AQ, "dp_dd": "2026년 04월 15일"}, observed_at=_OBS)
    assert a.external_id != b.external_id
    assert a.external_id.endswith(":tsstkAqDecsn")
    assert b.external_id.endswith(":tsstkDpDecsn")
    print("  external_id 유일성          OK")


def _check_fail_closed_and_dedup():
    for bad, why in (({"rcept_no": "1"}, "corp_code 없음"),
                     ({"corp_code": "1"}, "rcept_no 없음")):
        try:
            to_action(_SPEC_AQ, bad, observed_at=_OBS)
            raise AssertionError(f"{why} 가 통과했다")
        except ValueError:
            pass

    class _C:
        def get(self, ep, p):
            return ({"status": "000", "list": [_AQ, _AQ]}
                    if ep == "tsstkAqDecsn.json" else {"list": []})

    r = collect_actions(_C(), ["00126380"], start=date(2026, 1, 1), end=date(2026, 7, 30))
    assert len(r.actions) == 1 and r.skipped == 1, (len(r.actions), r.skipped)

    try:
        collect_actions(_C(), [], start=date(2026, 1, 1), end=date(2026, 7, 30))
        raise AssertionError("빈 corp_code 가 통과했다")
    except ValueError:
        pass
    print("  fail-closed / 중복 제거     OK")


def _check_endpoint_failure_reported():
    class _Fail:
        def get(self, ep, p):
            if ep == "tsstkAqDecsn.json":
                raise OpenDartError("일시 장애")
            return {"status": "013", "list": []}

    r = collect_actions(_Fail(), ["00126380"], start=date(2026, 1, 1), end=date(2026, 7, 30))
    assert len(r.actions) == 0
    assert len(r.failures) == 1 and "tsstkAqDecsn" in r.failures[0]
    assert "⚠ 실패" in r.summary()
    print("  엔드포인트 실패 보고        OK")


def _check_discovery_windows():
    """3개월 제한 대응 - 창이 공백·겹침 없이 전 구간을 덮는지."""
    ws = discovery_windows(date(2025, 1, 1), date(2026, 7, 31))
    assert ws[0][0] == date(2025, 1, 1) and ws[-1][1] == date(2026, 7, 31)
    for (s1, e1), (s2, _e2) in zip(ws, ws[1:]):
        assert (s2 - e1).days == 1, f"공백/겹침: {e1} -> {s2}"
        assert (e1 - s1).days <= 89
    # 한 창이면 그대로
    assert discovery_windows(date(2026, 7, 1), date(2026, 7, 31)) == [
        (date(2026, 7, 1), date(2026, 7, 31))]
    # 역순은 거부
    try:
        discovery_windows(date(2026, 2, 1), date(2026, 1, 1))
        raise AssertionError("역순 기간이 통과했다")
    except ValueError:
        pass
    print("  90일 창 분할 (3개월 제한)   OK")


def _check_target_discovery():
    """공시 제목으로 호출 대상을 좁힌다. 모르는 제목은 추측하지 않는다."""
    assert endpoint_for_report("주요사항보고서(자기주식취득결정)") == "tsstkAqDecsn"
    assert endpoint_for_report("[기재정정]주요사항보고서(유상증자결정)") == "piicDecsn"
    assert endpoint_for_report("주요사항보고서(무상증자결정)") == "fricDecsn"
    assert endpoint_for_report("주요사항보고서(유무상증자결정)") == "pifricDecsn"
    assert endpoint_for_report("주요사항보고서(회사분할결정)") == "cmpDvDecsn"
    # 신탁계약은 별도 API 라 제외한다
    assert endpoint_for_report("주요사항보고서(자기주식취득신탁계약체결결정)") is None
    # 모르는 제목은 None - 비슷한 엔드포인트로 추측하지 않는다
    assert endpoint_for_report("타인에대한채무보증결정") is None
    assert endpoint_for_report("") is None

    class _D:
        def __init__(self, corp, title): self.corp_code = corp; self.title = title

    targets = discover_targets([
        _D("00126380", "주요사항보고서(자기주식취득결정)"),
        _D("00164779", "주요사항보고서(자기주식취득결정)"),
        _D("00126380", "주요사항보고서(유상증자결정)"),
        _D("00999999", "최대주주등소유주식변동신고서"),
    ])
    assert targets["tsstkAqDecsn"] == {"00126380", "00164779"}
    assert targets["piicDecsn"] == {"00126380"}
    assert "00999999" not in {c for v in targets.values() for c in v}

    # targets 를 주면 해당 조합만 호출한다
    calls = []

    class _C:
        def get(self, ep, p):
            calls.append((ep, p["corp_code"]))
            return {"status": "013", "list": []}

    collect_actions(_C(), ["00126380", "00164779"], start=date(2026, 1, 1),
                    end=date(2026, 7, 30), targets=targets)
    # 확장자 .json 이 반드시 붙어야 한다 - 없으면 DART 가 status=101 을 준다
    assert all(ep.endswith(".json") for ep, _ in calls), calls
    assert sorted(calls) == [("piicDecsn.json", "00126380"),
                             ("tsstkAqDecsn.json", "00126380"),
                             ("tsstkAqDecsn.json", "00164779")], calls
    print("  대상 축약 (호출 수 절감)    OK")


def _collect_and_report(start: date, end: date, limit: int) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repository"))
    from reference_repository import SupabaseReferenceRepository

    repo = SupabaseReferenceRepository()
    try:
        # issuer_id 가 연결된 단일 종목 발행사만 대상으로 한다
        with repo._conn.cursor() as cur:
            cur.execute(
                """
                select s.corp_code from reference.issuers s
                join reference.instruments i on i.issuer_id = s.issuer_id
                where i.instrument_type = 'STOCK' and i.status = 'ACTIVE'
                group by s.corp_code having count(*) = 1
                order by s.corp_code limit %s
                """,
                (limit,),
            )
            codes = [r[0] for r in cur.fetchall()]
        print(f"  대상 발행사 {len(codes)}개 (issuer 연결 + 단일 종목)")

        # 주요사항보고를 먼저 훑어 호출 대상을 좁힌다. corp_code 없는 list.json
        # 은 검색기간이 3개월 제한이라(collect_disclosures docstring + 실측
        # 2026-07-31 status=100 "3개월만 가능") 기간을 90일 창으로 잘라 잇는다.
        # 이 제한을 무시한 것이 CA 가 4건에서 멈춰 있던 원인이었다.
        from opendart_collector import collect_disclosures
        client = OpenDartClient()
        disc_records: list = []
        for w_start, w_end in discovery_windows(start, end):
            disc = collect_disclosures(client, start=w_start, end=w_end, max_pages=10)
            disc_records.extend(disc.records)
        targets = discover_targets(disc_records)
        n_calls = sum(len(v & set(codes)) for v in targets.values())
        print(f"  공시 {len(disc_records)}건에서 대상 축약: "
              f"{ {k: len(v & set(codes)) for k, v in targets.items() if v & set(codes)} } "
              f"= {n_calls}회 호출 (전수는 {len(ACTION_SPECS) * len(codes)}회)")

        res = collect_actions(client, codes, start=start, end=end, targets=targets)
        print(f"  {res.summary()}")
        for f in res.failures[:3]:
            print(f"    ⚠ {f}")

        _, _, src = repo.sync_data_sources()
        n, u, unmapped = repo.upsert_corporate_actions(
            list(res.actions), source_id=src["opendart"]
        )
        print(f"  적재: {n} 신규 / {u} 갱신, instrument 미매핑 {unmapped}")
        n2, u2, _ = repo.upsert_corporate_actions(list(res.actions), source_id=src["opendart"])
        print(f"  재적재 멱등: {n2} 신규 / {u2} 갱신")

        with repo._conn.cursor() as cur:
            cur.execute(
                "select action_type, count(*) from reference.corporate_actions "
                "group by 1 order by 2 desc"
            )
            for t, c in cur.fetchall():
                print(f"    {t:24} {c}")
        return 0
    finally:
        repo.close()


if __name__ == "__main__":
    if "--collect" in sys.argv:
        a = sys.argv
        def opt(n, d): return a[a.index(n) + 1] if n in a else d
        s = parse_yyyymmdd(opt("--from", "20250101"))
        e = parse_yyyymmdd(opt("--to", datetime.now(KST).strftime("%Y%m%d")))
        lim = int(opt("--limit", "40"))
        print(f"{COLLECTOR_VERSION} 실제 수집 {s} ~ {e} limit={lim}")
        raise SystemExit(_collect_and_report(s, e, lim))

    print(f"{COLLECTOR_VERSION} 자체 점검 (외부 호출 없음)")
    _check_date_number_parsing()
    _check_verified_mapping()
    _check_unverified_kept_raw()
    _check_external_id_uniqueness()
    _check_fail_closed_and_dedup()
    _check_endpoint_failure_reported()
    _check_discovery_windows()
    _check_target_discovery()
    print("Corporate Action 8개 영역 통과. 실제 수집은 --collect")
