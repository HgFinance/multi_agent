# 트레이딩본부 Backend Image — trading-api(FastAPI)를 담는다.
#
# 담당: 도현 (트레이딩/회계·포트폴리오)
# 근거: docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md
#       3.1절(부서당 Image 1개, command만 바꿔 역할 분리), 6.3절(트레이딩본부 Container 목록),
#       11.1절(Image 기준), docs/02-engineering/TRADING_DOMAIN_API_SPEC.md
#
# ▶ 조건주문 엔진의 공통 deterministic 계약을 함께 패키징하므로 Build Context는
#   저장소 루트다. Hermes나 다른 부서 런타임을 import하는 것이 아니라
#   orchestration/conditional_rules의 순수 계약·평가 코드만 포함한다.
#
# ▶ psycopg2-binary와 redis는 2026-08-10에 추가했다. 위 주석이 예고한 "psycopg
#   OrderStore가 들어오는 순간"이 왔다 - store_postgres.py가 생겼고 api/app.py가
#   PAPER_DB에서 그걸 쓴다. redis는 oms/relay.py가 Outbox를 발행할 때만 import한다
#   (`serve()` 안에서 지연 import - 자체 점검은 Redis 없이 돈다).
#
# ▶ 하나의 Image, command만 바꿔 여러 프로세스를 돌린다(계획서 3.1). 지금 두 개다 -
#   trading-api(uvicorn)와 trading-outbox-relay(oms/relay.py --serve). 계획서 6.3의
#   broker-adapter는 아직 코드가 없어 넣지 않았다.

FROM python:3.12-slim

# 버전은 이 저장소에서 이미 검증된 값(risk/audit/ceo Dockerfile과 동일 pin)으로 고정한다.
RUN pip install --no-cache-dir \
    fastapi==0.128.8 "uvicorn[standard]==0.39.0" pydantic==2.13.3 \
    psycopg2-binary==2.9.12 redis==7.0.1

WORKDIR /app/departments/02-trading
# app.py가 sys.path에 넣는 하위 폴더 이름과 같아야 한다(_DEPT / _sub).
COPY departments/02-trading/api ./api
COPY departments/02-trading/contracts ./contracts
COPY departments/02-trading/oms ./oms
COPY departments/02-trading/broker ./broker
COPY departments/02-trading/multileg ./multileg
COPY departments/02-trading/capability ./capability
COPY departments/02-trading/directives ./directives
COPY departments/02-trading/rules ./rules
COPY orchestration/conditional_rules /app/orchestration/conditional_rules

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app:/app/departments/02-trading:/app/departments/02-trading/api

CMD ["uvicorn", "app:app", "--app-dir", "api", "--host", "0.0.0.0", "--port", "8000"]
