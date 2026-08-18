# 트레이딩본부 Backend Image — trading-api(FastAPI)를 담는다.
#
# 담당: 도현 (트레이딩/회계·포트폴리오)
# 근거: docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md
#       3.1절(부서당 Image 1개, command만 바꿔 역할 분리), 6.3절(트레이딩본부 Container 목록),
#       11.1절(Image 기준), docs/02-engineering/TRADING_DOMAIN_API_SPEC.md
#
# ▶ Build Context가 저장소 루트가 아니라 **departments/02-trading** 이다
#   (00-ceo-office/01-research와 같고, 03-risk/06-ai-qa-audit와 다르다).
#   api/app.py가 contracts/oms/broker/multileg/capability만 import하고 다른 본부 코드를
#   참조하지 않는다 - 리스크본부는 반대로 우리 contracts를 import해서 루트 Context가 필요했다.
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
COPY api ./api
COPY contracts ./contracts
COPY oms ./oms
COPY broker ./broker
COPY multileg ./multileg
COPY capability ./capability
COPY directives ./directives

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

CMD ["uvicorn", "app:app", "--app-dir", "api", "--host", "0.0.0.0", "--port", "8000"]
