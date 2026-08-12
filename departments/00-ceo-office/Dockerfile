# CEO Office Backend Image — governance-api(FastAPI)와 notification-worker(Redis Consumer)를 담는다.
#
# 담당: 영주 (CEO Office)
# 근거: docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md
#       3.1절(부서당 Image 1개, command만 바꿔 역할 분리), 6.1절(CEO Office Container 목록),
#       11.1절(Image 기준), departments/03-risk/Dockerfile·departments/06-ai-qa-audit/Dockerfile 패턴
#
# ▶ Build Context가 **저장소 루트**다 (risk-api Dockerfile과 같은 이유, 2026-08-10 변경 -
#   change_workflow.py가 저장소 루트의 orchestration.contracts를 참조하면서 departments/
#   00-ceo-office만으로는 이미지를 완성할 수 없어졌다). compose에서는 build.context: ../..
#   (departments/00-ceo-office/compose.yaml 기준) / build.dockerfile: departments/00-ceo-office/
#   Dockerfile. api/app.py의 `_BASE = Path(__file__).resolve().parent.parent` 계산은
#   Build Context가 아니라 `__file__` 기준이라 이 변경에 영향받지 않는다 - 디렉터리 트리를
#   이미지 안에서도 저장소와 같은 상대 구조로 유지하기만 하면 된다.
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
    psycopg2-binary==2.9.12 redis==7.0.1 \
    langgraph==0.6.11 langgraph-checkpoint-postgres

WORKDIR /app
COPY departments/00-ceo-office/api ./departments/00-ceo-office/api
COPY departments/00-ceo-office/src ./departments/00-ceo-office/src
COPY departments/00-ceo-office/governance_events ./departments/00-ceo-office/governance_events
# change_workflow.py(src/mandate)가 저장소 루트의 orchestration.contracts.mandate_confirmation을
# 참조한다(HITL Mandate 변경 Workflow의 Risk/QA/사용자 승인 게이트, 2026-08-05 도입) - Build Context를
# 저장소 루트로 옮긴 이유. orchestration 패키지는 pydantic+표준 라이브러리만 써서 이 Image에 안전하게
# 들어간다(다른 본부 코드에 대한 의존은 없음).
COPY orchestration ./orchestration

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app

CMD ["uvicorn", "app:app", "--app-dir", "departments/00-ceo-office/api", "--host", "0.0.0.0", "--port", "8000"]
