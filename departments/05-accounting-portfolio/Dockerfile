# 회계/포트폴리오본부 Backend Image — accounting-api(FastAPI)를 담는다.
#
# 담당: 도현 (트레이딩/회계·포트폴리오)
# 근거: docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md
#       3.1절(부서당 Image 1개, command만 바꿔 역할 분리), 6.6절(회계본부 Container 목록),
#       11.1절(Image 기준), docs/02-engineering/ACCOUNTING_PORTFOLIO_DOMAIN_API_SPEC.md
#
# ▶ Build Context가 departments/05-accounting-portfolio가 아니라 **저장소 루트**다
#   (03-risk와 같고, 02-trading과 다르다). 트레이딩은 자기 폴더 안에서 닫히지만
#   회계는 ledger.py와 reconciliation.py가 **모듈 최상위에서** 부서 경계를 넘는다:
#     sys.path.insert(0, .../parent.parent.parent / "02-trading" / "contracts")
#     from contracts import Side
#   이 계산이 `departments/<본부>/<모듈>/x.py` 배치를 전제하므로 이미지 안에서도
#   같은 상대 경로(/app/departments/05-accounting-portfolio/...)를 유지해야 한다.
#   compose에서는 build.context: ../.. / build.dockerfile: 이 파일.
#
# ▶ 의존성이 fastapi/uvicorn/pydantic 셋뿐이다. 감싸는 5개 모듈(ledger, portfolio,
#   reconciliation, corporate_actions, daily_report)의 외부 import는 없고 전부 표준
#   라이브러리이며, 넘어오는 contracts.py만 pydantic을 쓴다.
#   psycopg2를 넣지 않은 것은 빠뜨린 게 아니라 **아직 쓰지 않기 때문**이다 -
#   원장이 프로세스 메모리라 DB를 안 붙었다(설계서 4절). 안 쓰는 자격·의존성이
#   퍼지는 것 자체가 위험이라는 02-trading/Dockerfile과 같은 원칙이다.
#
# ▶ 계획서 6.6의 ledger-worker / portfolio-projector는 넣지 않았다 - 코드가 없다.
#   코드 없는 역할을 컨테이너부터 만들지 않는다.

FROM python:3.12-slim

# 버전은 이 저장소에서 이미 검증된 값(risk/audit/ceo/trading Dockerfile과 동일 pin)으로 고정한다.
RUN pip install --no-cache-dir \
    fastapi==0.128.8 "uvicorn[standard]==0.39.0" pydantic==2.13.3

WORKDIR /app
# 저장소 루트 기준 상대 경로를 그대로 유지한다 - ledger.py의 parent.parent.parent 계산과 맞아야 한다.
COPY departments/05-accounting-portfolio ./departments/05-accounting-portfolio
COPY departments/02-trading/contracts ./departments/02-trading/contracts

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

CMD ["uvicorn", "app:app", "--app-dir", "departments/05-accounting-portfolio/api", \
     "--host", "0.0.0.0", "--port", "8000"]
