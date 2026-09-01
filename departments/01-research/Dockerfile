# 리서치 Container Image
#
# 담당: 재일 (리서치/퀀트)
# 근거: 재일님 지시(2026-07-31) "수집기들 Docker 컨테이너로 띄워 DB 즉시 적재"
#
# ▶ 하나의 Image, 여러 entrypoint. 기본 CMD 는 안전한 시세 스케줄러 자체점검이며
#   Compose가 명시적으로 실시간 수집기·배치 스케줄러·API를 선택한다.
#
# ▶ .env 는 Image 에 넣지 않는다. Credential 은 compose 의 environment 로만
#   주입한다 - load_project_env 가 "환경변수 우선" 이라 파일 없이 동작한다.
#
# ▶ 의존성은 셋뿐이다 - psycopg2(Repository), pydantic(source_registry 의 Source
#   계약), python-dotenv(.env 파싱 - Image 에는 .env 가 없지만 코드 경로가 있다).
#   버전은 루트 requirements.txt 선언(pydantic>=2, 나머지 미고정)을 따르되 Image
#   재현성을 위해 여기서 고정한다. 새 라이브러리를 늘리기 전에 CLAUDE.md 개발
#   원칙 8 을 본다.

FROM python:3.12-slim

# pyarrow 는 06:50 market-archive Job(Raw -> 검증된 Parquet) 전용이다 (DoD
# "Raw Market Data가 검증된 Parquet로 Archive된다", 2026-08-01 추가)
# mcp: 리서치본부 도구 면(api/mcp_server.py)을 Hermes 에 붙이는 다리.
# 버전 고정하지 않는 이유 - MCP SDK 가 빠르게 바뀌고 우리는 MCPServer/tool/
# streamable-http 라는 안정된 최소 표면만 쓴다. 깨지면 자체점검이 먼저 잡는다.
RUN pip install --no-cache-dir psycopg2-binary==2.9.10 pydantic==2.11.7 python-dotenv==1.1.1 websockets==15.0.1 fastapi==0.116.1 "uvicorn[standard]==0.35.0" pyarrow==25.0.0 mcp==1.26.0

# 저장소 루트 build context에서 departments/01-research 경로 구조를 그대로 유지한다 - 수집기들이
# Path(__file__) 상대로 contracts/repository 를 sys.path 에 넣기 때문이다.
WORKDIR /app/departments/01-research
# Runtime collector surface is an explicit allowlist.  Operational drills and
# destructive retention tools stay in Git but cannot be invoked from this
# image, and a newly added collector is not shipped until this list is reviewed.
COPY departments/01-research/collectors/collector_scheduler.py \
     departments/01-research/collectors/market_breadth_collector.py \
     departments/01-research/collectors/derivatives_collector.py \
     departments/01-research/collectors/chart_backfill_collector.py \
     departments/01-research/collectors/market_cap_universe_collector.py \
     departments/01-research/collectors/label_snapshot_collector.py \
     departments/01-research/collectors/volatility_index_collector.py \
     departments/01-research/collectors/style_index_collector.py \
     departments/01-research/collectors/calendar_collector.py \
     departments/01-research/collectors/universe_restriction_collector.py \
     departments/01-research/collectors/market_data_steward.py \
     departments/01-research/collectors/market_archive_exporter.py \
     departments/01-research/collectors/retention_enforcer.py \
     departments/01-research/collectors/ls_realtime_service.py \
     departments/01-research/collectors/ls_realtime_worker.py \
     departments/01-research/collectors/ls_realtime_adapter.py \
     departments/01-research/collectors/ls_unified_parser.py \
     departments/01-research/collectors/subscription_plan.py \
     departments/01-research/collectors/ls_client.py \
     departments/01-research/collectors/source_registry.py \
     departments/01-research/collectors/symbol_universe.py \
     departments/01-research/collectors/market_observations.py \
     ./collectors/
COPY departments/01-research/autonomous/ls_market_data.py ./autonomous/ls_market_data.py
# 거래제한 수집기는 이 결정론 모듈의 제한 사유/TR 정의와 pagination 함수만
# 재사용한다. 전체 agents/ 실행면을 이미지에 되살리지 않고 정확히 한 파일만
# allowlist에 포함한다.
COPY departments/01-research/agents/universe_manager.py ./agents/universe_manager.py
COPY departments/01-research/agents/sector_regime_analyst.py ./agents/sector_regime_analyst.py
COPY departments/01-research/contracts ./contracts
COPY departments/01-research/repository ./repository
COPY departments/01-research/api ./api
# market_api installs the repository's existing Tool Gateway at import time.
# Package the canonical profile config with the API so the gateway never
# silently falls back to NOT_INSTALLED in the standalone market-api image.
COPY departments/01-research/hermes ./hermes
# Shared read-only readiness coordination; use one implementation across
# Research, Quant, Risk, and QA.
COPY orchestration/readiness_cache.py /app/orchestration/readiness_cache.py
# Research evidence metrics use the existing credential-free tracing policy;
# package that canonical module instead of weakening the import or duplicating
# its redaction and sampling rules in this image.
COPY orchestration/llm_observability.py ./orchestration/llm_observability.py
# The tracing policy imports the shared egress circuit breaker. Keep the
# standalone Research image on that one implementation rather than adding a
# local copy of the flag logic.
COPY orchestration/langsmith_egress.py ./orchestration/langsmith_egress.py
# evidence/ 는 research-api /evidence/stories 가 story_cluster(결정론 군집)를
# import 하기 위해 필요하다 - 표준 라이브러리만 써서 의존성은 늘지 않는다
COPY departments/01-research/evidence ./evidence
# 나머지 퇴역 종목별 Packet 파이프라인(agents/, scripts.py)은 감사 계보로
# Git에만 남긴다. 현재 직원 Worker는 별도 MCP 이미지와 명시적
# employee_workers 마운트로만 실행된다.

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/app/departments/01-research:/app/departments/01-research/api

CMD ["python", "collectors/collector_scheduler.py", "--check"]
