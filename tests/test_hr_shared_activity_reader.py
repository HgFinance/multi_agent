"""HR 관측 4종이 Langfuse 를 몇 번 부르는지 고정한다 (2026-08-26 통합).

왜 이 테스트가 있나: 통합 전에는 유휴·Capacity·LLM 사용량·발화율이 각자 reader 를
만들어 **같은 실행 이벤트를 네 번** 읽었다. Worker 8명 기준 화면 1회당 왕복 40회.
그중 Capacity 와 LLM 사용량은 event_name·창·limit 이 글자 그대로 같은 질의였고,
집계 축만 달랐다. 60초 폴링이라 그게 그대로 분당 부하가 됐다.

이 실패는 조용하다 - 값은 다 맞게 나오고 화면도 정상이라, 창이 어긋나 캐시 키가
갈라지는 식으로 회귀해도 아무도 모른다. 그래서 "결과가 맞나"가 아니라 "왕복이
몇 번인가"를 직접 센다.

자체 점검(python observability.py)이 아니라 pytest 로 두는 이유: 왕복 수는 등록
Worker 수에 딸린 값이라 registry 를 읽어야 하고, 그건 자체 점검이 이미 하는 일과
겹치지 않는 별개의 계약이다.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "departments/07-agent-workforce/scorecard"))

from observability import (  # noqa: E402
    INVESTMENT_DEPARTMENT_STAGE,
    LangfuseApiTraceReader,
    LangfuseQueryError,
    LangfuseTraceReader,
    WindowedActivityReader,
    WorkerActivityPage,
    WorkerActivityRecord,
    check_idle_agents,
    collect_workforce_observability,
)

_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


class _CountingReader(LangfuseTraceReader):
    """왕복 없이 호출 횟수만 세는 대역. event_name 별 호출 수를 그대로 들고 있다."""

    def __init__(self) -> None:
        self.fetches: list[str] = []
        self.counts: list[str] = []

    def fetch_worker_activity(
        self, *, event_name: str, since: datetime, max_pages: int = 10
    ) -> WorkerActivityPage:
        self.fetches.append(event_name)
        record = WorkerActivityRecord(
            timestamp=since + timedelta(hours=1),
            latency_ms=1200, error_count=0, retries=0, attempts=1, status="SUCCESS",
            llm_calls=1, model_name="qwen2.5-14b-instruct-awq",
            prompt_tokens=800, completion_tokens=120,
        )
        return WorkerActivityPage(records=(record,), total_items=1, truncated=False)

    def count_worker_activity(self, *, event_name: str, since: datetime) -> int:
        self.counts.append(event_name)
        return 3

    # 실제 reader(LangfuseApiTraceReader)와 같은 모양 - 아래 셋은 위 두 원시함수로
    # 접힌다. 통합 전 구조를 재현하는 비교 테스트가 이 경로를 탄다.

    def latest_event_timestamp(self, *, event_name: str, since: datetime) -> datetime | None:
        page = self.fetch_worker_activity(event_name=event_name, since=since)
        return max((r.timestamp for r in page.records), default=None)

    def list_worker_activity(
        self, *, event_name: str, since: datetime, limit: int = 200
    ) -> list[WorkerActivityRecord]:
        return list(self.fetch_worker_activity(event_name=event_name, since=since).records)

    def count_events(self, *, event_name: str, since: datetime, limit: int = 200) -> int:
        return self.count_worker_activity(event_name=event_name, since=since)


class _FailingReader(LangfuseTraceReader):
    def __init__(self) -> None:
        self.attempts = 0

    def fetch_worker_activity(
        self, *, event_name: str, since: datetime, max_pages: int = 10
    ) -> WorkerActivityPage:
        self.attempts += 1
        raise LangfuseQueryError("simulated_query_failure")

    def count_worker_activity(self, *, event_name: str, since: datetime) -> int:
        self.attempts += 1
        raise LangfuseQueryError("simulated_query_failure")


def _registered_worker_count() -> int:
    return len(check_idle_agents(reader=None, now=_NOW))


def test_unified_collect_queries_langfuse_twice_per_worker() -> None:
    """Worker 당 왕복은 실행 이벤트 1 + 미발화 건수 1, 그 이상이면 통합이 풀린 것이다."""

    reader = _CountingReader()
    observed = collect_workforce_observability(reader=reader, now=_NOW)

    workers = _registered_worker_count()
    assert workers > 0, "등록된 Worker 가 없으면 이 테스트가 무의미해진다"

    # 실행 이벤트는 Worker 당 정확히 한 번만 읽는다 - 유휴 판정(최근 timestamp),
    # Capacity(지연·재시도), LLM 사용량(토큰·모델), 발화율의 분자가 전부 이 한 번에서
    # 나온다. 통합 전에는 이 자리에 4 * workers 가 찍혔다.
    assert len(reader.fetches) == workers
    assert len(set(reader.fetches)) == workers, "같은 이벤트를 두 번 읽었다"

    # 미발화 이벤트는 건수만 필요하다(레코드를 안 모은다).
    assert len(reader.counts) == workers
    assert set(reader.counts).isdisjoint(set(reader.fetches))

    assert observed.langfuse_queries == workers * 2


def test_unified_collect_is_cheaper_than_calling_four_checks_separately() -> None:
    """통합 전 구조(집계마다 reader 하나)와 왕복 수를 직접 비교한다."""

    workers = _registered_worker_count()
    separate = _CountingReader()
    # 통합 전에는 네 집계가 각자 reader 를 들고 같은 창을 다시 읽었다.
    from observability import (
        check_department_capacity,
        check_department_llm_usage,
        check_worker_trigger_rates,
    )

    check_idle_agents(reader=separate, now=_NOW)
    check_department_capacity(reader=separate, now=_NOW)
    check_department_llm_usage(reader=separate, now=_NOW)
    check_worker_trigger_rates(reader=separate, now=_NOW)
    before = len(separate.fetches) + len(separate.counts)

    shared = _CountingReader()
    collect_workforce_observability(reader=shared, now=_NOW)
    after = len(shared.fetches) + len(shared.counts)

    assert before == workers * 5, f"통합 전 왕복 가정이 깨졌다: {before}"
    assert after == workers * 2
    assert after < before


def test_shared_reader_serves_counts_from_an_already_fetched_window() -> None:
    """레코드를 이미 받아온 창이면 건수는 왕복 없이 나온다."""

    inner = _CountingReader()
    shared = WindowedActivityReader(inner)
    since = _NOW - timedelta(hours=24)

    shared.fetch_worker_activity(event_name="llm.performance.metric:research:w", since=since)
    count = shared.count_worker_activity(event_name="llm.performance.metric:research:w", since=since)

    assert count == 1, "총 건수는 fetch 가 받아온 total_items 여야 한다"
    assert inner.counts == [], "이미 읽은 창인데 건수를 다시 물어봤다"
    assert shared.queries == 1


def test_shared_reader_caches_failures_instead_of_retrying_per_aggregate() -> None:
    """죽은 Worker 하나를 네 집계가 각각 다시 묻지 않는다 - 장애 때 왕복이 되돌아간다."""

    inner = _FailingReader()
    shared = WindowedActivityReader(inner)
    since = _NOW - timedelta(hours=24)

    for _ in range(4):
        with pytest.raises(LangfuseQueryError):
            shared.fetch_worker_activity(event_name="llm.performance.metric:risk:w", since=since)

    assert inner.attempts == 1


def test_shared_reader_separates_windows() -> None:
    """창이 다르면 다른 관측이다 - 캐시가 창을 무시하면 낡은 값이 섞인다."""

    inner = _CountingReader()
    shared = WindowedActivityReader(inner)
    name = "llm.performance.metric:qa:w"

    shared.fetch_worker_activity(event_name=name, since=_NOW - timedelta(hours=24))
    shared.fetch_worker_activity(event_name=name, since=_NOW - timedelta(hours=24 * 7))

    assert shared.queries == 2


# ── 페이지네이션 ──────────────────────────────────────────────────────────────
#
# 통합 전 count_events() 는 len(page.data) 를 돌려줬다. 창 안에 limit(200) 이상이
# 쌓이면 실행·미발화 둘 다 200 으로 포화돼 fire_rate 가 실제와 무관하게 0.5 로
# 수렴한다 - 예외 없이 조용히 틀리는 종류의 실패다.


class _FakeMeta:
    def __init__(self, total_items: int, total_pages: int) -> None:
        self.total_items = total_items
        self.total_pages = total_pages


class _FakeTrace:
    def __init__(self, timestamp: datetime) -> None:
        self.timestamp = timestamp
        self.metadata = {"latency_ms": 10, "attempts": 1, "status": "SUCCESS"}


class _FakePage:
    def __init__(self, data: list[_FakeTrace], meta: _FakeMeta) -> None:
        self.data = data
        self.meta = meta


class _FakeTraceApi:
    """총 450건이 3페이지에 나뉘어 있는 서버."""

    TOTAL = 450

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def list(self, *, name, from_timestamp, limit, page):
        self.calls.append((limit, page))
        start = (page - 1) * limit
        size = max(0, min(limit, self.TOTAL - start))
        data = [_FakeTrace(from_timestamp + timedelta(minutes=i)) for i in range(start, start + size)]
        total_pages = (self.TOTAL + limit - 1) // limit
        return _FakePage(data, _FakeMeta(self.TOTAL, total_pages))


def _api_reader(trace_api: _FakeTraceApi) -> LangfuseApiTraceReader:
    """__init__ 은 LANGFUSE_* 자격증명을 요구하므로 우회해서 client 만 갈아 끼운다."""

    reader = object.__new__(LangfuseApiTraceReader)
    reader._client = type("_C", (), {"api": type("_A", (), {"trace": trace_api})()})()
    return reader


def test_fetch_pages_past_the_limit_instead_of_stopping_at_one_page() -> None:
    trace_api = _FakeTraceApi()
    page = _api_reader(trace_api).fetch_worker_activity(
        event_name="llm.performance.metric:research:w", since=_NOW - timedelta(hours=24)
    )

    assert page.total_items == _FakeTraceApi.TOTAL
    assert len(page.records) == _FakeTraceApi.TOTAL, "limit 한 장에서 끊겼다"
    assert page.truncated is False
    assert [c[1] for c in trace_api.calls] == [1, 2, 3]


def test_count_reads_total_items_in_one_round_trip() -> None:
    """건수는 meta 에서 온다 - 레코드를 안 모으므로 왕복 한 번이고 포화되지 않는다."""

    trace_api = _FakeTraceApi()
    count = _api_reader(trace_api).count_worker_activity(
        event_name="llm.performance.opportunity:research:w", since=_NOW - timedelta(hours=24)
    )

    assert count == _FakeTraceApi.TOTAL
    assert len(trace_api.calls) == 1
    assert trace_api.calls[0][0] == 1, "건수만 필요한데 페이지를 통째로 받아왔다"


def test_latest_timestamp_does_not_depend_on_server_ordering() -> None:
    """가장 최근 건이 첫 페이지에 없어도 ACTIVE 판정이 뒤집히지 않아야 한다."""

    trace_api = _FakeTraceApi()
    since = _NOW - timedelta(hours=24)
    latest = _api_reader(trace_api).latest_event_timestamp(
        event_name="llm.performance.metric:trading:w", since=since
    )

    # _FakeTraceApi 는 오래된 것부터 준다 - 마지막 페이지에 있는 최신 건을 집어야 한다.
    assert latest == since + timedelta(minutes=_FakeTraceApi.TOTAL - 1)


def test_investment_department_scope_is_unchanged_by_the_merge() -> None:
    """통합이 관측 범위를 조용히 바꾸지 않았는지 - 6개 투자본부 그대로다."""

    assert set(INVESTMENT_DEPARTMENT_STAGE) == {
        "research",
        "trading",
        "risk",
        "quant-backtest",
        "accounting-portfolio",
        "qa",
    }
