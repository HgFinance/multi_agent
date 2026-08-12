#!/usr/bin/env python3
"""배치 수집기 스케줄러 - Docker Container 의 entrypoint.

담당: 재일 (리서치/퀀트)
근거: 재일님 지시(2026-07-31) "다른 수집기들도 띄우자 / 계속 부족한 부분 개선"

▶ 왜 상주 서비스가 아니라 스케줄러인가
  공시·재무·CA·거시·기업개황·Calendar 는 하루 1~수십 회 실행이면 충분한 배치형이다.
  各各 컨테이너로 "떠 있게" 만들면 컨테이너 7개가 종일 잠만 잔다. 하나의 스케줄러가
  같은 Image 안의 수집기를 subprocess 로 돌린다.

▶ 지킨 것
  - **순차 실행.** 동시에 돌리지 않는다 - DART 계열이 같은 키·같은 Rate Limit 을
    공유하므로 병렬이면 서로를 429 로 민다.
  - **재시작 후 재실행은 안전하다.** 상태를 메모리에만 두므로 컨테이너 재시작이
    일일 Job 을 다시 돌릴 수 있는데, 모든 수집기가 멱등 적재라 중복이 생기지 않는다
    (그래서 상태 파일을 만들지 않았다 - 지금 볼륨 하나 늘리는 것보다 싸다).
  - **종료 코드 2 는 SKIP 이다.** market_breadth 가 휴장·수집 불가 국면에서 2 를
    돌려준다("수집하지 않았다"). 실패(1)와 섞으면 휴장일마다 거짓 경보가 쌓인다.
  - **연속 실패를 드러낸다.** Job 별 연속 실패 횟수를 세고 3회부터 요약에 ⚠ 를
    붙인다. 실패해도 스케줄러는 계속 돈다 - 한 Source 장애가 나머지를 멈추지 않는다.

▶ 여기 없는 것
  - watchlist_builder: LS t1444 에 호출 건수 제한(IGW00201)이 있고 결과 파일을
    호스트가 커밋 관리한다 - 주 1회 호스트에서 수동 실행.
  - 뉴스·실시간 시세: 전용 상주 컨테이너(news-watcher, ls-realtime)가 맡는다.

실행
  python collectors/collector_scheduler.py            # 자체 점검 (실행 없음)
  python collectors/collector_scheduler.py --check    # 같음 (명시형)
  python collectors/collector_scheduler.py --serve    # **상주 실행** (compose 가 쓴다)
  python collectors/collector_scheduler.py --once 이름  # 한 Job 즉시 1회 (수동 점검용)

  인자 없는 실행이 상주가 아닌 이유: 이 저장소는 "python <파일> = 자체 점검"이
  관례다. 이 파일만 예외라서, 전 모듈을 훑는 점검 루프가 돌 때마다 상주
  스케줄러가 조용히 떴다(2026-08-02까지 4회). 관례를 어기는 쪽이 플래그를
  달게 했다.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

SCHEDULER_VERSION = "research-collector-scheduler-v1"
KST = timezone(timedelta(hours=9))

CHECK_EVERY_SECONDS = 30.0
JOB_TIMEOUT_SECONDS = 30 * 60
FAILURE_ALERT_THRESHOLD = 3
EXIT_SKIP = 2  # 수집기 규약: "의도된 미수집" (market_breadth 실측)
TIMEOUT_EXIT = -1  # 타임아웃은 종료 코드를 못 받는다 - DB 기록용 표식


@dataclass(frozen=True)
class Job:
    """스케줄 한 줄. every 와 daily_at 중 하나만 갖는다."""

    name: str
    argv: tuple[str, ...]
    every_minutes: int | None = None
    window: tuple[time, time] | None = None  # 주기형의 활동 창 (KST)
    daily_at: time | None = None             # 일일형의 실행 시각 (KST)
    # ▶ Job 별 제한 시간. 기본 30분은 짧은 Job 을 지키는 값이라, 유니버스
    #   전량처럼 본래 몇 시간 걸리는 Job 에 그대로 걸면 **정상 동작이
    #   타임아웃으로 기록된다.** 예산은 Job 이 하는 일의 크기에서 나온다.
    timeout_seconds: float | None = None

    def __post_init__(self):
        if (self.every_minutes is None) == (self.daily_at is None):
            raise ValueError(f"{self.name}: every_minutes 와 daily_at 중 하나만 지정한다")
        if self.every_minutes is not None and self.window is None:
            raise ValueError(f"{self.name}: 주기형은 활동 창이 필수다 - 무제한 폴링을 막는다")

    @property
    def timeout(self) -> float:
        return self.timeout_seconds or JOB_TIMEOUT_SECONDS


# 시각은 전부 KST. DART 정기공시가 주로 장 마감 후에 몰리므로 일일 Job 은 저녁에 둔다.
JOBS: tuple[Job, ...] = (
    # 공시는 증분 폴링. 기본 범위 = 최근 3일(주말·야간 공시 누락 방지), 멱등이라
    # 겹쳐도 안전. --max-pages 30: 3일 창이 25페이지까지 나온 실측 + 여유
    Job("disclosure", ("collectors/opendart_collector.py", "--collect", "--max-pages", "30"),
        every_minutes=10, window=(time(7, 0), time(19, 0))),
    # 시장 Breadth - 세션 판정은 수집기 자신이 한다 (휴장이면 exit 2 = SKIP)
    Job("breadth", ("collectors/market_breadth_collector.py", "--collect"),
        every_minutes=10, window=(time(8, 30), time(16, 10))),
    # KOSPI200 파생 스냅샷 - 파생 세션(주식 ±15분)은 수집기가 판정, 밖이면 SKIP.
    # 창 상한 17:00: 수능일 파생 마감 16:45 까지 덮는다. 호출 4회/실행이라 가볍다.
    Job("derivatives", ("collectors/derivatives_collector.py", "--collect"),
        every_minutes=10, window=(time(8, 40), time(17, 0))),
    # 거래제한 종목 스냅샷 - **자격을 수집기에 가둔다.** 판정하는 쪽
    # (universe_manager)이 LS 를 직접 물면 파이프라인 컨테이너마다 LS 키가
    # 필요해진다(통합계획 6.2 위반). 07:05: 공시 폴링(07:00) 직후, 개장 전.
    Job("universe-restrictions",
        ("collectors/universe_restriction_collector.py", "--collect"),
        daily_at=time(7, 5)),
    # ▶ 일봉 증분 - **이게 스케줄에 없어서 7/31 이후 봉이 멈춰 있었다.**
    #   chart_backfill_collector 는 손으로 돌리는 백필 도구로만 있었고, 매일
    #   도는 자리가 없었다. 그 사이 리포트는 나흘 전 종가를 "최신" 으로 인용했고
    #   아무도 그것을 잡지 못했다(2026-08-04 재일님 지적).
    #   15:50: 정규장 마감(15:30) 직후. VKOSPI(16:05)·라벨 스냅샷(16:30)보다
    #   먼저 둬서 그날 봉이 뒤 배치의 재료가 되게 한다.
    #   최근 7일을 겹쳐 받는다 - PK 가 멱등이고, 연휴·장애로 며칠 빠져도
    #   다음 실행이 스스로 메운다(사람이 백필을 기억할 필요가 없어야 한다).
    Job("chart-daily",
        ("collectors/chart_backfill_collector.py", "--daily", "--recent-days", "7"),
        daily_at=time(15, 50)),
    # ▶ 일봉 **전량** - 위 15:50 Job 은 인자에 --universe 가 없어서 뉴스
    #   워치리스트 350종목만 받는다. 호가·체결은 2,595종목을 받는데 일봉이
    #   350종목이면 **유니버스가 어긋난다** - 마이크로구조에서 찾은 것을
    #   일봉으로 확인하려 해도 종목이 안 겹치고, 횡단면 전략은 표본 종목 수가
    #   곧 검정력이다. 2026-08-11 실측: 그날 일봉이 350종목만 있고 3,573종목이
    #   비어 있었다. 8/3~8/10 이 차 있던 것은 손으로 돌린 백필이었고, 사람이
    #   기억해야 채워지는 것은 채워지지 않는다.
    #
    #   15:50 자리를 넓히지 않고 Job 을 따로 두는 이유: 초당 1회 제한이라
    #   3,900종목이면 두 시간 가까이 걸린다. 스케줄러는 Job 을 **순차로**
    #   돌리므로 15:50 에 두 시간짜리를 걸면 VKOSPI(16:05)·라벨 스냅샷(16:30)
    #   같은 그날치 관측이 전부 뒤로 밀린다. 21:00 은 저녁 배치(document
    #   -archive 20:00)가 끝난 뒤이고, 시간외단일가(~18:00)까지 반영된 확정
    #   일봉을 받는 시각이다.
    #
    #   --recent-days 3: PK 가 멱등이라 겹쳐 받아도 안전하고, 하루 장애가 나도
    #   다음 실행이 스스로 메운다. 3일이면 주말·연휴 하나를 건넌다.
    Job("chart-daily-universe",
        ("collectors/chart_backfill_collector.py",
         "--daily", "--universe", "--recent-days", "3"),
        daily_at=time(21, 0), timeout_seconds=3 * 60 * 60),
    # 일별 라벨 스냅샷 - 레짐·지정학 판정을 그날의 사실로 남긴다. 16:30:
    # VKOSPI(16:05) 뒤라 그날 시장 상태가 다 반영된 시점. 이 이력이 있어야
    # Packet 의 REGIME_FLIP·GEO_ESCALATION 주장을 채점할 수 있다.
    # **사후 소급이 불가능하므로 매일 도는 것 자체가 자산이다.**
    Job("label-snapshot", ("collectors/label_snapshot_collector.py", "--collect"),
        daily_at=time(16, 30)),
    # VKOSPI 종가 - 정규장 마감(15:45) 뒤. 휴장이면 수집기가 SKIP(exit 2) 한다
    # (t1511 은 휴장에도 직전 종가를 주므로 그대로 넣으면 없는 관측이 생긴다).
    Job("vkospi", ("collectors/volatility_index_collector.py", "--collect"),
        daily_at=time(16, 5)),
    # 스타일·안전자산 지수 6계열 - VKOSPI 와 같은 t1511 경로, 같은 휴장 규칙.
    # 1분 뒤에 둬서 두 수집기가 같은 초에 LS 를 두드리지 않게 한다.
    Job("style-index", ("collectors/style_index_collector.py", "--collect"),
        daily_at=time(16, 6)),
    # 관측 Calendar 갱신 - 오늘 세션을 역산에 반영해 선언 Calendar 검증 폭을 늘린다
    Job("calendar-observed", ("collectors/calendar_collector.py", "--collect"),
        daily_at=time(16, 20)),
    # ▶ macro 는 내렸다 (2026-08-12, 재일님 결정 - "퀀트는 정량분석만, 정성분석은
    #   점수 팩터로")
    #
    #   하던 일: FRED·ECOS·KOSIS 를 매일 07:30 에 research.macro_observations 로.
    #
    #   무엇이 대신하는가: 거시는 이제 **정성 점수 팩터**의 재료다. 에이전트가
    #   MCP 로 조회해서 판정하고, 결정론 Python 이 점수로 환산해 적재한다
    #   (docs/02-engineering/QUALITATIVE_FACTOR_SPEC.md). 원계열을 통째로
    #   적재하지 않는다.
    #   MCP 후보: FRED(stefanoamorelli/fred-mcp-server, HTTP 지원·AGPL 주의),
    #             KOSIS(seolcoding/korean-stat-mcp, Python·MIT·HTTP).
    #   ⚠ ECOS(한국은행)는 단독 MCP 가 없다 - 직접 만들거나 계열을 포기해야 한다.
    #
    #   ⚠ 소비처가 살아 있다. 테이블은 남고 **갱신만 멈춘다**:
    #     - departments/04-quant-backtest/pipeline/data_resolution.py
    #       (macro_observations 를 observed_at PIT 소스로 등록)
    #     - departments/01-research/evidence/narrative_guard.py
    #     - supabase/migrations/20260731000900_public_dashboard_views.sql
    #     - departments/01-research/contracts/research_v2.py
    #   재일님 결정은 "어느 정도 재현 가능하면 MCP 로 충분하다"이다. 그 판단대로
    #   **원계열의 소급 재현은 포기**한다 - 점수 팩터는 forward-only 다.
    #   research-data-steward(07:15) 가 리서치 평면 DQ 를 보므로 계열이 굳는 것은
    #   거기서 드러나야 한다. 안 드러나면 그건 Steward 의 결함이다.
    #
    # Job("macro", ("collectors/macro_collector.py", "--collect"),
    #     daily_at=time(7, 30)),
    # 시세 평면 심박·품질 감사 - 개장 전, 밤 배치가 다 끝난 뒤 (FAIL 이면 exit 1
    # 로 스케줄러 로그에 남는다 - 개장 전에 눈에 띄는 것이 목적)
    Job("data-steward", ("collectors/market_data_steward.py", "--audit"),
        daily_at=time(7, 10)),
    # 리서치 평면 DQ - 시세 Steward(07:10)와 같은 목적, 다른 평면.
    # 07:15: 개장 전이고 밤 배치(문서·재무·거시)가 다 끝난 뒤다.
    # FAIL 이면 exit 1 로 스케줄러 로그에 남는다.
    Job("research-data-steward", ("collectors/research_data_steward.py", "--audit"),
        daily_at=time(7, 15)),
    # Raw -> 검증된 Parquet Archive (전일분 - 기본값이 데이터 있는 최근 거래일).
    # 06:50: 분봉 백필 등 밤 작업이 끝난 뒤, Steward(07:10)가 결과를 보기 전.
    Job("market-archive", ("collectors/market_archive_exporter.py", "--export"),
        daily_at=time(6, 50)),
    # ▶ 보존 집행 - **아카이브 뒤에 둔다** (재일님 지시 2026-08-12:
    #   "원시 db에서 하루 지나면 이전 parquet에 데이터 합치게")
    #
    #   retention_registry 는 2026-07-30 부터 있었는데 **집행하는 코드가
    #   없었다.** 그래서 디스크가 찰 때마다 사람이 손으로 drop_chunks 를
    #   돌렸고, 8/11 에는 "이 날이 Archive 됐나" 확인을 건너뛰어 호가가
    #   구멍난 채로 지워졌다.
    #
    #   07:10 인 이유: 06:50 아카이브가 끝난 뒤여야 그날 것이 verified 로
    #   올라와 있다. 순서가 뒤집히면 **아카이브 안 된 날을 지우려다 보류**만
    #   쌓이고 디스크는 안 준다(집행기가 막긴 하지만, 매일 헛돈다).
    #
    #   스케줄러는 Job 을 순차로 도므로 06:50 이 길어져도 07:10 이 앞서지
    #   않는다 - 시각이 아니라 순서가 보장한다.
    Job("retention", ("collectors/retention_enforcer.py", "--enforce"),
        daily_at=time(7, 10)),
    # ▶ 상한을 모수보다 크게 (2026-08-03 실측)
    #   corp_code 보유 발행사가 **1,315** 인데 재무 1200·CA 400 이었다.
    #   특히 CA 는 400 이라 매일 900개사가 통째로 빠졌다 - 상한이 모수보다
    #   작으면 꼬리가 영원히 안 돌고, 그 사실이 어디에도 안 드러난다.
    #   DART 일 한도 20,000 대비 현금흐름 2,595 + 재무 배치 + CA 1,500 이면
    #   25% 수준이라 여유가 있다.
    # --limit 명시: CLI 기본값(재무 20, CA 40)은 프로브용이라 스케줄이 그대로 쓰면
    # 발행사 1,049곳 중 꼬리만 돌게 된다 (2026-07-31 점검에서 발견).
    # 재무는 corp_code 콤마 배치 조회라 1,200 이어도 호출 수십 회다.
    Job("financial", ("collectors/opendart_financial.py", "--collect", "--limit", "1500"),
        daily_at=time(18, 10)),
    # 전종목 바스켓 재생성 - 제한 스냅샷(07:05) 뒤라 그날의 정지·관리 종목이
    # 반영된다. 파일은 config 에 쓰므로 이 Job 은 호스트 권한이 필요하다 -
    # 컨테이너 config 는 읽기 전용이라 실패한다(알려진 제약, 백로그).
    # 현금흐름표 - F-Score 를 6/9 에서 9/9 로 올리는 재료. 회사당 1호출이라
    # 감시 바스켓(400)만 받는다. 18:50: 재무(18:10) 뒤, CA(18:30) 사이 여유.
    Job("cashflow", ("collectors/opendart_cashflow.py", "--collect", "--limit", "3000"),
        daily_at=time(18, 50)),
    Job("corporate-action", ("collectors/corporate_action_collector.py", "--collect", "--limit", "1500"),
        daily_at=time(18, 30)),
    # ▶ document-archive 는 내렸다 (2026-08-12, 재일님 결정)
    #
    #   하던 일: 당일 공시 원본 ZIP 을 Private Storage 로 적재하고 research.documents
    #   본문을 채웠다. 저녁 20:00, 하루 600건.
    #
    #   왜 내리는가 - **용량이다.** 실측 DB 937MB 중
    #     research.evidence_chunks  586MB (43,404행)  ← 원문에서 파생된 RAG 청크
    #     research.documents         78MB (138,019행)
    #   evidence_chunks 가 큰 이유는 문서 수가 아니라 1,536차원 임베딩 + HNSW
    #   인덱스다(행당 ~14KB). **원문이 안 들어오면 청크도 안 생긴다** - 신규
    #   유입을 끊는 지점이 여기 하나뿐이라 이 Job 이 대상이 됐다.
    #
    #   무엇이 대신하는가: DART MCP 의 `get_attachments`/`download_document` 를
    #   에이전트가 **필요할 때만** 부른다. 전량 선적재 -> 요청시 조회로 바뀐다.
    #   기준과 팩터 설계는 docs/02-engineering/QUALITATIVE_FACTOR_SPEC.md.
    #
    #   ⚠ 남아 있는 것 - 착각하지 말 것
    #     - 이미 쌓인 evidence_chunks 586MB 는 그대로다. retention 으로 따로 정리한다.
    #     - 공시 **목록**(`disclosure` Job)은 안 내린다. 10분 주기 장중 감지라
    #       실시간성이 있고, QF-05(정정 빈도)의 재료이기도 하다.
    #     - 재무·현금흐름도 안 내린다. F-Score 8/9 와 Altman Z 가 그 위에 있다.
    #
    #   되돌리려면: 아래 Job 을 되살리면 된다. 수집기 파일
    #   (collectors/opendart_document_collector.py) 은 지우지 않았다 -
    #   MCP 대체가 실측으로 검증되기 전까지 남겨 둔다.
    #
    # Job("document-archive", ("collectors/opendart_document_collector.py", "--collect", "--limit", "600"),
    #     daily_at=time(20, 0)),
    # 개황이 빈 issuer 보강 (전량 보강은 완료 - 이후는 신규 필러 몫).
    # 300: 공시 백필 하루치가 신규 143 corp 를 만든 실측(2026-07-31) + 여유.
    # 2건/초 제한이라 300개 = 2.5분이면 끝난다.
    Job("company-profile", ("collectors/opendart_company_collector.py", "--collect", "--limit", "300"),
        daily_at=time(19, 0)),
    # 역량 격차 감사 - "쓸 수 있는데 안 쓰는 것"을 스스로 찾는다. 매일 07:40
    # (지정학 07:20 뒤). LS 호출 1회 + 질의 몇 개라 가볍다. 발견만 하고
    # 채택은 사람이 한다 - 리포트가 reports/capability_audit_*.md 로 남는다.
    Job("capability-audit", ("collectors/capability_audit.py", "--audit"),
        daily_at=time(7, 40)),
    # Packet 사후 채점 - 선순환의 되먹임. 지평(5·20 거래일)이 지난 Packet 을
    # 시세로 대조해 research.packet_outcomes 에 남긴다. 18:00: 당일 종가가
    # 확정되고 밤 배치가 시작되기 전.
    Job("packet-outcome", ("collectors/packet_outcome_scorer.py", "--score"),
        daily_at=time(18, 0)),
    # ▶ geopolitical 은 내렸다 (2026-08-12, 재일님 결정 - macro 와 같은 이유)
    #
    #   하던 일: GPR 일별 지수 + GDELT 테마 보도량·톤을 매일 07:20 에 적재.
    #
    #   무엇이 대신하는가: 지정학도 **정성 점수 팩터**로 간다. 원계열 120일치를
    #   매일 다시 받아 쌓는 대신, 에이전트가 조회해 판정하고 점수만 남긴다.
    #
    #   ⚠ departments/01-research/agents/geopolitical_analyst.py (37KB) 가
    #     이 테이블에 전적으로 의존한다. 계열이 굳으면 그 분석가의 출력도
    #     굳는다 - 팩터 경로가 서기 전까지는 그 분석가를 "최신"으로 읽으면 안 된다.
    #     config/geopolitical_themes.txt 의 테마 정의는 팩터 설계에 그대로 쓴다.
    #
    # Job("geopolitical", ("collectors/geopolitical_collector.py", "--collect",
    #                      "--days", "120"),
    #     daily_at=time(7, 20)),
    # Bluesky 미국 표적 계정 (기관 미디어 4곳 + 매크로 논객 2인, ~166건/일 실측).
    # 60분 주기면 계정당 피드 50건 버퍼가 최고 볼륨(Reuters ~57건/일)도 20시간
    # 이상 덮는다 - 놓칠 수 없는 구조. 창 06:00~23:50: 미 장중(KST 밤)은 다음날
    # 아침 첫 폴링이 버퍼로 회수하므로 새벽 상주가 필요 없다.
    Job("bluesky-watch", ("collectors/bluesky_watch_collector.py", "--collect"),
        every_minutes=60, window=(time(6, 0), time(23, 50))),
)


@dataclass
class JobState:
    last_started: datetime | None = None
    last_finished_date: date | None = None  # 일일형 중복 방지
    consecutive_failures: int = 0
    runs: int = 0
    skips: int = 0


def is_due(job: Job, state: JobState, now: datetime) -> bool:
    if job.daily_at is not None:
        if now.timetz().replace(tzinfo=None) < job.daily_at:
            return False
        return state.last_finished_date != now.date()
    lo, hi = job.window
    t = now.timetz().replace(tzinfo=None)
    if not (lo <= t <= hi):
        return False
    if state.last_started is None:
        return True
    return (now - state.last_started) >= timedelta(minutes=job.every_minutes)


def run_job(job: Job, *, timeout: float | None = None) -> tuple[int, str]:
    """subprocess 로 한 번 실행. (종료 코드, 출력 꼬리)."""
    proc = subprocess.run(
        [sys.executable, *job.argv],
        check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout if timeout is not None else job.timeout,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    tail = "\n".join((proc.stdout or "").strip().splitlines()[-6:])
    if proc.returncode not in (0, EXIT_SKIP) and proc.stderr:
        tail += "\nstderr: " + (proc.stderr or "").strip()[-400:]
    return proc.returncode, tail


def classify(exit_code: int) -> str:
    """종료 코드 -> 상태. SKIP 은 실패가 아니다(휴장 등 의도된 미수집)."""
    if exit_code == 0:
        return "OK"
    if exit_code == EXIT_SKIP:
        return "SKIP"
    if exit_code == TIMEOUT_EXIT:
        return "TIMEOUT"
    return "FAILED"


def record_run(job_name: str, argv: tuple[str, ...], *, started, ended,
               exit_code: int, tail: str, consecutive_failures: int,
               conn=None) -> bool:
    """research.collector_runs 기록.

    **기록 실패가 수집을 죽이면 안 된다** - 조용한 실패를 막으려고 만든 장치가
    새로운 실패 원인이 되면 본말전도다. 실패는 stderr 로만 알린다.
    """
    try:
        own = conn is None
        if own:
            import psycopg2
            from source_registry import load_project_env

            conn = psycopg2.connect(load_project_env()["DATABASE_URL"],
                                    connect_timeout=10)
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into research.collector_runs
                  (job_name, argv, started_at, ended_at, duration_ms, exit_code,
                   status, consecutive_failures, output_tail, scheduler_version)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (job_name, " ".join(argv), started, ended,
                 int((ended - started).total_seconds() * 1000), exit_code,
                 classify(exit_code), consecutive_failures,
                 (tail or "")[:4000], SCHEDULER_VERSION))
        conn.commit()
        if own:
            conn.close()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ collector_runs 기록 실패({job_name}): {type(e).__name__}: "
              f"{str(e)[:120]}", file=sys.stderr, flush=True)
        return False


def seed_states_from_history(states: dict[str, JobState], *, conn=None,
                             today: date | None = None) -> int:
    """재시작 뒤 **오늘 이미 끝낸 일일 Job 을 다시 돌리지 않게** 원장에서 복원한다.

    ▶ 왜 필요한가 (2026-08-11)
      JobState 는 메모리에만 있었다. 컨테이너를 한 번 올리면 그 시각 이전의
      daily_at Job 이 **전부 다시 돈다** - is_due 가 `last_finished_date !=
      오늘` 로 판정하는데, 갓 만든 상태는 None 이라 언제나 참이다.
      저녁에 재시작하면 그날 일일 Job 열몇 개가 한꺼번에 재실행되고,
      DART·GDELT·LS 를 다시 두드린다(GDELT 는 실제로 429 를 준다).
      멱등이라 데이터는 안 깨지지만, **재시작이 비싸지면 고치는 것을 미루게
      된다.** 배포를 겁내게 만드는 것 자체가 결함이다.

      복원 실패는 수집을 죽이지 않는다 - 못 읽었으면 예전처럼 다 도는 것뿐이라
      안전한 쪽으로 떨어진다. 다만 조용히 넘기지 않고 stderr 로 알린다.

    OK/SKIP 만 '끝냈다'로 친다. 실패한 Job 은 재시작이 곧 재시도여야 한다.
    """
    today = today or datetime.now(KST).date()
    # 주기형은 복원하지 않는다 - is_due 가 last_started 로 판정하고, 재시작
    # 직후 한 번 더 도는 것이 오히려 맞다(10분 폴링은 겹쳐도 싸다).
    daily = [j.name for j in JOBS if j.daily_at is not None and j.name in states]
    if not daily:
        return 0
    try:
        own = conn is None
        if own:
            import psycopg2
            from source_registry import load_project_env

            conn = psycopg2.connect(load_project_env()["DATABASE_URL"],
                                    connect_timeout=10)
        with conn.cursor() as cur:
            cur.execute(
                """
                select job_name, max((ended_at at time zone 'Asia/Seoul')::date)
                  from research.collector_runs
                 where job_name = any(%s)
                   and status in ('OK','SKIP')
                   and ended_at >= now() - interval '48 hours'
                 group by job_name
                """,
                (daily,))
            rows = cur.fetchall()
        if own:
            conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ 실행 이력 복원 실패 - 오늘 일일 Job 이 다시 돈다: "
              f"{type(e).__name__}: {str(e)[:120]}", file=sys.stderr, flush=True)
        return 0
    seeded = 0
    for name, last in rows:
        if last == today and name in states:
            states[name].last_finished_date = last
            seeded += 1
    return seeded


def main() -> int:
    stop = threading.Event()

    def _handle(signum, _frame):
        print(f"신호 {signum} 수신 - 진행 중인 Job 을 마치고 종료한다", flush=True)
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    states: dict[str, JobState] = {j.name: JobState() for j in JOBS}
    seeded = seed_states_from_history(states)
    print(f"{SCHEDULER_VERSION}: Job {len(JOBS)}개"
          f" (오늘 이미 끝난 일일 Job {seeded}개는 건너뛴다)", flush=True)
    for j in JOBS:
        when = (f"{j.every_minutes}분마다 {j.window[0]:%H:%M}~{j.window[1]:%H:%M}"
                if j.every_minutes else f"매일 {j.daily_at:%H:%M}")
        done = " [오늘 완료]" if states[j.name].last_finished_date else ""
        print(f"  {j.name:<21} {when}{done}  <- {' '.join(j.argv)}", flush=True)

    while not stop.is_set():
        now = datetime.now(KST)
        for job in JOBS:
            if stop.is_set():
                break
            st = states[job.name]
            if not is_due(job, st, now):
                continue
            st.last_started = datetime.now(KST)
            try:
                code, tail = run_job(job)
            except subprocess.TimeoutExpired:
                st.consecutive_failures += 1
                print(f"[{datetime.now(KST):%H:%M}] {job.name}: ⚠ TIMEOUT "
                      f"({job.timeout / 60:.0f}분, 연속 {st.consecutive_failures})",
                      flush=True)
                # 타임아웃도 기록한다 - 로그에만 남기면 재시작과 함께 사라진다
                record_run(job.name, job.argv, started=st.last_started,
                           ended=datetime.now(KST), exit_code=TIMEOUT_EXIT,
                           tail=f"{job.timeout / 60:.0f}분 초과",
                           consecutive_failures=st.consecutive_failures)
                continue
            st.runs += 1
            st.last_finished_date = datetime.now(KST).date()
            # 상태 갱신 뒤 기록해야 consecutive_failures 가 맞다 - 아래 분기에서
            # 0 으로 초기화되거나 증가한 값을 그대로 남긴다.
            if code == 0:
                st.consecutive_failures = 0
                last = tail.splitlines()[-1] if tail else ""
                print(f"[{datetime.now(KST):%H:%M}] {job.name}: OK  {last}", flush=True)
            elif code == EXIT_SKIP:
                st.consecutive_failures = 0
                st.skips += 1
                print(f"[{datetime.now(KST):%H:%M}] {job.name}: SKIP (수집기 판단)", flush=True)
            else:
                st.consecutive_failures += 1
                mark = " ⚠" if st.consecutive_failures >= FAILURE_ALERT_THRESHOLD else ""
                print(f"[{datetime.now(KST):%H:%M}] {job.name}: 실패 exit={code} "
                      f"(연속 {st.consecutive_failures}){mark}\n{tail}", flush=True)
            record_run(job.name, job.argv, started=st.last_started,
                       ended=datetime.now(KST), exit_code=code, tail=tail,
                       consecutive_failures=st.consecutive_failures)
        stop.wait(CHECK_EVERY_SECONDS)

    print("종료: " + ", ".join(
        f"{n}={s.runs}회(skip {s.skips})" for n, s in states.items() if s.runs
    ), flush=True)
    return 0


# ---------------------------------------------------------------------------
# 자체 점검 - 실행 없이
# ---------------------------------------------------------------------------

def _check_job_table():
    names = [j.name for j in JOBS]
    assert len(names) == len(set(names)), "Job 이름이 겹친다"
    for j in JOBS:
        assert Path(__file__).resolve().parent.parent.joinpath(j.argv[0]).exists(), \
            f"{j.name}: {j.argv[0]} 이 없다"
        # 실행 플래그가 없으면 수집기 규약상 자체점검만 돌게 된다.
        # --collect(수집) / --audit(Steward 감사) / --export(Archive) /
        # --score(Packet 사후 채점) 가 실행 동사다.
        assert {"--collect", "--audit", "--export", "--score",
                "--daily", "--minute", "--enforce"} & set(j.argv), \
            f"{j.name}: 실행 플래그가 없다 - 자체점검만 돈다"
    # ▶ **아카이브가 보존 집행보다 앞서야 한다** (2026-08-12)
    #   순서가 뒤집히면 그날 것이 아직 verified 가 아니라, 집행기가 전부
    #   보류로 넘긴다. 막히긴 하지만 매일 헛돌고 디스크는 안 준다.
    order = {j.name: i for i, j in enumerate(JOBS)}
    if "retention" in order and "market-archive" in order:
        assert order["market-archive"] < order["retention"], \
            "보존 집행이 아카이브보다 먼저다 - 아카이브 안 된 날을 지우려 한다"
    try:
        Job("bad", ("x.py", "--collect"))
        raise AssertionError("every/daily 둘 다 없는 Job 이 통과했다")
    except ValueError:
        pass
    try:
        Job("bad2", ("x.py", "--collect"), every_minutes=10)
        raise AssertionError("활동 창 없는 주기형이 통과했다")
    except ValueError:
        pass
    print("  Job 목록 무결성          OK")


def _check_due_logic():
    kst = lambda h, m=0: datetime(2026, 7, 31, h, m, tzinfo=KST)
    periodic = Job("p", ("x.py", "--collect"), every_minutes=10,
                   window=(time(9, 0), time(15, 0)))
    st = JobState()
    assert not is_due(periodic, st, kst(8, 59)), "창 밖에서 돌았다"
    assert is_due(periodic, st, kst(9, 0))
    st.last_started = kst(9, 0)
    assert not is_due(periodic, st, kst(9, 5)), "주기 전에 다시 돌았다"
    assert is_due(periodic, st, kst(9, 10))
    assert not is_due(periodic, st, kst(15, 1)), "창이 닫혔는데 돌았다"

    daily = Job("d", ("x.py", "--collect"), daily_at=time(18, 0))
    st2 = JobState()
    assert not is_due(daily, st2, kst(17, 59))
    assert is_due(daily, st2, kst(18, 0))
    st2.last_finished_date = date(2026, 7, 31)
    assert not is_due(daily, st2, kst(23, 0)), "같은 날 두 번 돌았다"
    # 빈 상태는 다시 due 다. 이건 is_due 의 결함이 아니라 **상태를 안 준
    # 쪽의 문제**라, 재시작 경로는 seed_states_from_history 로 메운다
    # (아래 _check_seed_skips_finished_daily_jobs).
    st3 = JobState()
    assert is_due(daily, st3, kst(23, 0))
    print("  due 판정                 OK")


class _FakeCursor:
    def __init__(self, rows): self._rows = rows
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, _sql, _params=None): pass
    def fetchall(self): return self._rows


class _FakeConn:
    def __init__(self, rows): self._rows = rows
    def cursor(self): return _FakeCursor(self._rows)


def _check_seed_skips_finished_daily_jobs():
    """재시작이 **그날 이미 끝낸 일일 Job 을 다시 돌리지 않는가.**

    이걸 안 하면 저녁 배포 한 번에 DART·GDELT·LS 를 하루치 더 두드린다.
    멱등이라 데이터는 안 깨지지만, 재시작이 비싸지면 고치는 것을 미루게 된다.
    """
    today = date(2026, 8, 11)
    yesterday = date(2026, 8, 10)
    states = {j.name: JobState() for j in JOBS}
    # ▶ **Job 이름을 박지 않는다** (2026-08-12)
    #   예전엔 `macro` 를 표본으로 썼는데, 다른 세션이 그 Job 을 내리자 점검이
    #   `KeyError` 로 죽었다. 편제가 바뀔 때마다 점검이 깨지면 사람이 점검을
    #   고치는 대신 지운다. 조건(21:30 이전에 도는 일일 Job)에 맞는 것을 고른다.
    at_2130 = datetime(2026, 8, 11, 21, 30, tzinfo=KST)
    daily = [j for j in JOBS
             if j.daily_at is not None and j.daily_at <= at_2130.timetz().replace(tzinfo=None)]
    assert len(daily) >= 2, "일일 Job 이 둘은 있어야 이 점검이 성립한다"
    done_job, stale_job = daily[0].name, daily[1].name

    n = seed_states_from_history(
        states,
        conn=_FakeConn([(done_job, today),        # 오늘 끝냄 -> 건너뛴다
                        (stale_job, yesterday),   # 어제 것 -> 오늘은 돈다
                        ("no-such-job", today)]), # 사라진 Job -> 무시
        today=today)
    assert n == 1, f"복원 수가 틀리다: {n}"
    assert states[done_job].last_finished_date == today
    assert states[stale_job].last_finished_date is None

    by_name = {j.name: j for j in JOBS}
    assert not is_due(by_name[done_job], states[done_job], at_2130), \
        "오늘 끝낸 Job 이 재시작 뒤 또 돈다"
    assert is_due(by_name[stale_job], states[stale_job], at_2130), \
        "어제가 마지막인 Job 은 오늘 돌아야 한다"

    # 주기형은 복원 대상이 아니다 - 재시작 직후 한 번 더 도는 것이 맞다
    assert states["disclosure"].last_finished_date is None

    # 원장을 못 읽어도 수집은 계속된다 (예전처럼 다 도는 쪽 = 안전한 실패)
    class _Boom:
        def cursor(self): raise RuntimeError("DB 없음")
    assert seed_states_from_history({j.name: JobState() for j in JOBS},
                                    conn=_Boom(), today=today) == 0
    print("  재시작 중복 실행 차단    OK")


def _check_job_timeout_budget():
    """유니버스 전량처럼 오래 걸리는 Job 에 기본 30분을 걸면 **정상이 타임아웃으로 기록된다.**

    초당 1회 제한 × 3,900종목 = 65분 이상이다. 예산은 Job 이 하는 일에서 나온다.
    """
    by_name = {j.name: j for j in JOBS}
    assert by_name["disclosure"].timeout == JOB_TIMEOUT_SECONDS, "기본값이 안 붙는다"
    uni = by_name["chart-daily-universe"]
    assert "--universe" in uni.argv, "전량 Job 인데 --universe 가 없다"
    assert uni.timeout >= 65 * 60, \
        f"초당 1회 × 3,900종목을 못 담는 예산이다: {uni.timeout / 60:.0f}분"

    # 순차 실행이라 긴 Job 은 다른 일일 Job 뒤에 와야 한다 - 그날치 관측
    # (VKOSPI·라벨 스냅샷)이 밀리면 사후 소급이 안 된다.
    later_than = [j.name for j in JOBS
                  if j.daily_at is not None and j is not uni
                  and j.daily_at > uni.daily_at]
    assert not later_than, f"전량 Job 뒤에 일일 Job 이 남아 밀린다: {later_than}"
    print("  Job 제한 시간 예산       OK")


def _check_exit_codes():
    assert EXIT_SKIP == 2, "market_breadth 의 '수집하지 않았다' 규약과 다르다"
    print("  종료 코드 규약           OK")


def _check_status_classification():
    """SKIP 을 실패로 세면 휴장마다 경보가 울려 아무도 안 보게 된다."""
    assert classify(0) == "OK"
    assert classify(EXIT_SKIP) == "SKIP"
    assert classify(1) == "FAILED"
    assert classify(TIMEOUT_EXIT) == "TIMEOUT"
    assert classify(255) == "FAILED"
    print("  실행 상태 분류           OK")


def _check_record_failure_is_contained():
    """기록 실패가 수집을 죽이면 안 된다 - 조용한 실패를 막는 장치가 새로운
    실패 원인이 되면 본말전도다."""
    class _Boom:
        def cursor(self):
            raise RuntimeError("DB down")

    now = datetime.now(KST)
    ok = record_run("t", ("x.py", "--collect"), started=now, ended=now,
                    exit_code=0, tail="", consecutive_failures=0, conn=_Boom())
    assert ok is False, "기록 실패를 성공으로 보고했다"
    print("  기록 실패 격리           OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--check" in sys.argv:
        print(f"{SCHEDULER_VERSION} 자체 점검 (실행 없음)")
        _check_job_table()
        _check_due_logic()
        _check_exit_codes()
        _check_status_classification()
        _check_record_failure_is_contained()
        _check_seed_skips_finished_daily_jobs()
        _check_job_timeout_budget()
        print("스케줄러 7개 영역 통과. 상주 실행은 --serve")
        raise SystemExit(0)

    if "--once" in sys.argv:
        name = sys.argv[sys.argv.index("--once") + 1]
        matches = [j for j in JOBS if j.name == name]
        if not matches:
            print(f"모르는 Job 이다: {name} (가능: {', '.join(j.name for j in JOBS)})")
            raise SystemExit(1)
        code, tail = run_job(matches[0])
        print(tail)
        print(f"exit={code}")
        raise SystemExit(0 if code in (0, EXIT_SKIP) else code)

    # ▶ 상주 기동은 **명시적으로만** (2026-08-02)
    #   예전에는 인자가 없으면 곧장 상주 루프였다. 그런데 이 저장소의 관례는
    #   "python <파일>" = 자체 점검이라, 전 모듈을 훑는 점검 루프가 이 파일을
    #   만날 때마다 스케줄러가 조용히 떴다(네 번 겪었다 - 그때마다 사람이
    #   알아채고 정리해야 했다). 관례를 지키는 쪽이 아니라 **어기는 쪽이
    #   비용을 내야** 한다: 상주는 --serve 로만, 인자 없으면 점검이다.
    if "--serve" in sys.argv:
        raise SystemExit(main())

    print(f"{SCHEDULER_VERSION}: 인자가 없으면 자체 점검만 한다 "
          f"(상주 기동은 --serve, 단발 실행은 --once <job>)")
    print(f"{SCHEDULER_VERSION} 자체 점검 (실행 없음)")
    _check_job_table()
    _check_due_logic()
    _check_exit_codes()
    _check_status_classification()
    _check_record_failure_is_contained()
    print("스케줄러 5개 영역 통과. 상주 실행은 --serve")
    raise SystemExit(0)
