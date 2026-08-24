# AI QA/감사본부 Backend Image — audit-api(FastAPI)와 qa-worker(Redis Consumer)를 담는다.
#
# 담당: 동규 (리스크/QA)
# 근거: docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md
#       3.1절(부서당 Image 1개, command만 바꿔 역할 분리), 6.7절(AI QA/감사본부 Container 목록),
#       11.1절(Image 기준)
#
# ▶ Build Context가 departments/06-ai-qa-audit가 아니라 **저장소 루트**다 (research
#   Dockerfile과 다른 점, departments/03-risk/Dockerfile과 같은 이유). audit-api가
#     - skills/agentic-rag (compliance-policy-agent, RAG 계열 재사용)
#     - apps/observability (공통 계측)
#   를 import하고 `_REPO_ROOT = Path(__file__).resolve().parents[3]`로 저장소 루트를
#   sys.path에 넣는다. compose에서는 build.context: . / build.dockerfile: departments/06-ai-qa-audit/Dockerfile.
#
# ▶ 하나의 Image, command만 바꿔 audit-api와 qa-worker를 둘 다 돌린다(계획서 3.1).
#   qa_events/worker.py는 DATABASE_URL(또는 RISK_QA_DATABASE_URL)과
#   REDIS_URL(또는 RISK_QA_EVENT_REDIS_URL)이 모두 필요하다. compose가 DB 연결과
#   내부 Redis 기본값을 명시적으로 전달한다.

FROM python:3.12-slim

# 버전은 risk와 동일하게 이 저장소에서 실제로 테스트를 통과한 값으로 고정한다(2026-08-03 pip freeze).
# langfuse: 이 이미지의 Worker 실행기가 HR 관측 이벤트를 발행한다
# (orchestration/llm_observability.publish_worker_activity). 없으면 publish 가
# ModuleNotFoundError 를 삼키고 **조용히 False** 를 돌려줘, 자격증명이 멀쩡한데
# 이벤트만 0건인 상태가 된다(2026-08-23 실측: ceo-kanban-supervisor 에서 같은
# 결함을 추적하는 데 몇 시간이 걸렸다). 루트 requirements.txt 와 같은 pin.
RUN pip install --no-cache-dir \
    fastapi==0.128.8 "uvicorn[standard]==0.39.0" pydantic==2.13.3 \
    psycopg2-binary==2.9.12 python-dotenv==1.2.1 redis==7.0.1 \
    openai==2.35.1 langgraph==0.6.11 numpy==2.3.2 \
    langfuse==4.14.3

WORKDIR /app
COPY departments/06-ai-qa-audit ./departments/06-ai-qa-audit
# Shared Worker Model Gateway. The model overlay injects Qwen AWQ v1
# coordinates; without the overlay, the QA image retains its local fallback.
COPY departments/worker_model_gateway.py ./departments/worker_model_gateway.py
# QA employee graphs import the shared WorkerSpec/runtime and the common
# technology profile definitions at request time, not only during API startup.
COPY departments/employee_worker_runtime.py ./departments/employee_worker_runtime.py
COPY departments/risk_qa_worker_profiles.py ./departments/risk_qa_worker_profiles.py
# The independent reproduction worker reuses the frozen, pure forward
# evaluator in a separate process.  It does not import quant's DB writer or
# receive svc_quant privileges.
COPY departments/04-quant-backtest/pipeline ./departments/04-quant-backtest/pipeline
COPY departments/01-research/contracts ./departments/01-research/contracts
COPY skills/agentic-rag ./skills/agentic-rag
COPY apps/observability ./apps/observability
COPY apps/security ./apps/security
# qa_mandate_workers.py 가 `orchestration.contracts.mas` 를 최상위에서 import 한다
# (build_agent_task_result). risk-api 와 같은 이유 - 없으면 audit-api 와 qa-worker
# 둘 다 크래시 루프다(2026-08-12 실측). 외부 의존은 pydantic 하나뿐이다.
COPY orchestration ./orchestration

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

CMD ["uvicorn", "app:app", "--app-dir", "departments/06-ai-qa-audit/api", "--host", "0.0.0.0", "--port", "8000"]
