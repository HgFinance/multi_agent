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
# ▶ 의존성이 fastapi/uvicorn/pydantic/psycopg2 넷이다. 감싸는 5개 모듈(ledger, portfolio,
#   reconciliation, corporate_actions, daily_report)의 외부 import는 없고 전부 표준
#   라이브러리이며, 넘어오는 contracts.py만 pydantic을 쓴다.
#   psycopg2는 2026-08-04 원장 저장소(ledger/repository.py)가 들어오면서 추가했다 -
#   DATABASE_URL이 있을 때만 실제로 import된다(`_load_driver()`).
#
# ▶ 계획서 6.6의 ledger-worker / portfolio-projector는 넣지 않았다 - 코드가 없다.
#   코드 없는 역할을 컨테이너부터 만들지 않는다.

FROM python:3.12-slim

# 버전은 이 저장소에서 이미 검증된 값(risk/audit/ceo/trading Dockerfile과 동일 pin)으로 고정한다.
RUN pip install --no-cache-dir \
    fastapi==0.128.8 "uvicorn[standard]==0.39.0" pydantic==2.13.3 psycopg2-binary==2.9.12 \
    langgraph==0.6.11
# ▶ langgraph 가 왜 필요한가 (2026-08-12)
#   위 주석의 "의존성이 넷이다"는 더 이상 사실이 아니다. scripts.py 85행이
#   `from langgraph.graph import END, StateGraph` 를 쓰고 close_scheduler.py 가
#   그 scripts 를 import 한다. 그래서 close-scheduler 만 크래시 루프였다 -
#   accounting-api 는 그 경로를 안 타서 멀쩡했고, 그만큼 늦게 발견됐다.
#   버전은 risk/audit Dockerfile 과 같은 pin 을 쓴다.

WORKDIR /app
# 저장소 루트 기준 상대 경로를 그대로 유지한다 - ledger.py의 parent.parent.parent 계산과 맞아야 한다.
COPY departments/05-accounting-portfolio ./departments/05-accounting-portfolio
COPY departments/02-trading/contracts ./departments/02-trading/contracts
# notion_reporter.py 가 `departments.notion_markdown` 을 import 한다. 부서 폴더만
# 복사하면 namespace 패키지 `departments` 는 생기는데 이 파일이 없어서
# close-scheduler 가 크래시 루프였다(2026-08-12 실측). 부서 폴더 바로 위의
# 공용 모듈은 폴더 복사에 딸려오지 않는다 - 명시해야 한다.
COPY departments/notion_markdown.py ./departments/notion_markdown.py
# employee_workers.py -> employee_worker_runtime (공용 Worker 런타임) -> langgraph +
# orchestration.llm_observability. close_scheduler 가 load_ops 때문에
# employee_workers 를 import 하므로 이 사슬이 전부 이미지에 있어야 한다.
# notion_markdown 과 같은 부류의 누락이다 - 부서 폴더 위의 공용 모듈은 명시해야 한다.
COPY departments/employee_worker_runtime.py ./departments/employee_worker_runtime.py
COPY orchestration ./orchestration

# ▶ PYTHONPATH=/app 이 필요한 이유
#   이 이미지는 위 주석대로 **저장소 루트 상대 경로**(/app/departments/...)를 일부러
#   유지한다. 그런데 close_scheduler 는 `python close_scheduler.py` 로 뜨고
#   working_dir 가 부서 폴더라 sys.path[0] 가 그 폴더가 된다 - `/app` 은 어디에도
#   안 들어간다. 그래서 notion_reporter 의 `from departments.notion_markdown import ...`
#   가 파일이 이미지에 있는데도 실패했다(2026-08-12 실측). uvicorn 으로 뜨는
#   accounting-api 도 콘솔 스크립트라 사정이 같다. 레이아웃을 유지하기로 한 이상
#   그 루트를 import 경로로 선언해 주는 것이 짝이 맞는다.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app

CMD ["uvicorn", "app:app", "--app-dir", "departments/05-accounting-portfolio/api", \
     "--host", "0.0.0.0", "--port", "8000"]
