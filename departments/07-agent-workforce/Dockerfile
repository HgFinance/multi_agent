# Agent Workforce 인사팀 Backend Image — workforce-api(FastAPI)와 improvement-worker(Redis
# Consumer)를 담는다.
#
# 담당: 영주 (Agent Workforce 인사팀)
# 근거: docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md
#       3.1절(부서당 Image 1개, command만 바꿔 역할 분리), 6.8절(Agent Workforce Container 목록),
#       11.1절(Image 기준), departments/03-risk/Dockerfile·departments/06-ai-qa-audit/Dockerfile 패턴
#
# Build context is the repository root so the image can receive the shared
# metadata-only Worker Registry contract and the bounded HR review bridge.
# Other department runtime modules remain excluded.
#
# ▶ 하나의 Image, command만 바꿔 workforce-api와 improvement-worker를 둘 다 돌린다
#   (research/risk와 동일 원칙). lifecycle-worker(계획서 6.8)는 아직 안 만들었다 -
#   승인 Event를 받아 Platform/IAM에 Provisioning을 요청한다는 책임인데, 그 Adapter의
#   이벤트 계약 자체가 아직 없다(Platform 담당자 선행 작업 필요, 인사팀이 대신 정하지
#   않는다 - CLAUDE.md 권한 경계).
#
# ▶ improvement-worker는 Redis Stream(hf:workforce)을 소비한다 - risk-api/audit-api와
#   같은 공용 `redis` 서비스에 연결한다(ADR-0002 Proposed 상태, 아직 부서별로 안 쪼갠다).
#   workforce-api(HTTP)는 Redis에 의존하지 않는다 - Access/Improvements/Scorecard 전부
#   Postgres(psycopg2)로만 응답한다.

FROM python:3.12-slim

# 버전은 이 저장소에서 이미 검증된 값(risk/audit Dockerfile과 동일 pin)으로 고정한다.
# langfuse: GET /workforce/v1/departments/idle-agents 의 read 측 SDK(2026-08-10, 993682f).
# 미설치도 LangfuseQueryError -> 전원 UNAVAILABLE 로 접히므로 크래시는 안 나지만,
# 그러면 이 이미지에서 관측이 영원히 UNAVAILABLE 이다 - 그래서 명시적으로 넣는다.
RUN pip install --no-cache-dir \
    fastapi==0.128.8 "uvicorn[standard]==0.39.0" pydantic==2.13.3 \
    psycopg2-binary==2.9.12 redis==7.0.1 langfuse==4.14.3

WORKDIR /app/departments/07-agent-workforce
COPY departments/07-agent-workforce/api ./api
COPY departments/07-agent-workforce/lifecycle ./lifecycle
COPY departments/07-agent-workforce/improvements ./improvements
COPY departments/07-agent-workforce/scorecard ./scorecard
COPY departments/07-agent-workforce/roster ./roster
COPY departments/07-agent-workforce/workforce_events ./workforce_events
# api/app.py가 sys.path 에 넣는 디렉터리는 lifecycle/improvements/scorecard/
# roster/planning/hiring/performance다. COPY 목록도 이 import root와 같이 움직인다.
# planning/hiring이 빠져 있었을 때
# `from hiring_request import ...` 가 죽고 workforce-api·improvement-worker 가
# 둘 다 크래시 루프였다(2026-08-12 실측). sys.path 목록과 이 COPY 목록은
# 같이 움직여야 한다.
COPY departments/07-agent-workforce/planning ./planning
COPY departments/07-agent-workforce/hiring ./hiring
COPY departments/07-agent-workforce/performance ./performance
# scorecard_brief imports the shared Markdown-cell renderer from the
# department root. A warm image masked this missing explicit dependency.
COPY departments/07-agent-workforce/reporting.py ./reporting.py
# improvement-worker는 root loader로 api/app.py를 고유 모듈명에 로드한다.
COPY departments/07-agent-workforce/workforce_api_loader.py ./workforce_api_loader.py

# HR Langfuse review uses the same redacted approval ledger as QA. Keep the
# copied surface narrow: these modules contain only bounded metadata/SQLite
# coordination and the Discord transport; no trace payload reader is added to
# the Workforce image.
COPY orchestration/__init__.py /app/orchestration/__init__.py
COPY orchestration/langsmith_queries.py /app/orchestration/langsmith_queries.py
COPY orchestration/langsmith_feedback.py /app/orchestration/langsmith_feedback.py
COPY orchestration/qa_discord_feedback.py /app/orchestration/qa_discord_feedback.py
COPY orchestration/hr_langfuse_feedback.py /app/orchestration/hr_langfuse_feedback.py

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY orchestration/contracts/worker_registry.py /app/orchestration/contracts/worker_registry.py
COPY orchestration/contracts/worker_registry.v1.json /app/orchestration/contracts/worker_registry.v1.json
CMD ["uvicorn", "app:app", "--app-dir", "api", "--host", "0.0.0.0", "--port", "8000"]
