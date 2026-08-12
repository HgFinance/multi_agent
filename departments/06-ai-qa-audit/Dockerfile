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
#   qa_events/worker.py는 REDIS_URL(또는 RISK_QA_EVENT_REDIS_URL)이 없으면 즉시 종료한다 -
#   compose에서 REDIS_URL을 `:?` 필수로 준다.

FROM python:3.12-slim

# 버전은 risk와 동일하게 이 저장소에서 실제로 테스트를 통과한 값으로 고정한다(2026-08-03 pip freeze).
RUN pip install --no-cache-dir \
    fastapi==0.128.8 "uvicorn[standard]==0.39.0" pydantic==2.13.3 \
    psycopg2-binary==2.9.12 python-dotenv==1.2.1 redis==7.0.1 \
    openai==2.35.1 langgraph==0.6.11

WORKDIR /app
COPY departments/06-ai-qa-audit ./departments/06-ai-qa-audit
COPY skills/agentic-rag ./skills/agentic-rag
COPY apps/observability ./apps/observability
COPY apps/security ./apps/security

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

CMD ["uvicorn", "app:app", "--app-dir", "departments/06-ai-qa-audit/api", "--host", "0.0.0.0", "--port", "8000"]
