# Agent Workforce 인사팀 Backend Image — workforce-api(FastAPI)와 improvement-worker(Redis
# Consumer)를 담는다.
#
# 담당: 영주 (Agent Workforce 인사팀)
# 근거: docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md
#       3.1절(부서당 Image 1개, command만 바꿔 역할 분리), 6.8절(Agent Workforce Container 목록),
#       11.1절(Image 기준), departments/03-risk/Dockerfile·departments/06-ai-qa-audit/Dockerfile 패턴
#
# ▶ Build Context가 저장소 루트가 아니라 **departments/07-agent-workforce** 다 (risk/audit
#   Dockerfile과 다른 점, research/CEO Dockerfile과 같은 이유). api/app.py는 lifecycle/
#   improvements/scorecard만 import하고 다른 본부 코드를 참조하지 않는다(2026-08-03 확인).
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
RUN pip install --no-cache-dir \
    fastapi==0.128.8 "uvicorn[standard]==0.39.0" pydantic==2.13.3 \
    psycopg2-binary==2.9.12 redis==7.0.1

WORKDIR /app/departments/07-agent-workforce
COPY api ./api
COPY lifecycle ./lifecycle
COPY improvements ./improvements
COPY scorecard ./scorecard
COPY workforce_events ./workforce_events

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

CMD ["uvicorn", "app:app", "--app-dir", "api", "--host", "0.0.0.0", "--port", "8000"]
