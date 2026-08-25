"""지분공시 매집 신호 - 한국판 13F. 순수 함수, I/O 없음.

## 왜 이걸 만드나

우리 백테스트는 두 번 다 엣지를 못 찾았다(모멘텀 초과 +0.07%/t=0.12, 반전은
IC 재현되나 손익 0). 그래서 종합점수는 계속 `[미검증]` 이다. **검증 못 한
예측을 파는 대신 관측된 사실을 보고한다** - "국민연금이 지분을 3.2%p 늘렸다"는
예측이 아니라 공시에 적힌 사실이고, 확인 가능한 좌표(rcept_no)가 붙는다.

## 소스

DART `list.json?pblntf_ty=D` 가 **시장 전체 지분공시**를 한 번에 준다
(실측 2026-08-25: 2주간 1,058건). 상세는 두 갈래다.

- `majorstock.json` - 주식등의 대량보유상황보고서(**5% 룰**). 외부 투자자가
  5% 이상 보유하거나 1%p 이상 변동하면 5영업일 내 보고. 13F 에 가장 가깝다.
- `elestock.json` - 임원·주요주주 특정증권등 소유상황보고서(내부자).

## 증가를 다 매집으로 세지 않는다

`stkrt_irds`(비율 증감)가 양수여도 이유가 다르다. 실측에서 나온 것만 봐도
"전환사채권 인수", "신규 상장, 무상 증자", "장내 매도"가 섞여 있었다.
**전환사채 인수로 지분이 38%p 늘어난 것은 시장에서 사 모은 게 아니다.**
그래서 사유를 분류해서 **드러내고**, 조용히 버리지 않는다.

  MARKET_BUY  장내·시간외 매수 - 돈을 주고 시장에서 샀다. 가장 강한 관측.
  STRUCTURAL  전환사채·신주인수·무상증자·상장·상속·스톡옵션 - 지분은 늘었으나
              시장에서 산 것이 아니다.
  DISPOSAL    매도·감소.
  UNCLASSIFIED 사유 문구를 못 읽었다. 0 으로 세지 않고 그대로 표시한다.

## 이건 후행 지표다

5% 룰은 **5영업일 내** 보고다. 공시를 볼 때는 이미 산 뒤다. 그리고 "기관이
샀다"가 "오른다"는 아니다 - 그건 측정한 적이 없다. 산출물은 `[관측됨]` 으로만
나가고 예측으로 포장하지 않는다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

MARKET_BUY = "MARKET_BUY"
STRUCTURAL = "STRUCTURAL"
DISPOSAL = "DISPOSAL"
UNCLASSIFIED = "UNCLASSIFIED"

# 사유 문구 -> 분류. 순서가 의미 있다 - 매도 표현을 먼저 잡아야
# "장내 매수 및 장내 매도" 같은 혼합 문구가 매집으로 새지 않는다.
_REASON_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (DISPOSAL, ("매도", "처분", "감소", "장외매도")),
    (STRUCTURAL, ("전환사채", "신주인수", "무상", "유상", "상장", "합병", "분할",
                  "상속", "증여", "스톡옵션", "주식매수선택권", "담보", "대차",
                  "출자", "현물", "배정", "전환청구", "행사")),
    (MARKET_BUY, ("장내매수", "장내 매수", "시간외매수", "시간외 매수",
                  "장내매매", "매수")),
)


def classify_reason(reason: str) -> str:
    """공시 사유 문구를 분류한다. 못 읽으면 UNCLASSIFIED - 0 으로 세지 않는다."""
    text = re.sub(r"\s+", "", str(reason or ""))
    if not text:
        return UNCLASSIFIED
    for label, needles in _REASON_RULES:
        if any(re.sub(r"\s+", "", n) in text for n in needles):
            return label
    return UNCLASSIFIED


def normalize_date(value: Any) -> str:
    """rcept_dt 를 YYYYMMDD 로 통일한다.

    **목록 API 와 상세 API 의 형식이 다르다** - `list.json` 은 `20260825`,
    `majorstock.json` 은 `2024-10-04` 를 준다(2026-08-25 실측). 문자열로 그냥
    비교하면 기간 필터가 전부 거짓이 되어 파싱 결과가 0건이 된다.
    """
    text = re.sub(r"[^0-9]", "", str(value or ""))
    return text[:8] if len(text) >= 8 else ""


def _num(value: Any) -> float | None:
    """DART 숫자 필드. 콤마·부호·공백이 섞이고 '-' 하나만 오기도 한다."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("%", "")
    if text in ("", "-", "--"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


# ── 매수자 유형 ──────────────────────────────────────────────────────────
INSTITUTION = "INSTITUTION"    # 외부 운용사·펀드·연기금
CONTROLLING = "CONTROLLING"    # 지배주주·지주회사·계열사
INSIDER = "INSIDER"            # 임원·개인 주요주주
BUYER_UNKNOWN = "UNKNOWN"

_INSTITUTION_WORDS = (
    "자산운용", "투자자문", "인베스트먼트", "캐피탈", "연금", "공제회", "운용",
    "펀드", "신탁", "증권", "은행", "보험", "asset", "capital", "management",
    "investment", "partners", "fund", "advisors", "securities", "llc", "ltd",
    "inc", "l.p", "lp",
)
_CONTROLLING_WORDS = ("홀딩스", "지주", "그룹", "holdings", "group")


def _tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^0-9A-Za-z가-힣]+", str(name or "")) if len(t) > 1}


def _shares_stem(buyer: str, company: str) -> bool:
    """상호가 회사명과 어간을 공유하는가.

    "다산인베스트 <-> 다산솔루에타", "평화홀딩스 <-> 평화산업" 처럼 계열은
    앞머리를 공유한다. 토큰이 통째로 같지 않아도 앞 2글자가 겹치면 본다 -
    한국 계열사 작명이 대체로 그렇다.
    """
    b, c = re.sub(r"\s+", "", str(buyer or "")), re.sub(r"\s+", "", str(company or ""))
    if not b or not c:
        return False
    if _tokens(b) & _tokens(c):
        return True
    return len(b) >= 2 and len(c) >= 2 and b[:2] == c[:2]


def classify_buyer(holder: str, company: str = "", source: str = "") -> str:
    """보고자 이름으로 유형을 짐작한다. 확신 없으면 UNKNOWN 이다."""
    name = str(holder or "").strip()
    if not name:
        return BUYER_UNKNOWN
    low = name.lower()
    if any(w in low for w in _CONTROLLING_WORDS) or _shares_stem(name, company):
        return CONTROLLING
    if any(w in low for w in _INSTITUTION_WORDS):
        return INSTITUTION
    if source == "elestock":
        return INSIDER
    # 법인 접미가 없고 3~4글자 한글이면 개인으로 본다(대주주 개인 보고).
    if re.fullmatch(r"[가-힣]{2,4}", name):
        return INSIDER
    return BUYER_UNKNOWN


@dataclass(frozen=True)
class Filing:
    """지분공시 한 건. 원문 좌표(rcept_no)를 반드시 들고 다닌다."""

    symbol: str
    company: str
    holder: str
    filed_at: str          # rcept_dt YYYYMMDD
    rcept_no: str
    source: str            # majorstock | elestock
    ratio_after: float | None
    ratio_change: float | None
    qty_change: float | None
    reason: str
    reason_class: str = ""
    buyer_type: str = ""

    def __post_init__(self) -> None:
        if not self.rcept_no:
            # 좌표 없는 사실 주장은 확인할 방법이 없다.
            raise ValueError(f"rcept_no 없는 공시: {self.company} {self.holder}")
        object.__setattr__(self, "reason_class",
                           self.reason_class or classify_reason(self.reason))
        object.__setattr__(self, "buyer_type",
                           self.buyer_type or classify_buyer(
                               self.holder, self.company, self.source))


# 두 API 의 **필드 이름이 완전히 다르다**(2026-08-25 실측). 하나로 읽으면
# elestock 행이 전부 None 이 되어 "? (보유 None%)" 로 나간다.
_FIELD_MAP: dict[str, dict[str, str]] = {
    "majorstock": {
        "ratio_after": "stkrt", "ratio_change": "stkrt_irds",
        "qty_change": "stkqy_irds", "reason": "report_resn",
    },
    # 임원·주요주주 보고에는 **사유 필드가 없다.** 지분이 늘어도 장내매수인지
    # 주식보상인지 알 수 없으므로 매집으로 세지 않는다(UNCLASSIFIED 로 남아
    # net_ratio 에 안 들어간다). 근거 목록에는 맥락으로 싣는다.
    "elestock": {
        "ratio_after": "sp_stock_lmp_rate", "ratio_change": "sp_stock_lmp_irds_rate",
        "qty_change": "sp_stock_lmp_irds_cnt", "reason": "",
    },
}


def parse_filings(rows: Sequence[Mapping[str, Any]], *, source: str,
                  symbol_of: Mapping[str, str] | None = None) -> list[Filing]:
    """majorstock/elestock 응답을 Filing 으로. 좌표 없는 행은 버리고 센다."""
    fields = _FIELD_MAP.get(source, _FIELD_MAP["majorstock"])
    out: list[Filing] = []
    for r in rows:
        rcept = str(r.get("rcept_no") or "").strip()
        if not rcept:
            continue
        corp_code = str(r.get("corp_code") or "").strip()
        symbol = str(r.get("stock_code") or "").strip()
        if not symbol and symbol_of:
            symbol = str(symbol_of.get(corp_code, "")).strip()
        reason_key = fields["reason"]
        reason = str(r.get(reason_key) or "").strip() if reason_key else ""
        if not reason and source == "elestock":
            # 사유 대신 직위·구분을 남긴다 - 누가 샀는지가 맥락이다.
            role = " ".join(x for x in (r.get("isu_exctv_ofcps"),
                                        r.get("isu_exctv_rgist_at")) if x and x != "-")
            reason = f"임원·주요주주 보고({role})" if role else "임원·주요주주 보고"
        out.append(Filing(
            symbol=symbol,
            company=str(r.get("corp_name") or "").strip(),
            holder=str(r.get("repror") or "").strip(),
            filed_at=normalize_date(r.get("rcept_dt")),
            rcept_no=rcept,
            source=source,
            ratio_after=_num(r.get(fields["ratio_after"])),
            ratio_change=_num(r.get(fields["ratio_change"])),
            qty_change=_num(r.get(fields["qty_change"])),
            reason=reason,
        ))
    return out


@dataclass
class Accumulation:
    """한 종목의 관측된 매집. **점수가 아니라 집계다.**"""

    symbol: str
    company: str
    market_buy_ratio: float = 0.0     # 장내매수로 늘어난 지분비율 합(%p)
    structural_ratio: float = 0.0
    disposal_ratio: float = 0.0
    unclassified_ratio: float = 0.0
    # 유형별 장내 순증(%p). "누가 샀나"가 "얼마나"만큼 중요하다.
    by_buyer_type: dict[str, float] = field(default_factory=dict)
    buyers: list[str] = field(default_factory=list)
    filings: list[Filing] = field(default_factory=list)

    @property
    def net_ratio(self) -> float:
        """장내 순증(%p). 구조적 증가는 빼고 센다 - 시장에서 산 것만."""
        return round(self.market_buy_ratio - self.disposal_ratio, 4)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "company": self.company,
            "net_market_buy_ratio_pp": self.net_ratio,
            "market_buy_ratio_pp": round(self.market_buy_ratio, 4),
            "structural_ratio_pp": round(self.structural_ratio, 4),
            "disposal_ratio_pp": round(self.disposal_ratio, 4),
            "unclassified_ratio_pp": round(self.unclassified_ratio, 4),
            "buyer_count": len(set(self.buyers)),
            "by_buyer_type_pp": {k: round(v, 4)
                                 for k, v in sorted(self.by_buyer_type.items())},
            "institution_ratio_pp": round(self.by_buyer_type.get(INSTITUTION, 0.0), 4),
            "buyers": sorted(set(self.buyers))[:6],
            "filing_count": len(self.filings),
            "evidence": [
                {"rcept_no": f.rcept_no, "filed_at": f.filed_at,
                 "holder": f.holder, "buyer_type": f.buyer_type,
                 "ratio_change_pp": f.ratio_change,
                 "ratio_after_pct": f.ratio_after,
                 "reason": f.reason[:60], "reason_class": f.reason_class,
                 "source": f.source}
                for f in sorted(self.filings, key=lambda x: x.filed_at, reverse=True)[:5]
            ],
        }


def aggregate(filings: Sequence[Filing]) -> list[Accumulation]:
    """종목별로 접는다. 정렬은 호출부가 한다 - 여기서 순위를 정하지 않는다."""
    by_symbol: dict[str, Accumulation] = {}
    for f in filings:
        if not f.symbol:
            continue  # 상장 종목코드가 없으면 추천 대상이 아니다(비상장·SPAC 등)
        acc = by_symbol.setdefault(
            f.symbol, Accumulation(symbol=f.symbol, company=f.company))
        acc.filings.append(f)
        change = f.ratio_change
        if change is None:
            continue
        magnitude = abs(change)
        if f.reason_class == MARKET_BUY and change > 0:
            acc.market_buy_ratio += magnitude
            acc.by_buyer_type[f.buyer_type] = (
                acc.by_buyer_type.get(f.buyer_type, 0.0) + magnitude)
            if f.holder:
                acc.buyers.append(f.holder)
        elif f.reason_class == STRUCTURAL and change > 0:
            acc.structural_ratio += magnitude
        elif f.reason_class == DISPOSAL or change < 0:
            acc.disposal_ratio += magnitude
        else:
            acc.unclassified_ratio += magnitude
    return list(by_symbol.values())


def rank_by_observation(accs: Sequence[Accumulation], *,
                        min_net_ratio: float = 0.5) -> list[Accumulation]:
    """관측된 장내 순증이 큰 순. **예측 점수가 아니다.**

    min_net_ratio 는 노이즈 컷이다(기본 0.5%p) - 5% 룰 변동보고 기준이 1%p 라
    그보다 낮은 변동은 대개 단순 정정이거나 계산 반올림이다.
    """
    return sorted(
        [a for a in accs if a.net_ratio >= min_net_ratio],
        key=lambda a: (-a.net_ratio, -len(set(a.buyers)), a.symbol),
    )


@dataclass
class HolderBook:
    """한 매수자가 조회 구간에 사 모은 종목들. 13F 의 한 줄에 해당한다."""

    holder: str
    buyer_type: str
    positions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_ratio(self) -> float:
        return round(sum(p["ratio_change_pp"] for p in self.positions), 4)

    def as_dict(self) -> dict[str, Any]:
        return {
            "holder": self.holder,
            "buyer_type": self.buyer_type,
            "position_count": len(self.positions),
            "total_ratio_pp": self.total_ratio,
            "positions": sorted(self.positions,
                                key=lambda x: -x["ratio_change_pp"]),
        }


def by_holder(filings: Sequence[Filing], *, only_market_buy: bool = True
              ) -> list[HolderBook]:
    """매수자별로 접는다. 여러 종목을 담은 쪽이 위로 온다.

    `only_market_buy` 가 참이면 장내매수만 센다 - 전환사채 인수나 상장으로
    생긴 지분은 "고른 것"이 아니라 "생긴 것"이라 포트폴리오로 읽으면 안 된다.
    """
    books: dict[str, HolderBook] = {}
    for f in filings:
        if not f.holder or not f.symbol or f.ratio_change is None:
            continue
        if only_market_buy and (f.reason_class != MARKET_BUY or f.ratio_change <= 0):
            continue
        book = books.setdefault(
            f.holder, HolderBook(holder=f.holder, buyer_type=f.buyer_type))
        # ▶ **종목 단위로 접는다.** 한 종목에 공시가 여러 건이면 그건 한
        #   포지션을 나눠 산 것이지 여러 종목이 아니다(실측 2026-08-25:
        #   정해운이 "3종목"으로 떴는데 닷밀 공시 3건이었다). 이 뷰의 요점이
        #   "여러 곳을 담았나"라서 부풀면 뷰 자체가 거짓말이 된다.
        pos = next((q for q in book.positions if q["symbol"] == f.symbol), None)
        if pos is None:
            book.positions.append({
                "symbol": f.symbol, "company": f.company,
                "ratio_change_pp": abs(f.ratio_change),
                "ratio_after_pct": f.ratio_after,
                "filed_at": f.filed_at, "rcept_no": f.rcept_no,
                "filing_count": 1,
            })
        else:
            pos["ratio_change_pp"] = round(
                pos["ratio_change_pp"] + abs(f.ratio_change), 4)
            pos["filing_count"] += 1
            if f.filed_at > pos["filed_at"]:      # 최신 공시를 대표로
                pos.update(filed_at=f.filed_at, rcept_no=f.rcept_no,
                           ratio_after_pct=f.ratio_after)
    # 종목 수가 먼저다 - 여러 곳을 담은 쪽이 포트폴리오를 짜고 있다는 뜻이다.
    return sorted(books.values(),
                  key=lambda b: (-len(b.positions), -b.total_ratio, b.holder))


def rank_by_buyer_type(accs: Sequence[Accumulation], buyer_type: str, *,
                       min_ratio: float = 0.3) -> list[Accumulation]:
    """특정 유형의 매수만으로 줄을 세운다.

    지배주주 거래와 외부 기관 거래를 한 줄에 세우면 늘 전자가 이긴다 -
    지분을 크게 움직이는 쪽이라서지 신호가 강해서가 아니다. 문턱을 낮게
    잡는 이유도 같다(기관은 1%p 를 잘 안 넘긴다).
    """
    return sorted(
        [a for a in accs if a.by_buyer_type.get(buyer_type, 0.0) >= min_ratio],
        key=lambda a: (-a.by_buyer_type.get(buyer_type, 0.0), a.symbol),
    )


# ── 자체 점검 ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # 사유 분류 - 실측에서 나온 문구 그대로
    assert classify_reason("장내매수에 따른 보유 비율 증가") == MARKET_BUY
    assert classify_reason("전환사채권 인수로 인한 특별관계자 추가") == STRUCTURAL
    assert classify_reason("신규 상장, 무상 증자, 장내") == STRUCTURAL
    assert classify_reason("장내 매도에 따른 보유 비율 감소") == DISPOSAL
    # 혼합 문구는 매도가 이긴다 - 매집으로 새면 안 된다
    assert classify_reason("장내 매수 및 장내 매도") == DISPOSAL
    assert classify_reason("") == UNCLASSIFIED
    assert classify_reason("기타") == UNCLASSIFIED

    # 좌표 없는 사실 주장은 만들 수 없다
    try:
        Filing(symbol="005930", company="X", holder="h", filed_at="20260825",
               rcept_no="", source="majorstock", ratio_after=1.0,
               ratio_change=1.0, qty_change=1.0, reason="장내매수")
        raise AssertionError("rcept_no 없는 공시를 통과시켰다")
    except ValueError:
        pass

    rows = [
        {"rcept_no": "1", "corp_name": "가", "stock_code": "000001", "repror": "국민연금",
         "rcept_dt": "20260825", "stkrt": "8.10", "stkrt_irds": "1.20",
         "stkqy_irds": "100000", "report_resn": "장내매수"},
        {"rcept_no": "2", "corp_name": "가", "stock_code": "000001", "repror": "블랙록",
         "rcept_dt": "20260824", "stkrt": "5.40", "stkrt_irds": "0.90",
         "stkqy_irds": "80000", "report_resn": "장내매수"},
        {"rcept_no": "3", "corp_name": "나", "stock_code": "000002", "repror": "대주주",
         "rcept_dt": "20260825", "stkrt": "55.30", "stkrt_irds": "38.51",
         "stkqy_irds": "900000", "report_resn": "전환사채권 인수"},
        {"rcept_no": "4", "corp_name": "다", "stock_code": "000003", "repror": "개인",
         "rcept_dt": "20260825", "stkrt": "3.50", "stkrt_irds": "-1.84",
         "stkqy_irds": "-50000", "report_resn": "장내 매도"},
        {"rcept_no": "5", "corp_name": "라", "stock_code": "", "repror": "비상장",
         "rcept_dt": "20260825", "stkrt": "9.9", "stkrt_irds": "2.0",
         "stkqy_irds": "1", "report_resn": "장내매수"},
    ]
    fs = parse_filings(rows, source="majorstock")
    assert len(fs) == 5
    accs = {a.symbol: a for a in aggregate(fs)}
    # 종목코드 없는 건은 대상에서 빠진다
    assert set(accs) == {"000001", "000002", "000003"}, sorted(accs)

    # 장내매수 두 건이 합산되고 매수자가 둘로 센다
    a1 = accs["000001"]
    assert abs(a1.market_buy_ratio - 2.10) < 1e-9, a1.market_buy_ratio
    assert a1.net_ratio == 2.1 and len(set(a1.buyers)) == 2

    # 전환사채는 매집이 아니다 - 구조적 증가로만 잡힌다
    a2 = accs["000002"]
    assert a2.market_buy_ratio == 0.0 and a2.structural_ratio == 38.51, a2
    assert a2.net_ratio == 0.0

    # 매도는 음수로 반영
    a3 = accs["000003"]
    assert a3.disposal_ratio == 1.84 and a3.net_ratio == -1.84

    ranked = rank_by_observation(list(accs.values()))
    assert [a.symbol for a in ranked] == ["000001"], [a.symbol for a in ranked]
    # 전환사채 종목이 1위로 올라오면 안 된다(비율만 보면 38%p 로 최대다)
    assert "000002" not in [a.symbol for a in ranked]

    d = ranked[0].as_dict()
    assert d["net_market_buy_ratio_pp"] == 2.1 and d["buyer_count"] == 2
    assert d["evidence"][0]["rcept_no"] and d["evidence"][0]["reason_class"] == MARKET_BUY

    # 두 API 의 날짜 형식이 같은 값으로 정규화된다
    assert normalize_date("20260825") == "20260825"
    assert normalize_date("2024-10-04") == "20241004"
    assert normalize_date("") == "" and normalize_date(None) == ""
    f = parse_filings([{"rcept_no": "9", "corp_name": "가", "stock_code": "000001",
                        "repror": "h", "rcept_dt": "2026-08-25", "stkrt": "1",
                        "stkrt_irds": "1", "stkqy_irds": "1",
                        "report_resn": "장내매수"}], source="majorstock")
    assert f[0].filed_at == "20260825", f[0].filed_at

    # elestock 은 필드 이름이 다르다 - 매핑 없이 읽으면 전부 None 이 된다
    ele = parse_filings([{
        "rcept_no": "E1", "rcept_dt": "2026-08-20", "corp_name": "가",
        "stock_code": "000001", "repror": "안상헌", "isu_exctv_ofcps": "대표이사",
        "isu_exctv_rgist_at": "등기임원", "sp_stock_lmp_cnt": "10,000",
        "sp_stock_lmp_irds_cnt": "10,000", "sp_stock_lmp_rate": "0.06",
        "sp_stock_lmp_irds_rate": "0.06",
    }], source="elestock")
    assert ele[0].ratio_after == 0.06 and ele[0].ratio_change == 0.06, ele[0]
    assert ele[0].qty_change == 10000.0
    assert "대표이사" in ele[0].reason
    # 사유가 없으니 매집으로 세지 않는다
    assert ele[0].reason_class == UNCLASSIFIED, ele[0].reason_class
    acc = aggregate(ele)[0]
    assert acc.market_buy_ratio == 0.0 and acc.unclassified_ratio == 0.06

    # 매수자 유형
    assert classify_buyer("MiriCapitalManagementLLC", "미코") == INSTITUTION
    assert classify_buyer("삼성자산운용", "다른회사") == INSTITUTION
    assert classify_buyer("대교홀딩스", "대교") == CONTROLLING
    # 상호가 회사명 어간을 공유하면 계열로 본다
    assert classify_buyer("다산인베스트", "다산솔루에타") == CONTROLLING
    assert classify_buyer("평화홀딩스", "평화산업") == CONTROLLING
    assert classify_buyer("홍성천", "파인디앤씨") == INSIDER
    assert classify_buyer("안상헌", "가", source="elestock") == INSIDER
    assert classify_buyer("", "가") == BUYER_UNKNOWN

    # 유형별로 따로 센다
    typed = parse_filings([
        {"rcept_no": "T1", "corp_name": "미코", "stock_code": "059090",
         "repror": "MiriCapitalManagementLLC", "rcept_dt": "20260820",
         "stkrt": "6.0", "stkrt_irds": "1.72", "stkqy_irds": "1",
         "report_resn": "장내매수"},
        {"rcept_no": "T2", "corp_name": "대교", "stock_code": "019680",
         "repror": "대교홀딩스", "rcept_dt": "20260820", "stkrt": "60",
         "stkrt_irds": "3.99", "stkqy_irds": "1", "report_resn": "장내매수"},
    ], source="majorstock")
    accs2 = aggregate(typed)
    by = {a.symbol: a for a in accs2}
    assert by["059090"].by_buyer_type == {INSTITUTION: 1.72}, by["059090"].by_buyer_type
    assert by["019680"].by_buyer_type == {CONTROLLING: 3.99}
    # 전체 순위는 지배주주가 위지만, 기관만 세우면 미코가 1위다
    assert [a.symbol for a in rank_by_observation(accs2)][0] == "019680"
    inst = rank_by_buyer_type(accs2, INSTITUTION)
    assert [a.symbol for a in inst] == ["059090"], [a.symbol for a in inst]
    assert by["059090"].as_dict()["institution_ratio_pp"] == 1.72

    # 매수자별 포트폴리오 - 여러 종목을 담은 쪽이 위로
    multi = parse_filings([
        {"rcept_no": "H1", "corp_name": "미코", "stock_code": "059090",
         "repror": "MiriCapitalManagementLLC", "rcept_dt": "20260820",
         "stkrt": "6", "stkrt_irds": "1.72", "stkqy_irds": "1",
         "report_resn": "장내매수"},
        {"rcept_no": "H2", "corp_name": "현대이지웰", "stock_code": "090850",
         "repror": "MiriCapitalManagementLLC", "rcept_dt": "20260819",
         "stkrt": "5.5", "stkrt_irds": "1.29", "stkqy_irds": "1",
         "report_resn": "장내매수"},
        {"rcept_no": "H3", "corp_name": "대교", "stock_code": "019680",
         "repror": "대교홀딩스", "rcept_dt": "20260820", "stkrt": "60",
         "stkrt_irds": "3.99", "stkqy_irds": "1", "report_resn": "장내매수"},
        {"rcept_no": "H4", "corp_name": "가", "stock_code": "000001",
         "repror": "누군가", "rcept_dt": "20260820", "stkrt": "9",
         "stkrt_irds": "9.0", "stkqy_irds": "1", "report_resn": "전환사채 인수"},
    ], source="majorstock")
    books = by_holder(multi)
    # 종목 2개를 담은 운용사가, 한 종목에 3.99%p 넣은 지주보다 위다
    assert books[0].holder == "MiriCapitalManagementLLC", [b.holder for b in books]
    assert len(books[0].positions) == 2 and books[0].buyer_type == INSTITUTION
    assert abs(books[0].total_ratio - 3.01) < 1e-9, books[0].total_ratio
    assert books[1].holder == "대교홀딩스"
    # 전환사채로 생긴 지분은 포트폴리오가 아니다
    assert all(b.holder != "누군가" for b in books), [b.holder for b in books]
    d = books[0].as_dict()
    assert d["positions"][0]["symbol"] == "059090" and d["position_count"] == 2

    # 같은 종목 공시 여러 건은 **한 포지션**이다 - 종목 수가 부풀면 안 된다
    repeat = parse_filings([
        {"rcept_no": f"R{i}", "corp_name": "닷밀", "stock_code": "464580",
         "repror": "정해운", "rcept_dt": f"2026082{i}", "stkrt": "10",
         "stkrt_irds": v, "stkqy_irds": "1", "report_resn": "장내매수"}
        for i, v in enumerate(("0.42", "0.32", "0.09"), start=1)
    ], source="majorstock")
    rb = by_holder(repeat)
    assert len(rb) == 1 and len(rb[0].positions) == 1, rb[0].as_dict()
    assert rb[0].positions[0]["filing_count"] == 3
    assert abs(rb[0].positions[0]["ratio_change_pp"] - 0.83) < 1e-9
    # 대표 공시는 가장 최신 것
    assert rb[0].positions[0]["rcept_no"] == "R3"
    assert rb[0].as_dict()["position_count"] == 1

    print("ownership_flow self-check OK")
