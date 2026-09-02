# 리스크본부 Backend Image — risk-api(FastAPI)를 담는다.
#
# 담당: 동규 (리스크/QA)
# 근거: docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md
#       3.1절(부서당 Image 1개, command만 바꿔 역할 분리), 6.4절(리스크본부 Container 목록),
#       11.1절(Image 기준)
#
# ▶ Build Context가 departments/03-risk가 아니라 **저장소 루트**다 (research Dockerfile과
#   다른 점). risk-api(api/app.py)가 부서 경계를 넘어
#     - departments/02-trading/contracts (OrderIntent 계약)
#     - skills/agentic-rag (compliance-policy-agent)
#     - apps/observability (공통 계측)
#   를 import하고, `_REPO_ROOT = Path(__file__).resolve().parents[3]`로 저장소 루트를
#   sys.path에 넣는 전제라 이미지 안에서도 같은 상대 경로(/app/departments/03-risk/...)를
#   유지해야 한다. compose에서는 build.context: . / build.dockerfile: departments/03-risk/Dockerfile.
#
# ▶ 하나의 Image, command만 바꿔 risk-api와 읽기 전용 risk-mcp를 실행한다.
#   risk-projection-worker(계획서 6.4)는 넣지 않았다 -
#   risk_events/redis_event_bus.py에 Publisher만 있고 Consumer 루프가 아직 없다
#   (2026-08-03 확인). 코드 없는 역할을 컨테이너부터 만들지 않는다.

FROM python:3.12-slim

# 버전은 이 저장소에서 실제로 테스트를 통과한 값으로 고정한다(2026-08-03 pip freeze).
# langfuse: 이 이미지의 Worker 실행기가 HR 관측 이벤트를 발행한다
# (orchestration/llm_observability.publish_worker_activity). 없으면 publish 가
# ModuleNotFoundError 를 삼키고 **조용히 False** 를 돌려줘, 자격증명이 멀쩡한데
# 이벤트만 0건인 상태가 된다(2026-08-23 실측: ceo-kanban-supervisor 에서 같은
# 결함을 추적하는 데 몇 시간이 걸렸다). 루트 requirements.txt 와 같은 pin.
RUN pip install --no-cache-dir \
    fastapi==0.141.1 starlette==1.6.0 "uvicorn[standard]==0.39.0" pydantic==2.13.3 \
    psycopg2-binary==2.9.12 python-dotenv==1.2.1 redis==7.0.1 \
    openai==2.35.1 langgraph==0.6.11 numpy==2.3.2 \
    langfuse==4.14.3 mcp==1.29.1

WORKDIR /app
# 저장소 루트 기준 상대 경로를 그대로 유지한다 - api/app.py의 parents[3] 계산과 맞아야 한다.
COPY departments/03-risk ./departments/03-risk
# Shared Worker Model Gateway. Production model-plane overlays inject the
# vLLM/Qwen14B coordinates; without them the local Ollama fallback remains.
COPY departments/worker_model_gateway.py ./departments/worker_model_gateway.py
COPY departments/qwen_hybrid_runtime.py ./departments/qwen_hybrid_runtime.py
COPY departments/risk_notion_schema.py ./departments/risk_notion_schema.py
COPY departments/01-research/config/worker_model_registry.json ./departments/01-research/config/worker_model_registry.json
COPY benchmarks/quantization/knowledge/bok800_2026/glossary_rag_v1.json ./benchmarks/quantization/knowledge/bok800_2026/glossary_rag_v1.json
# Risk's employee graph imports these shared runtime contracts directly.
COPY departments/employee_worker_runtime.py ./departments/employee_worker_runtime.py
COPY departments/risk_qa_worker_profiles.py ./departments/risk_qa_worker_profiles.py
COPY departments/02-trading/contracts ./departments/02-trading/contracts
COPY skills/agentic-rag ./skills/agentic-rag
COPY skills/risk/mandate-dynamic-risk-controls ./skills/risk/mandate-dynamic-risk-controls
COPY apps/observability ./apps/observability
COPY apps/security ./apps/security
# risk_mandate_workers.py 가 `orchestration.contracts.mas` 를 최상위에서 import 한다
# (build_agent_task_result). 이게 없으면 uvicorn 이 app 을 못 읽고 컨테이너가
# 크래시 루프에 빠진다 - 2026-08-12 첫 기동에서 실제로 그랬다. mas.py 의 외부
# 의존은 pydantic 하나뿐이라 이미지가 무거워지지 않는다.
COPY orchestration ./orchestration

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

CMD ["uvicorn", "app:app", "--app-dir", "departments/03-risk/api", "--host", "0.0.0.0", "--port", "8000"]
