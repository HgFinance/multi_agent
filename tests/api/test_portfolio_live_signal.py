"""`/ui/portfolio/live/signal` - 거래 신호가 확인될 때만 화면을 깨우는 채널.

이 채널의 값어치는 **울리지 않는 것**에 있다. 3초 폴링은 그대로 살아 있으므로
신호가 한 번 빠져도 화면은 늦게나마 맞고, 반대로 아무 일도 없는데 울리면
비싼 `/ui/portfolio/live`(TR + DB)를 공짜로 한 번 더 부르는 순손실이다.
그래서 발화만큼 무발화를 같은 무게로 검증한다.
"""

from __future__ import annotations

import asyncio

import pytest

from apps.api import ls_account_stream as stream


def _order_event(tr_cd: str) -> dict[str, object]:
    return {
        "header": {"tr_cd": tr_cd},
        "body": {
            "accno1": "12345678901",
            "shtnIsuNo": "A005930",
            "IsuNm": "삼성전자",
            "ordno": "9001",
            "orgordno": "0",
            "execqty": "10",
            "execprc": "70000",
            "bnsTpCode": "2",
        },
    }


@pytest.fixture
def feed(monkeypatch: pytest.MonkeyPatch) -> stream._Feed:
    """모듈 전역 FEED를 건드리지 않는 깨끗한 피드."""

    fresh = stream._Feed()
    monkeypatch.setattr(stream, "FEED", fresh)
    monkeypatch.setattr(stream, "_revision_cache", None, raising=False)
    return fresh


@pytest.mark.parametrize("tr_cd", ["SC0", "SC1", "SC2", "SC3", "SC4"])
def test_every_order_event_kind_moves_the_revision(
    feed: stream._Feed, tr_cd: str
) -> None:
    before, _ = stream.portfolio_revision()
    assert feed.ingest(_order_event(tr_cd)) is not None
    after, payload = stream.portfolio_revision()
    assert after != before, f"{tr_cd} 주문 사건이 리비전을 움직이지 않았다"
    assert payload["seq"] == 1


def test_repeated_sync_of_identical_holdings_does_not_move_the_revision(
    feed: stream._Feed,
) -> None:
    """`sync_holdings`는 값이 같아도 as_of에 새 시각을 찍는다.

    as_of를 리비전에 그대로 넣으면 조용한 계좌가 브로커 재동기화 주기마다
    깨어나고, 그건 신호가 아니라 폴링을 하나 더 얹는 것이다.
    """

    holdings = {
        "net_asset": "1000",
        "realized_pnl": "0",
        "purchase_amount": "0",
        "valuation": "1000",
        "valuation_pnl": "0",
        "rows": [{"symbol": "005930", "quantity": "10"}],
    }
    feed.sync_holdings(dict(holdings))
    first, first_payload = stream.portfolio_revision()

    feed.sync_holdings(dict(holdings))
    second, second_payload = stream.portfolio_revision()

    assert second_payload["holdings_as_of"] != first_payload["holdings_as_of"], (
        "이 테스트는 as_of가 갱신되는 상황을 전제한다"
    )
    assert second == first, "잔고가 그대로인데 리비전이 움직였다"


def test_changed_holdings_move_the_revision(feed: stream._Feed) -> None:
    base = {
        "net_asset": "1000",
        "realized_pnl": "0",
        "purchase_amount": "0",
        "valuation": "1000",
        "valuation_pnl": "0",
        "rows": [{"symbol": "005930", "quantity": "10"}],
    }
    feed.sync_holdings(dict(base))
    before, _ = stream.portfolio_revision()
    feed.sync_holdings({**base, "rows": [{"symbol": "005930", "quantity": "11"}]})
    after, _ = stream.portfolio_revision()
    assert after != before, "잔고가 실제로 바뀌었는데 리비전이 그대로다"


def test_identical_today_activity_does_not_move_the_revision(
    feed: stream._Feed,
) -> None:
    activity = {"trade_count": 3, "summary": {"buy_amount": "100"}}
    feed.sync_today_activity(dict(activity))
    before, _ = stream.portfolio_revision()
    feed.sync_today_activity(dict(activity))
    after, _ = stream.portfolio_revision()
    assert after == before


def test_stream_status_change_moves_the_revision(feed: stream._Feed) -> None:
    """연결 상태는 화면에 배너로 그려지므로 값의 일부다."""

    before, _ = stream.portfolio_revision()
    feed.status = "CONNECTED"
    after, _ = stream.portfolio_revision()
    assert after != before


def test_revision_survives_decimal_holdings(feed: stream._Feed) -> None:
    """잔고 행에 Decimal이 섞여도 신호 채널이 죽으면 안 된다."""

    from decimal import Decimal

    feed.sync_holdings({"rows": [{"symbol": "005930", "quantity": Decimal("10")}]})
    token, _ = stream.portfolio_revision()
    assert len(token) == 16


def _collect(response, seconds: float) -> list[str]:
    async def run() -> list[str]:
        frames: list[str] = []

        async def read() -> None:
            async for chunk in response.body_iterator:
                frames.append(chunk)

        task = asyncio.create_task(read())
        await asyncio.sleep(seconds)
        task.cancel()
        return frames

    return asyncio.run(run())


def test_reconnect_with_the_same_cursor_stays_silent(
    feed: stream._Feed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """25초짜리 스트림이라, 커서를 안 보면 조용한 계좌도 25초마다 헛 갱신한다."""

    monkeypatch.setattr(stream, "PORTFOLIO_LIVE_MODE", "fixture")
    monkeypatch.setattr(stream, "PORTFOLIO_SIGNAL_SECONDS", 1.0)
    # 기본 2.5초 주기로는 1초짜리 창에 heartbeat가 한 번도 들어오지 않는다.
    monkeypatch.setattr(stream, "PORTFOLIO_SIGNAL_HEARTBEAT_SECONDS", 0.3)
    token, _ = stream.portfolio_revision()

    response = asyncio.run(stream.portfolio_live_signal(after=token))
    frames = _collect(response, 1.2)
    assert not [frame for frame in frames if "event: revision" in frame]
    assert [frame for frame in frames if frame.startswith(": heartbeat")]


def test_stale_cursor_catches_up_immediately(
    feed: stream._Feed, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stream, "PORTFOLIO_LIVE_MODE", "fixture")
    monkeypatch.setattr(stream, "PORTFOLIO_SIGNAL_SECONDS", 1.0)

    response = asyncio.run(stream.portfolio_live_signal(after="deadbeefdeadbeef"))
    frames = _collect(response, 1.2)
    assert len([frame for frame in frames if "event: revision" in frame]) == 1
