#!/usr/bin/env python3
"""브로커 계좌 직행 조회. **에이전트를 거치지 않는다.**

소유: 재일 (사용자 입구 배선분)
근거: CLAUDE.md 개발원칙 "LLM은 관련성 판단·서술에만 쓴다 - 한도 검사·필터는
      결정론적 Python", 마스터플랜 5.6(권한 분리)

▶ 왜 이 경로가 따로 있나 (2026-08-11 실측)
  "내 계좌 잔고" 를 묻는 데 CEO 라우팅(90~180초) + 부서 5곳(각 3~6분) + 종합을
  거쳤고, 결과는 5개 본부 전부 "산출 불가" 였다. **잔고 조회는 서술이 아니라
  사실 조회다** - 같은 입력에 같은 답이 나오는 것을 LLM 7번에 4분을 들여
  물어볼 이유가 없다. 여기는 0.5초에 답한다.

▶ 이 값이 무엇이고 무엇이 아닌가
  브로커(LS)가 **자기 장부 기준으로 말해 주는 값**이다. 우리 원장(`accounting.*`)이
  복식부기로 확정한 공식 NAV 가 **아니다.** 둘은 서로 대사하는 상대이고
  (`/accounting/v1/ledgers/{id}/reconciliations/*`), 값이 어긋나는 것 자체가
  회계본부가 조사할 사건이다. 그래서 응답에 `authoritative: false` 와
  `source: ls-openapi` 를 항상 싣는다 - 이 라벨이 빠지면 브로커 조회값이
  공식 NAV 로 둔갑하고, 자문과 원장의 구분이 사라진다.

▶ 여기서 하지 않는 것
  주문·정정·취소를 하지 않는다. 조회 TR(t0424) 하나만 부른다.
  값을 해석하지 않는다 - 수익률·비중 판단은 이 숫자를 받은 쪽의 일이다.

자체 점검:
    python apps/api/account_snapshot.py
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

ROOT = Path(__file__).resolve().parents[2]
# 리스크본부가 이미 t0424 클라이언트를 갖고 있다(동규 소유). 같은 것을 또 만들지
# 않는다 - 두 벌이 되면 토큰 갱신·필드 파싱이 서로 어긋난다.
_LS_PATH = ROOT / "departments" / "03-risk" / "integrations"
if str(_LS_PATH) not in sys.path:
    sys.path.insert(0, str(_LS_PATH))

# 브로커 조회는 사용자 입구와 별도 스위치다. 자격이 있어도 명시로 켠다.
ENABLE_BROKER_SNAPSHOT = os.getenv("ENABLE_BROKER_SNAPSHOT", "false").strip().lower() in {
    "1", "true", "yes", "on",
}
# 같은 계좌를 여러 화면·에이전트가 동시에 물어본다. TR 호출 한도를 아끼되
# **오래된 값을 새 값처럼 보이지 않게** 관측시각을 항상 같이 준다.
CACHE_SECONDS = int(os.getenv("BROKER_SNAPSHOT_CACHE_SECONDS", "10"))
_SYMBOL_RE = re.compile(r"^[0-9]{6}$")

router = APIRouter(tags=["account-snapshot"])

_lock = threading.Lock()
_cached: tuple[float, dict[str, Any]] | None = None


def _decimal_string(value: Any, field: str) -> str:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(502, f"브로커 보유잔고 {field}가 유효하지 않습니다") from exc
    if not parsed.is_finite() or parsed < 0:
        raise HTTPException(502, f"브로커 보유잔고 {field}가 유효하지 않습니다")
    return str(parsed)


def _normalize_positions(positions: Any) -> list[dict[str, str]]:
    """Normalize the already-fetched t0424 rows for the accounting consumer.

    This is intentionally a projection of the same PortfolioSnapshot used by
    ``/ui/account/snapshot``. It does not call the portfolio-live endpoint or
    issue another broker request, so Accounting and the user account card share
    one aggregation/cache path.
    """

    if not isinstance(positions, (tuple, list)):
        raise HTTPException(502, "브로커 보유잔고 행이 유효하지 않습니다")
    rows: list[dict[str, str]] = []
    for raw in positions:
        if not isinstance(raw, Mapping):
            raise HTTPException(502, "브로커 보유잔고 행이 유효하지 않습니다")
        symbol = str(raw.get("expcode") or raw.get("symbol") or "").strip()
        if symbol.startswith("A"):
            symbol = symbol[1:]
        if not _SYMBOL_RE.fullmatch(symbol):
            raise HTTPException(502, "브로커 보유잔고 종목코드가 유효하지 않습니다")
        quantity = raw.get("janqty", raw.get("quantity"))
        average_cost = raw.get("pamt", raw.get("average_cost"))
        if quantity is None or average_cost is None:
            raise HTTPException(502, "브로커 보유잔고 수량/평균단가가 없습니다")
        rows.append(
            {
                "symbol": symbol,
                "quantity": _decimal_string(quantity, "quantity"),
                "average_cost": _decimal_string(average_cost, "average_cost"),
            }
        )
    return rows


def _snapshot_now() -> dict[str, Any]:
    try:
        import ls_openapi  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - 통합 모듈이 없는 환경
        raise HTTPException(503, f"LS 연동 모듈을 찾을 수 없습니다: {exc}") from exc

    # ▶ `credential_status()` 로 미리 막지 않는다 (2026-08-11 실측)
    #   그 함수는 `LS_REST_BASE_URL_PAPER` 를 요구하는데, .env 주석대로 그 변수는
    #   **일부러 만들지 않는다**(모의도 같은 REST 호스트를 쓴다). 반면
    #   `LSOpenAPIConfig.from_env()` 는 접미사가 없으면 공용 URL 로 폴백한다.
    #   그래서 status 로 게이트를 걸면 **실제로는 되는 경로가 막힌다.**
    #   연결 가능 여부의 판정은 붙는 쪽이 한다 - 못 붙으면 아래에서 예외가 난다.
    status = ls_openapi.credential_status()
    try:
        with ls_openapi.LSOpenAPIClient.from_env() as client:
            snap = client.get_portfolio_snapshot()
    except ls_openapi.LSOpenAPIConfigurationError as exc:
        # 자격이 없는 것을 "잔고 0" 으로 바꾸지 않는다. 그건 거짓이다.
        raise HTTPException(503, f"LS 자격이 설정되지 않았습니다: {exc}"[:300]) from exc
    except Exception as exc:  # noqa: BLE001 - 브로커 오류를 빈 값으로 위장하지 않는다
        raise HTTPException(502, f"브로커 조회 실패: {type(exc).__name__}: {exc}"[:400]) from exc

    # ▶ **계좌를 식별 못 했으면 잔고를 주장하지 않는다** (2026-08-11 실측).
    #   LIVE 자격으로 붙었는데 account_no=None / equity=0 / positions=0 이 예외
    #   없이 돌아왔고, 사용자에게 "평가금액 0원" 이 **사실로** 나갔다. 계좌가 진짜
    #   비었는지, 조회가 조용히 실패했는지 이 응답만으로는 구분되지 않는다 -
    #   구분이 안 되면 단정하지 않는 쪽이 맞다(조회 실패 != 잔고 0).
    #   계좌번호는 잔고 조회가 성공하면 반드시 따라오는 값이라 이것이 판별자다.
    if not snap.account_no:
        raise HTTPException(
            502,
            "브로커가 계좌를 식별하지 못했습니다(계좌번호 없음) - 잔고를 "
            f"0으로 보고하지 않습니다. environment={status.get('environment')}")

    return {
        "schema_version": "account.broker-snapshot.v1",
        # 금액은 **문자열**이다. JSON number 는 double 이라 Decimal 이 깨진다
        # (readModel.ts 규칙 1과 같은 이유).
        "equity": str(snap.equity),
        "cash": str(snap.cash),
        "buying_power": str(snap.buying_power),
        "position_count": len(snap.positions),
        "account_no_masked": _mask(snap.account_no),
        "observed_at": snap.observed_at.isoformat(),
        "environment": status.get("environment"),
        # 이 세 줄이 이 응답의 계약이다. 빠지면 브로커 값이 공식 NAV 로 읽힌다.
        "source": snap.source,
        "authoritative": False,
        "official_nav_source": "/accounting/v1/ledgers/{ledger_id}",
        # Accounting consumes this projection from the same cached broker
        # response. ``synced`` means directly observed from LS, not agreement
        # with the official accounting ledger.
        "holdings": {
            "as_of": snap.observed_at.isoformat(),
            "error": None,
            "synced": True,
            "drift": [],
            "projection_source": "broker-account-snapshot-cache",
            "rows": _normalize_positions(snap.positions),
        },
    }


def _mask(account_no: str | None) -> str | None:
    """계좌번호는 뒤 4자리만 보인다. 화면·로그·에이전트 어디로도 전체가 안 나간다."""
    if not account_no:
        return None
    tail = str(account_no)[-4:]
    return f"****{tail}"


@router.get("/ui/account/snapshot", operation_id="broker_account_snapshot")
def broker_account_snapshot() -> dict[str, Any]:
    """브로커 계좌 현황. 사실 조회라 에이전트를 거치지 않는다."""
    if not ENABLE_BROKER_SNAPSHOT:
        raise HTTPException(
            503,
            "브로커 계좌 조회는 기본 비활성화 상태입니다 "
            "(ENABLE_BROKER_SNAPSHOT=true 로 엽니다).",
        )
    global _cached
    with _lock:
        if _cached is not None and time.time() - _cached[0] < CACHE_SECONDS:
            return _cached[1]
        # Keep the refresh inside the same lock. Account cards and the
        # accounting reconciler often wake together; without this single-flight
        # boundary they each issue the same t0424+CSPAQ12200 pair.
        payload = _snapshot_now()
        _cached = (time.time(), payload)
        return payload


__all__ = ["router", "ENABLE_BROKER_SNAPSHOT"]


if __name__ == "__main__":  # 자체 점검 - pytest 미도입(CLAUDE.md)
    # 계좌번호가 통째로 새지 않는가
    assert _mask("12345678901") == "****8901"
    assert _mask(None) is None
    assert _mask("") is None

    # 스위치가 꺼져 있으면 조회하지 않는다(비용·권한 경계)
    saved = ENABLE_BROKER_SNAPSHOT
    try:
        globals()["ENABLE_BROKER_SNAPSHOT"] = False
        try:
            broker_account_snapshot()
        except HTTPException as exc:
            assert exc.status_code == 503, exc
        else:
            raise AssertionError("스위치가 꺼졌는데 조회했다")
    finally:
        globals()["ENABLE_BROKER_SNAPSHOT"] = saved

    print("account_snapshot 자체 점검 통과")
