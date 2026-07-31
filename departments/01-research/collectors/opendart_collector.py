#!/usr/bin/env python3
"""Sprint J2: Open DART 공시 수집.

소유: 재일 (리서치본부)
근거: docs/06-integrations/opendart/01-disclosure-information.md (공시검색 2019001)
      docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 3.1(공시), 5.2(research Schema), 8.2
      supabase/migrations/20260729000300_research_quant_strategy.sql (research.documents)

▶ PIT 관련 중요한 한계 (실측 2026-07-30에 확인. 반드시 읽을 것)
  `list.json` 응답의 `rcept_dt` 는 **YYYYMMDD 날짜뿐이고 접수 시각이 없다.** 가이드 3.1이
  요구하는 "공시 시각"을 이 API 는 주지 않는다. `rcept_no` 앞 8자리도 날짜이고 뒤는
  순번이라 시각이 아니다.

  그래서 published_at 을 그날 00:00 KST 로 두면 **장중 의사결정에 미래 정보가 새어든다**
  (09:00 판단에 15:00 공시가 published_at=00:00 으로 보이는 문제).

  처리 방식: published_at 은 날짜만 있음을 명시하고(metadata.published_at_precision=DAY),
  **Backtest 와 Agent 는 observed_at 을 본다.** 가이드 4.2가 정확히 이것을 요구한다 -
  "Backtest 는 event_time 이 아니라 observed_at <= decision_time 조건을 지켜야 한다".
  정확한 접수 시각이 필요하면 공시원문(2019003)이나 별도 Source 를 붙여야 한다.

▶ 정정공시
  report_nm 앞에 `[기재정정]`, `[첨부정정]` 같은 표기가 붙는다. 이것으로 정정 여부를
  탐지해 status=CORRECTED 로 두되, **원본 문서를 덮어쓰지 않는다**(가이드 5.2 -
  document_versions 는 수정·삭제를 덮어쓰지 않음). 원본과의 관계 연결은 rcept_no 만으로는
  알 수 없어 미구현이며 document_relations 채우기는 후속이다.

자체 점검(호출 없음): python departments/01-research/collectors/opendart_collector.py
실제 수집:            python departments/01-research/collectors/opendart_collector.py --collect
                      (옵션: --from 20260727 --to 20260730 --cls Y)
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from source_registry import SourceRegistry, UseScope, load_project_env  # noqa: E402

COLLECTOR_VERSION = "research-opendart-collector-v1"

KST = timezone(timedelta(hours=9))
SOURCE_CODE = "opendart"
BASE_URL = "https://opendart.fss.or.kr/api"

# 응답 status. 000 만 정상이고 나머지는 전부 실패로 다룬다 - 013(데이터 없음)도
# 조용히 빈 결과로 넘기지 않고 구분해서 돌려준다.
STATUS_OK = "000"
STATUS_NO_DATA = "013"

# 문서에 초당 한도가 없다. 분당 1000건 제한이 알려져 있으나 확인된 값이 아니므로
# 보수적으로 초당 2회로 둔다. 확인되면 이 값을 근거와 함께 고친다.
RATE_LIMIT_PER_SEC = 2.0
PAGE_COUNT_MAX = 100

# corp_cls 법인구분. 문서 요청 인자 설명에서 확인한 값이다.
CORP_CLS = {"Y": "유가", "K": "코스닥", "N": "코넥스", "E": "기타"}

# report_nm 앞에 붙는 정정 표기. 문서에 목록이 없어 실측 관측값만 넣는다.
CORRECTION_MARKERS = ("[기재정정]", "[첨부정정]", "[첨부추가]", "[정정]", "[변경]")

DOCUMENT_TYPE_DISCLOSURE = "DART_DISCLOSURE"


class OpenDartError(RuntimeError):
    """Open DART 호출 실패. 빈 결과로 바꾸지 않는다."""


@dataclass(frozen=True)
class DocumentRecord:
    """research.documents 한 행 + 적재에 필요한 부가 정보."""

    external_id: str          # rcept_no - 접수번호. source 안에서 유일하다
    document_type: str
    title: str
    canonical_url: str
    published_at: datetime    # rcept_dt 날짜 00:00 KST. 시각 정밀도가 DAY 다
    observed_at: datetime     # 우리가 관측한 시각. Backtest 는 이걸 본다
    status: str
    metadata: dict = field(default_factory=dict)

    # 연결용. documents Column 이 아니다.
    corp_code: str = ""
    stock_code: str = ""
    corp_name: str = ""


@dataclass(frozen=True)
class CollectResult:
    fetched: int
    pages: int
    skipped: int
    records: tuple[DocumentRecord, ...]

    def summary(self) -> str:
        corrected = sum(1 for r in self.records if r.status == "CORRECTED")
        listed = sum(1 for r in self.records if r.stock_code)
        return (
            f"공시 {self.fetched}건 / {self.pages}페이지, 정규화 {len(self.records)} "
            f"(정정 {corrected}, 상장종목 연결 가능 {listed}), 제외 {self.skipped}"
        )


class _Limiter:
    def __init__(self, per_sec: float) -> None:
        self._interval = 1.0 / per_sec
        self._last = 0.0

    def wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self._interval:
            time.sleep(self._interval - gap)
        self._last = time.monotonic()


class OpenDartClient:
    """Open DART REST Client. 인증키를 Log 에 남기지 않는다."""

    def __init__(self, api_key: str | None = None) -> None:
        env = load_project_env()
        SourceRegistry(env=env).require(SOURCE_CODE)
        self._key = (api_key or env["OPEN_DART_API_KEY"]).strip()
        self._limiter = _Limiter(RATE_LIMIT_PER_SEC)

    def get(self, endpoint: str, params: dict) -> dict:
        self._limiter.wait()
        q = urllib.parse.urlencode({**params, "crtfc_key": self._key})
        url = f"{BASE_URL}/{endpoint}?{q}"
        try:
            with urllib.request.urlopen(url, timeout=25) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            # 인증키가 URL 에 있으므로 응답 본문을 그대로 올리지 않는다.
            raise OpenDartError(f"{endpoint} HTTP {e.code}") from None
        except urllib.error.URLError as e:
            raise OpenDartError(f"{endpoint} 연결 실패: {e.reason}") from None

        status = str(d.get("status", ""))
        if status == STATUS_NO_DATA:
            return {"status": status, "message": d.get("message", ""), "list": []}
        if status != STATUS_OK:
            raise OpenDartError(f"{endpoint} status={status} message={d.get('message')}")
        return d


def _parse_rcept_dt(raw: str) -> date:
    text = (raw or "").strip()
    if len(text) != 8 or not text.isdigit():
        raise ValueError(f"rcept_dt 가 YYYYMMDD 가 아니다: {raw!r}")
    return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))


def detect_correction(report_nm: str) -> str | None:
    """정정 표기를 찾아 돌려준다. 없으면 None."""
    text = (report_nm or "").strip()
    for marker in CORRECTION_MARKERS:
        if text.startswith(marker):
            return marker
    return None


def to_record(row: dict, *, observed_at: datetime) -> DocumentRecord:
    """list.json 한 행을 research.documents 계약으로 옮긴다."""
    rcept_no = str(row.get("rcept_no", "")).strip()
    if not rcept_no:
        raise ValueError("rcept_no 가 없다")
    report_nm = re.sub(r"\s+", " ", str(row.get("report_nm", "")).strip())
    if not report_nm:
        raise ValueError(f"{rcept_no}: report_nm 이 없다")

    dt = _parse_rcept_dt(row.get("rcept_dt", ""))
    marker = detect_correction(report_nm)

    return DocumentRecord(
        external_id=rcept_no,
        document_type=DOCUMENT_TYPE_DISCLOSURE,
        title=report_nm,
        canonical_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}",
        # 날짜만 아는 값이다. 정밀도를 metadata 에 명시한다.
        published_at=datetime.combine(dt, dtime(0, 0), tzinfo=KST),
        observed_at=observed_at,
        status="CORRECTED" if marker else "ACTIVE",
        metadata={
            "source": "opendart:2019001",
            "published_at_precision": "DAY",
            "published_at_note": (
                "list.json 의 rcept_dt 는 날짜뿐이며 접수 시각이 없다. "
                "PIT 판단은 observed_at 을 사용한다"
            ),
            "rcept_dt": dt.isoformat(),
            "corp_code": str(row.get("corp_code", "")).strip(),
            "corp_name": str(row.get("corp_name", "")).strip(),
            "stock_code": str(row.get("stock_code", "")).strip(),
            "corp_cls": str(row.get("corp_cls", "")).strip(),
            "corp_cls_name": CORP_CLS.get(str(row.get("corp_cls", "")).strip()),
            "filer_name": str(row.get("flr_nm", "")).strip(),
            "remark": str(row.get("rm", "")).strip() or None,
            "correction_marker": marker,
        },
        corp_code=str(row.get("corp_code", "")).strip(),
        stock_code=str(row.get("stock_code", "")).strip(),
        corp_name=str(row.get("corp_name", "")).strip(),
    )


def collect_disclosures(
    client: OpenDartClient,
    *,
    start: date,
    end: date,
    corp_cls: str | None = None,
    max_pages: int = 20,
    observed_at: datetime | None = None,
) -> CollectResult:
    """기간 공시 목록을 수집한다.

    corp_code 없이 조회하면 검색기간이 3개월로 제한된다(문서 요청 인자 설명).
    페이지를 끝까지 돌지 않고 max_pages 로 끊는데, **끊었으면 그 사실을 알린다** -
    조용히 자르면 수집 누락을 커버리지 문제로 오인한다.
    """
    if start > end:
        raise ValueError("start 가 end 보다 늦다")
    if corp_cls is not None and corp_cls not in CORP_CLS:
        raise ValueError(f"corp_cls 는 {sorted(CORP_CLS)} 중 하나다: {corp_cls!r}")

    obs = observed_at or datetime.now(timezone.utc)
    records: list[DocumentRecord] = []
    skipped = 0
    page = 1
    total_pages = None

    while page <= max_pages:
        params = {
            "bgn_de": start.strftime("%Y%m%d"),
            "end_de": end.strftime("%Y%m%d"),
            "page_no": page,
            "page_count": PAGE_COUNT_MAX,
        }
        if corp_cls:
            params["corp_cls"] = corp_cls
        d = client.get("list.json", params)
        rows = d.get("list") or []
        if total_pages is None:
            total_pages = int(d.get("total_page") or 0)
        if not rows:
            break

        for row in rows:
            try:
                records.append(to_record(row, observed_at=obs))
            except ValueError:
                # 한 건 때문에 전체를 멈추지 않되 조용히 버리지도 않는다
                skipped += 1
        if total_pages and page >= total_pages:
            break
        page += 1

    truncated = bool(total_pages and total_pages > max_pages)
    if truncated:
        # 로그가 아니라 결과에 남겨야 호출자가 판단할 수 있다
        for r in records[:1]:
            r.metadata["coverage_truncated"] = {
                "max_pages": max_pages, "total_page": total_pages,
            }

    return CollectResult(
        fetched=len(records) + skipped,
        pages=min(page, total_pages or page),
        skipped=skipped,
        records=tuple(records),
    )


# ---------------------------------------------------------------------------
# 자체 점검 - 외부 호출 없음
# ---------------------------------------------------------------------------

_ROW = {
    "corp_code": "00126380", "corp_name": "삼성전자", "stock_code": "005930",
    "corp_cls": "Y", "report_nm": "주요사항보고서(자기주식취득결정)",
    "rcept_no": "20260730000123", "flr_nm": "삼성전자", "rcept_dt": "20260730", "rm": "유",
}
_OBS = datetime(2026, 7, 30, 5, 0, tzinfo=timezone.utc)


def _check_record_mapping():
    r = to_record(_ROW, observed_at=_OBS)
    assert r.external_id == "20260730000123"
    assert r.document_type == DOCUMENT_TYPE_DISCLOSURE
    assert r.status == "ACTIVE"
    assert r.canonical_url.endswith("rcpNo=20260730000123")
    assert r.published_at.astimezone(KST).date() == date(2026, 7, 30)
    assert r.published_at.astimezone(KST).hour == 0
    assert r.observed_at == _OBS
    assert r.stock_code == "005930" and r.corp_code == "00126380"
    # PIT 한계가 metadata 에 남아야 한다
    assert r.metadata["published_at_precision"] == "DAY"
    assert "observed_at" in r.metadata["published_at_note"]
    assert r.metadata["corp_cls_name"] == "유가"
    print("  공시 -> documents 매핑      OK")


def _check_correction_detection():
    for nm, expected in (
        ("[기재정정]단일판매ㆍ공급계약체결", "[기재정정]"),
        ("[첨부정정]지속가능경영보고서등관련사항(자율공시)", "[첨부정정]"),
        ("타인에대한채무보증결정", None),
        ("", None),
    ):
        assert detect_correction(nm) == expected, nm

    c = to_record({**_ROW, "report_nm": "[기재정정]단일판매ㆍ공급계약체결"}, observed_at=_OBS)
    assert c.status == "CORRECTED"
    assert c.metadata["correction_marker"] == "[기재정정]"
    # 원본을 덮어쓰지 않는다 - external_id 가 다르므로 별개 문서로 적재된다
    assert c.external_id == _ROW["rcept_no"]
    print("  정정공시 탐지               OK")


def _check_title_normalization():
    # 실측 응답의 report_nm 에 공백이 다수 붙어 있었다
    r = to_record({**_ROW, "report_nm": "타인에대한채무보증결정   \t  (자회사)  "}, observed_at=_OBS)
    assert r.title == "타인에대한채무보증결정 (자회사)", repr(r.title)
    print("  제목 공백 정규화            OK")


def _check_fail_closed():
    for bad, why in (
        ({**_ROW, "rcept_no": ""}, "rcept_no 없음"),
        ({**_ROW, "report_nm": "  "}, "report_nm 없음"),
        ({**_ROW, "rcept_dt": "2026-07-30"}, "날짜 형식"),
        ({**_ROW, "rcept_dt": ""}, "날짜 없음"),
    ):
        try:
            to_record(bad, observed_at=_OBS)
            raise AssertionError(f"{why} 가 통과했다")
        except ValueError:
            pass

    class _FakeClient:
        def get(self, endpoint, params):
            return {"status": "000", "list": [], "total_page": 0}

    try:
        collect_disclosures(_FakeClient(), start=date(2026, 7, 30), end=date(2026, 7, 1))
        raise AssertionError("start > end 가 통과했다")
    except ValueError:
        pass
    try:
        collect_disclosures(_FakeClient(), start=date(2026, 7, 1), end=date(2026, 7, 30),
                            corp_cls="Z")
        raise AssertionError("잘못된 corp_cls 가 통과했다")
    except ValueError:
        pass
    print("  fail-closed                 OK")


def _check_pagination_and_truncation():
    class _Pages:
        def __init__(self, total): self.total = total; self.calls = 0
        def get(self, endpoint, params):
            self.calls += 1
            page = params["page_no"]
            if page > self.total:
                return {"status": "000", "list": [], "total_page": self.total}
            return {"status": "000", "total_page": self.total,
                    "list": [{**_ROW, "rcept_no": f"2026073000{page:04d}{i:02d}"} for i in range(3)]}

    c = _Pages(total=3)
    r = collect_disclosures(c, start=date(2026, 7, 30), end=date(2026, 7, 30), max_pages=20)
    assert len(r.records) == 9 and r.pages == 3, (len(r.records), r.pages)
    assert len({x.external_id for x in r.records}) == 9

    # 페이지를 끊었으면 그 사실이 결과에 남아야 한다
    c2 = _Pages(total=10)
    r2 = collect_disclosures(c2, start=date(2026, 7, 30), end=date(2026, 7, 30), max_pages=2)
    assert len(r2.records) == 6
    assert r2.records[0].metadata["coverage_truncated"]["total_page"] == 10
    print("  페이지네이션/절단 표시      OK")


def _check_license_scope():
    """DART 는 전문 저장·Embedding 이 허용된 Source 다(뉴스와 다르다)."""
    reg = SourceRegistry(env={"OPEN_DART_API_KEY": "k" * 40})
    reg.check_use(SOURCE_CODE, UseScope.FULLTEXT_STORE)
    reg.check_use(SOURCE_CODE, UseScope.EMBEDDING)
    reg.check_use(SOURCE_CODE, UseScope.LONG_TERM_ARCHIVE)
    print("  라이선스 Scope              OK")


def _collect_and_report(start: date, end: date, corp_cls: str | None,
                        max_pages: int = 5) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repository"))
    from reference_repository import SupabaseReferenceRepository

    client = OpenDartClient()
    r = collect_disclosures(client, start=start, end=end, corp_cls=corp_cls,
                            max_pages=max_pages)
    print(f"  {r.summary()}")
    for rec in r.records[:5]:
        mark = f" {rec.metadata['correction_marker']}" if rec.metadata["correction_marker"] else ""
        print(f"    {rec.external_id} {rec.corp_name}({rec.stock_code or '-'}){mark}")
        print(f"      {rec.title[:60]}")
    if r.records and "coverage_truncated" in r.records[0].metadata:
        print(f"  ⚠ 페이지 절단: {r.records[0].metadata['coverage_truncated']}")

    # ▶ 적재 - 조회·보고로 끝나면 이 수집기는 수집기가 아니다 (2026-07-31 실측:
    #   스케줄러가 10분마다 OK 를 찍는데 Supabase 는 하루째 0건이었다. --collect 가
    #   fetch-and-report 프로브였던 것. 신규 corp 는 issuer 로 먼저 만들고 문서를
    #   연결한다 - 이후 daily company-profile Job 이 개황을 채우는 구조다.)
    if not r.records:
        print("  적재할 것이 없다")
        return 0
    repo = SupabaseReferenceRepository()
    try:
        _, _, src = repo.sync_data_sources()
        if "opendart" not in src:
            raise OpenDartError("data_sources 에 opendart 가 없다")
        corps = sorted({(rec.corp_code, rec.corp_name) for rec in r.records if rec.corp_code})
        ni, _ui, issuer_by_corp = repo.upsert_issuers(list(corps))
        n, u, unlinked = repo.upsert_documents(
            list(r.records), source_id=src["opendart"], issuer_by_corp=issuer_by_corp
        )
        print(f"  research.documents: 신규 {n} 갱신 {u} "
              f"(issuer 신규 {ni}, 미연결 {unlinked})")
        n2, _u2, _ = repo.upsert_documents(
            list(r.records), source_id=src["opendart"], issuer_by_corp=issuer_by_corp
        )
        if n2:
            raise OpenDartError(f"재적재가 새 행을 만들었다: {n2} - 멱등 키 확인")
        print("  멱등 재시도: 신규 0")
    finally:
        repo.close()
    return 0


if __name__ == "__main__":
    if "--collect" in sys.argv:
        a = sys.argv
        def opt(n, d): return a[a.index(n) + 1] if n in a else d
        s = _parse_rcept_dt(opt("--from", (datetime.now(KST) - timedelta(days=3)).strftime("%Y%m%d")))
        e = _parse_rcept_dt(opt("--to", datetime.now(KST).strftime("%Y%m%d")))
        cls = opt("--cls", None)
        # 기본 5 는 수동 프로브용이다. 3일 창 전체를 놓치지 않으려면 30 이 필요했다
        # (실측 2026-07-31: 3일 창이 25페이지 - 스케줄러가 --max-pages 30 을 넘긴다)
        pages = int(opt("--max-pages", "5"))
        print(f"{COLLECTOR_VERSION} 실제 수집 {s} ~ {e} cls={cls or '전체'} pages<={pages}")
        raise SystemExit(_collect_and_report(s, e, cls, max_pages=pages))

    print(f"{COLLECTOR_VERSION} 자체 점검 (외부 호출 없음)")
    _check_record_mapping()
    _check_correction_detection()
    _check_title_normalization()
    _check_fail_closed()
    _check_pagination_and_truncation()
    _check_license_scope()
    print("OpenDART 6개 영역 통과. 실제 수집은 --collect")
