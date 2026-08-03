# CEO Office Backend Image — governance-api(FastAPI)를 담는다.
#
# 담당: 영주 (CEO Office)
# 근거: docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md
#       3.1절(부서당 Image 1개, command만 바꿔 역할 분리), 6.1절(CEO Office Container 목록),
#       11.1절(Image 기준), departments/03-risk/Dockerfile·departments/06-ai-qa-audit/Dockerfile 패턴
#
# ▶ Build Context가 저장소 루트가 아니라 **departments/00-ceo-office** 다 (risk/audit
#   Dockerfile과 다른 점, research Dockerfile과 같은 이유). api/app.py는 src/mandate,
#   src/reporting, src/notification만 import하고 다른 본부 코드를 참조하지 않는다
#   (2026-08-03 확인 - contracts/apps/observability 등 교차 참조 없음).
#
# ▶ 하나의 Image, command만 바꿔 여러 프로세스를 돌리는 원칙(research/risk와 동일)은
#   유지하되 지금은 governance-api 하나뿐이다. reporting-worker/notification-worker
#   (계획서 6.1)는 넣지 않았다 - daily_report.py/notification.py는 순수 함수·서비스
#   클래스일 뿐 Event Consumer 루프가 아직 없다(risk-api Dockerfile의 risk-projection-worker
#   주석과 같은 이유). 코드 없는 역할을 컨테이너부터 만들지 않는다.
#
# ▶ Redis 미사용 - governance-api는 Mandate/Report/Notification을 Postgres(psycopg2)로만
#   다루고 Event Stream을 아직 발행·소비하지 않는다. REDIS_URL을 주입하지 않는다.

FROM python:3.12-slim

# 버전은 이 저장소에서 이미 검증된 값(risk/audit Dockerfile과 동일 pin)으로 고정한다.
RUN pip install --no-cache-dir \
    fastapi==0.128.8 "uvicorn[standard]==0.39.0" pydantic==2.13.3 \
    psycopg2-binary==2.9.12

WORKDIR /app/departments/00-ceo-office
COPY api ./api
COPY src ./src

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

CMD ["uvicorn", "app:app", "--app-dir", "api", "--host", "0.0.0.0", "--port", "8000"]
