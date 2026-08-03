# CEO Office Backend Image — governance-api(FastAPI)와 notification-worker(Redis Consumer)를 담는다.
#
# 담당: 영주 (CEO Office)
# 근거: docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md
#       3.1절(부서당 Image 1개, command만 바꿔 역할 분리), 6.1절(CEO Office Container 목록),
#       11.1절(Image 기준), departments/03-risk/Dockerfile·departments/06-ai-qa-audit/Dockerfile 패턴
#
# ▶ Build Context가 저장소 루트가 아니라 **departments/00-ceo-office** 다 (risk/audit
#   Dockerfile과 다른 점, research Dockerfile과 같은 이유). api/app.py는 src/mandate,
#   src/reporting, src/notification만 import하고 다른 본부 코드를 참조하지 않는다.
#
# ▶ 하나의 Image, command만 바꿔 governance-api와 notification-worker를 둘 다 돌린다
#   (research/risk와 동일 원칙). reporting-worker/lifecycle-worker류는 아직 안 만들었다 -
#   daily_report.py는 여러 이벤트에 걸친 상태 누적(부분 Snapshot 조합)이 필요해 순수 함수
#   호출만으로는 안 되고, 그 누적 로직 자체가 아직 없다(코드 없는 역할을 컨테이너부터 만들지
#   않는다).
#
# ▶ notification-worker는 Redis Stream(hf:governance)을 소비한다 - risk-api/audit-api와
#   같은 공용 `redis` 서비스에 연결한다(ADR-0002 Proposed 상태, 아직 부서별로 안 쪼갠다).
#   governance-api(HTTP)는 Redis에 의존하지 않는다 - Mandate/Report/Notification 전부
#   Postgres(psycopg2)로만 응답한다.

FROM python:3.12-slim

# 버전은 이 저장소에서 이미 검증된 값(risk/audit Dockerfile과 동일 pin)으로 고정한다.
RUN pip install --no-cache-dir \
    fastapi==0.128.8 "uvicorn[standard]==0.39.0" pydantic==2.13.3 \

    psycopg2-binary==2.9.12 redis==7.0.1


WORKDIR /app/departments/00-ceo-office
COPY api ./api
COPY src ./src
COPY governance_events ./governance_events

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

CMD ["uvicorn", "app:app", "--app-dir", "api", "--host", "0.0.0.0", "--port", "8000"]
