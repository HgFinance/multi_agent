#!/usr/bin/env python3
"""Sprint J1/F02: LS Open API REST Client와 종목 마스터 수집.

소유: 재일 (리서치본부)
근거: docs/06-integrations/ls-openapi/01-oauth/01-33bd887a.md (접근토큰 발급)
      docs/06-integrations/ls-openapi/03-stock/13-316495d3.md (t8436 주식종목조회 API용)
      docs/02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md F02(Instrument Universe)
      docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 3.1, 8.1, 8.2

이 파일이 외부 API 경계다. Agent 는 이 Client 를 직접 쓰지 않는다(가이드 3.3 - Agent 가
API Key 를 들고 직접 Crawling 하지 않는다). 수집기와 Reference Service 만 호출한다.

▶ 실측으로 확인한 것 (2026-07-30). 문서에 없던 값이라 근거를 남긴다.
  - grant_type=client_credentials, scope=oob 로 토큰이 발급된다. 문서 필드 목록에는
    이름만 있고 값이 없었다.
  - token_type=Bearer.
  - **모의(PAPER) 앱키도 실전 REST Domain(:8080)에서 토큰이 발급된다.** 모의투자 REST
    Domain 이 '-'(미제공)이라 LS_REST_BASE_URL_PAPER 를 만들지 않은 판단이 맞았다.
  - 문서는 응답에 expire_in 을 필수(Y)로 적어놨지만 **실제 응답에 없다.** 그래서 만료
    시각을 응답에서 읽지 않고 보수적인 기본값으로 관리한다.

자체 점검(호출 없음): python departments/01-research/collectors/ls_client.py
실제 수집:            python departments/01-research/collectors/ls_client.py --fetch-master
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from source_registry import SourceRegistry, UseScope, load_project_env  # noqa: E402
from subscription_plan import AssetClass, UniverseMember, Venue  # noqa: E402

CLIENT_VERSION = "research-ls-client-v1"

# 문서에 값이 없어 실측으로 확인한 상수. 바꾸기 전에 실제 호출로 재확인할 것.
GRANT_TYPE = "client_credentials"
SCOPE = "oob"

# 문서가 응답에 expire_in 을 적어놨지만 실제로 오지 않는다. 재발급 주기를 보수적으로
# 잡는다 - 토큰이 실제로 얼마나 유효한지 모르는 상태에서 오래 들고 있으면 장중에 끊긴다.
TOKEN_TTL = timedelta(hours=1)

# t8436 문서의 "초당 호출 제한: 2". 코드에 박지 않고 TR 별로 받는다.
DEFAULT_RATE_LIMIT_PER_SEC = 2.0


class LsApiError(RuntimeError):
    """LS API 호출 실패. 빈 결과로 바꾸지 않는다."""


@dataclass(frozen=True)
class LsEnvironment:
    """실전/모의 구분. 둘의 Key 를 절대 섞지 않는다(.env 규칙)."""

    name: str
    app_key: str
    app_secret: str
    rest_base_url: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> LsEnvironment:
        e = env or load_project_env()
        # 사용 가능 여부는 Registry 가 판정한다. 키가 없으면 여기서 예외다.
        SourceRegistry(env=e).require("ls_openapi_rest")

        mode = (e.get("LS_ENV") or "PAPER").strip().upper()
        if mode == "PAPER":
            key, secret = e.get("LS_APP_KEY_PAPER", ""), e.get("LS_APP_SECRET_KEY_PAPER", "")
        elif mode == "LIVE":
            key, secret = e.get("LS_APP_KEY", ""), e.get("LS_APP_SECRET_KEY", "")
        else:
            raise LsApiError(f"LS_ENV 는 PAPER 또는 LIVE 여야 한다: {mode!r}")

        if not key or not secret:
            raise LsApiError(f"LS_ENV={mode} 인데 해당 App Key/Secret 이 비어 있다")

        # REST 는 모의 Domain 이 없다. 실측에서 모의 키도 이 Domain 에서 발급됐다.
        return cls(
            name=mode,
            app_key=key,
            app_secret=secret,
            rest_base_url=(e.get("LS_REST_BASE_URL") or "").rstrip("/"),
        )


class RateLimiter:
    """TR 별 초당 호출 제한. 문서에 적힌 값을 그대로 받는다."""

    def __init__(self, per_sec: float = DEFAULT_RATE_LIMIT_PER_SEC) -> None:
        if per_sec <= 0:
            raise ValueError("per_sec 는 0 보다 커야 한다")
        self._interval = 1.0 / per_sec
        self._last = 0.0

    def wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self._interval:
            time.sleep(self._interval - gap)
        self._last = time.monotonic()


class LsRestClient:
    """LS Open API REST Client.

    토큰을 메모리에만 둔다. 파일이나 Log 에 쓰지 않는다(가이드 8.3).
    """

    def __init__(self, environment: LsEnvironment | None = None, *, timeout: int = 20) -> None:
        self._env = environment or LsEnvironment.from_env()
        self._timeout = timeout
        self._token: str | None = None
        self._token_expires_at: datetime | None = None
        self._limiters: dict[str, RateLimiter] = {}

    @property
    def environment(self) -> str:
        return self._env.name

    def _post(self, path: str, *, data: bytes, headers: dict[str, str]) -> dict:
        url = f"{self._env.rest_base_url}{path}"
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read()[:400].decode("utf-8", "replace")
            # 응답 본문에 Key 가 실릴 수 있으므로 그대로 올리지 않고 요약만 남긴다.
            raise LsApiError(f"{path} HTTP {e.code}: {body}") from None
        except urllib.error.URLError as e:
            raise LsApiError(f"{path} 연결 실패: {e.reason}") from None

    def token(self) -> str:
        """토큰을 발급/재사용한다. 만료 시각은 응답에 없어 TOKEN_TTL 로 관리한다."""
        now = datetime.now(timezone.utc)
        if self._token and self._token_expires_at and now < self._token_expires_at:
            return self._token

        body = urllib.parse.urlencode(
            {
                "grant_type": GRANT_TYPE,
                "appkey": self._env.app_key,
                "appsecretkey": self._env.app_secret,
                "scope": SCOPE,
            }
        ).encode()
        d = self._post(
            "/oauth2/token",
            data=body,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        tok = d.get("access_token")
        if not tok:
            raise LsApiError(f"access_token 이 응답에 없다: keys={sorted(d)}")

        self._token = tok
        self._token_expires_at = now + TOKEN_TTL
        return tok

    def call_tr(
        self,
        *,
        path: str,
        tr_cd: str,
        in_block: dict,
        rate_limit_per_sec: float = DEFAULT_RATE_LIMIT_PER_SEC,
        tr_cont: str = "N",
        tr_cont_key: str = "",
    ) -> dict:
        """TR 한 건 호출. in_block 은 {"t8436InBlock": {...}} 형태 그대로 넣는다.

        ▶ **LS API 는 필드 타입을 엄격히 본다.** 문서 필드표의 종류가 Number 인 것은
          JSON 숫자로 보내야 한다. 문자열로 보내면 HTTP 500 과 함께
          "IGW40011 ... data type을 확인하세요" 가 온다(실측 2026-07-30:
          t8410 의 qrycnt, t1305 의 dwmcode 를 문자열로 보내 실패).
          401 이 아니라 500 이므로 인증 문제로 오진하기 쉽다.
        """
        limiter = self._limiters.setdefault(tr_cd, RateLimiter(rate_limit_per_sec))
        limiter.wait()

        headers = {
            "content-type": "application/json; charset=UTF-8",
            "authorization": f"Bearer {self.token()}",
            "tr_cd": tr_cd,
            "tr_cont": tr_cont,
            "tr_cont_key": tr_cont_key,
            # 문서상 필수지만 법인 전용이다. 개인 계정은 빈 값으로 보낸다.
            "mac_address": "",
        }
        return self._post(
            path, data=json.dumps(in_block, ensure_ascii=False).encode("utf-8"), headers=headers
        )


# ---------------------------------------------------------------------------
# t8436 주식종목조회 - F02 Instrument Universe 의 입력
# ---------------------------------------------------------------------------

T8436_PATH = "/stock/etc"
T8436_RATE_LIMIT = 2.0  # 문서 "초당 호출 제한: 2"

# t8436OutBlock.gubun - 문서: 구분(1:코스피 2:코스닥)
_GUBUN_TO_VENUE = {"1": Venue.KOSPI, "2": Venue.KOSDAQ}


@dataclass(frozen=True)
class StockMasterRow:
    """t8436OutBlock 한 행을 정규화한 것. 필드 이름은 문서와 1:1로 맞춘다."""

    shcode: str          # 단축코드 - provider_symbol
    expcode: str         # 확장코드(표준코드) - ISIN 계열
    hname: str           # 한글명
    venue: Venue         # gubun 에서 유도
    is_etf: bool         # etfgubun == "1"
    is_etn: bool         # etfgubun == "2"
    is_spac: bool        # spac_gubun == "Y"
    security_group: str  # bu12gubun 증권그룹
    order_unit: str      # memedan 주문수량단위
    prev_close: str      # jnilclose 전일가
    upper_limit: str     # uplmtprice 상한가
    lower_limit: str     # dnlmtprice 하한가

    @classmethod
    def from_out_block(cls, row: dict) -> StockMasterRow | None:
        """분류 불가 행은 None 이다. 추정으로 채우지 않는다."""
        gubun = str(row.get("gubun", "")).strip()
        venue = _GUBUN_TO_VENUE.get(gubun)
        if venue is None:
            return None
        shcode = str(row.get("shcode", "")).strip()
        if not shcode:
            return None
        etf = str(row.get("etfgubun", "")).strip()
        return cls(
            shcode=shcode,
            expcode=str(row.get("expcode", "")).strip(),
            hname=str(row.get("hname", "")).strip(),
            venue=venue,
            is_etf=etf == "1",
            is_etn=etf == "2",
            is_spac=str(row.get("spac_gubun", "")).strip().upper() == "Y",
            security_group=str(row.get("bu12gubun", "")).strip(),
            order_unit=str(row.get("memedan", "")).strip(),
            prev_close=str(row.get("jnilclose", "")).strip(),
            upper_limit=str(row.get("uplmtprice", "")).strip(),
            lower_limit=str(row.get("dnlmtprice", "")).strip(),
        )


def fetch_stock_master(client: LsRestClient, *, gubun: str = "0") -> tuple[list[StockMasterRow], int]:
    """전 종목 마스터를 받는다. gubun 0:전체 1:코스피 2:코스닥 (문서 t8436InBlock).

    (정규화된 행, 분류 실패 행 수)를 돌려준다. 실패 수를 숨기지 않는 이유 - 조용히
    버리면 Universe 가 줄어든 것을 알 수 없다(가이드 8.2 DQ).
    """
    if gubun not in ("0", "1", "2"):
        raise ValueError("gubun 은 0(전체)/1(코스피)/2(코스닥) 중 하나다")

    d = client.call_tr(
        path=T8436_PATH,
        tr_cd="t8436",
        in_block={"t8436InBlock": {"gubun": gubun}},
        rate_limit_per_sec=T8436_RATE_LIMIT,
    )
    raw = d.get("t8436OutBlock")
    if raw is None:
        raise LsApiError(f"t8436OutBlock 이 응답에 없다: keys={sorted(d)}")

    rows: list[StockMasterRow] = []
    unclassified = 0
    for r in raw:
        row = StockMasterRow.from_out_block(r)
        if row is None:
            unclassified += 1
            continue
        rows.append(row)
    return rows, unclassified


def to_universe_members(
    rows: list[StockMasterRow],
    *,
    instrument_id_for,
    exclude_etf: bool = False,
    exclude_spac: bool = False,
) -> list[UniverseMember]:
    """마스터 행을 구독 계획 입력으로 바꾼다.

    instrument_id_for 는 (venue, shcode) -> UUID 를 돌려주는 함수다. 영구
    instrument_id 발급은 Reference Service 책임이고 이 모듈이 만들지 않는다 -
    종목코드를 영구 Primary Key 로 쓰지 않기 위한 경계다(가이드 3.3, 8.1).
    """
    out: list[UniverseMember] = []
    for r in rows:
        if exclude_etf and (r.is_etf or r.is_etn):
            continue
        if exclude_spac and r.is_spac:
            continue
        out.append(
            UniverseMember(
                instrument_id=instrument_id_for(r.venue, r.shcode),
                provider_symbol=r.shcode,
                venue=r.venue,
                asset_class=AssetClass.EQUITY,
            )
        )
    return out


# ---------------------------------------------------------------------------
# 자체 점검 - 외부 호출 없음
# ---------------------------------------------------------------------------

_SAMPLE = {
    "t8436OutBlock": [
        {"hname": "삼성전자", "shcode": "005930", "expcode": "KR7005930003", "etfgubun": "0",
         "uplmtprice": "91000", "dnlmtprice": "49100", "jnilclose": "70000", "memedan": "1",
         "recprice": "70000", "gubun": "1", "bu12gubun": "01", "spac_gubun": "N"},
        {"hname": "에코프로", "shcode": "086520", "expcode": "KR7086520004", "etfgubun": "0",
         "uplmtprice": "1", "dnlmtprice": "1", "jnilclose": "1", "memedan": "1",
         "recprice": "1", "gubun": "2", "bu12gubun": "01", "spac_gubun": "N"},
        {"hname": "KODEX 200", "shcode": "069500", "expcode": "KR7069500007", "etfgubun": "1",
         "uplmtprice": "1", "dnlmtprice": "1", "jnilclose": "1", "memedan": "1",
         "recprice": "1", "gubun": "1", "bu12gubun": "05", "spac_gubun": "N"},
        {"hname": "무슨스팩", "shcode": "123456", "expcode": "KR7123456789", "etfgubun": "0",
         "uplmtprice": "1", "dnlmtprice": "1", "jnilclose": "1", "memedan": "1",
         "recprice": "1", "gubun": "2", "bu12gubun": "01", "spac_gubun": "Y"},
        # 분류 불가 - gubun 이 비었다. 추정하지 않고 세기만 한다
        {"hname": "이상행", "shcode": "999999", "gubun": "", "etfgubun": "0", "spac_gubun": "N"},
        # shcode 없음
        {"hname": "코드없음", "shcode": "", "gubun": "1", "etfgubun": "0", "spac_gubun": "N"},
    ]
}


def _check_parsing():
    rows, bad = [], 0
    for r in _SAMPLE["t8436OutBlock"]:
        row = StockMasterRow.from_out_block(r)
        if row is None:
            bad += 1
        else:
            rows.append(row)
    assert len(rows) == 4 and bad == 2, f"rows={len(rows)} bad={bad}"

    by_code = {r.shcode: r for r in rows}
    assert by_code["005930"].venue is Venue.KOSPI and by_code["005930"].hname == "삼성전자"
    assert by_code["086520"].venue is Venue.KOSDAQ
    assert by_code["069500"].is_etf is True
    assert by_code["123456"].is_spac is True
    assert by_code["005930"].expcode == "KR7005930003"
    print("  t8436 파싱                OK")


def _check_universe_mapping():
    rows = [r for r in (StockMasterRow.from_out_block(x) for x in _SAMPLE["t8436OutBlock"]) if r]
    from uuid import uuid5, NAMESPACE_URL

    def iid(venue, code):
        return uuid5(NAMESPACE_URL, f"ls://{venue.value}/{code}")

    allm = to_universe_members(rows, instrument_id_for=iid)
    assert len(allm) == 4
    # 같은 종목은 항상 같은 instrument_id 여야 한다(Ticker 변경 추적의 전제는 아니지만
    # 실행마다 바뀌면 적재가 중복된다)
    assert allm[0].instrument_id == iid(Venue.KOSPI, "005930")

    no_etf = to_universe_members(rows, instrument_id_for=iid, exclude_etf=True)
    assert len(no_etf) == 3
    no_spac = to_universe_members(rows, instrument_id_for=iid, exclude_etf=True, exclude_spac=True)
    assert len(no_spac) == 2
    assert {m.venue for m in no_spac} == {Venue.KOSPI, Venue.KOSDAQ}
    print("  Universe 매핑             OK")


def _check_env_and_rate_limit():
    # LS_ENV 가 이상하면 조용히 실전으로 떨어지지 않는다
    base = {"LS_APP_KEY": "k", "LS_APP_SECRET_KEY": "s", "LS_REST_BASE_URL": "https://x"}
    try:
        LsEnvironment.from_env({**base, "LS_ENV": "REAL"})
        raise AssertionError("잘못된 LS_ENV 가 통과했다")
    except LsApiError:
        pass

    # PAPER 인데 모의 키가 없으면 실전 키로 대체하지 않는다
    try:
        LsEnvironment.from_env({**base, "LS_ENV": "PAPER"})
        raise AssertionError("모의 키 없이 PAPER 가 통과했다")
    except LsApiError as e:
        assert "PAPER" in str(e)

    # 키가 아예 없으면 Registry 가 막는다
    try:
        LsEnvironment.from_env({"LS_ENV": "LIVE"})
        raise AssertionError("키 없이 통과했다")
    except Exception as e:
        assert "ls_openapi_rest" in str(e) or "LS_APP_KEY" in str(e)

    rl = RateLimiter(per_sec=20.0)
    t0 = time.monotonic()
    for _ in range(3):
        rl.wait()
    assert time.monotonic() - t0 >= 0.10 - 0.02, "Rate Limit 이 동작하지 않는다"
    try:
        RateLimiter(per_sec=0)
        raise AssertionError("per_sec=0 이 통과했다")
    except ValueError:
        pass
    print("  환경 분리/Rate Limit      OK")


def _fetch_and_report() -> int:
    """실제 LS API 를 호출해 전 종목 마스터를 받는다."""
    from uuid import uuid5, NAMESPACE_URL

    client = LsRestClient()
    print(f"  환경: LS_ENV={client.environment} (REST Domain 은 실전 하나뿐)")
    print(f"  토큰 발급... {len(client.token())}자 확보")

    rows, unclassified = fetch_stock_master(client)
    print(f"  t8436 전 종목: {len(rows)}개 정규화, 분류 실패 {unclassified}개")

    kospi = [r for r in rows if r.venue is Venue.KOSPI]
    kosdaq = [r for r in rows if r.venue is Venue.KOSDAQ]
    etf = [r for r in rows if r.is_etf or r.is_etn]
    spac = [r for r in rows if r.is_spac]
    print(f"    KOSPI {len(kospi)} / KOSDAQ {len(kosdaq)}")
    print(f"    ETF·ETN {len(etf)} / SPAC {len(spac)}")

    def iid(venue, code):
        return uuid5(NAMESPACE_URL, f"ls://{venue.value}/{code}")

    members = to_universe_members(rows, instrument_id_for=iid, exclude_etf=True, exclude_spac=True)
    print(f"  구독 대상(ETF·SPAC 제외): {len(members)}개")

    from subscription_plan import DataKind, build_plan

    plan = build_plan(members, (DataKind.TICK, DataKind.QUOTE))
    print(f"  구독 계획: {plan.count}건")
    for path, n in plan.by_socket().items():
        print(f"    {path:26} {n}")

    sample = sorted(rows, key=lambda r: r.shcode)[:3]
    print("  샘플:", ", ".join(f"{r.shcode} {r.hname}({r.venue.value})" for r in sample))
    return 0


if __name__ == "__main__":
    if "--fetch-master" in sys.argv:
        print(f"{CLIENT_VERSION} 실제 수집")
        raise SystemExit(_fetch_and_report())

    print(f"{CLIENT_VERSION} 자체 점검 (외부 호출 없음)")
    _check_parsing()
    _check_universe_mapping()
    _check_env_and_rate_limit()
    print("Client 3개 영역 통과. 실제 수집은 --fetch-master")
