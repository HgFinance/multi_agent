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
# ▶ 의존성이 pydantic 하나뿐이다. 실측(2026-08-03) 결과 감싸는 5개 모듈의 외부 import는
#   pydantic 이 전부이고 나머지는 표준 라이브러리다. psycopg2와 redis를 넣지 않은 것은
#   빠뜨린 게 아니라 **아직 쓰지 않기 때문**이다 - OrderStore가 프로세스 메모리라
#   DB를 안 붙었고(설계서 4절, 팀장 확인 대기), Event Stream도 아직 없다.
#   psycopg OrderStore가 들어오는 순간 psycopg2-binary를 여기에 추가한다.
#
# ▶ 하나의 Image, command만 바꿔 여러 프로세스를 돌리는 원칙은 유지하되 지금은
#   trading-api 하나뿐이다. 계획서 6.3의 oms-worker/broker-adapter는 넣지 않았다 -
#   코드가 없다(코드 없는 역할을 컨테이너부터 만들지 않는다).

FROM python:3.12-slim

# 버전은 이 저장소에서 이미 검증된 값(risk/audit/ceo Dockerfile과 동일 pin)으로 고정한다.
RUN pip install --no-cache-dir \
    fastapi==0.128.8 "uvicorn[standard]==0.39.0" pydantic==2.13.3

WORKDIR /app/departments/02-trading
# app.py가 sys.path에 넣는 하위 폴더 이름과 같아야 한다(_DEPT / _sub).
COPY api ./api
COPY contracts ./contracts
COPY oms ./oms
COPY broker ./broker
COPY multileg ./multileg
COPY capability ./capability

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

CMD ["uvicorn", "app:app", "--app-dir", "api", "--host", "0.0.0.0", "--port", "8000"]
