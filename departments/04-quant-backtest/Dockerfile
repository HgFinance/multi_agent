# 퀀트/백테스트 Container Image (quant-api)
#
# 담당: 재일 (퀀트·백테스트본부 QNT)
# 근거: 재일님 지시 2026-08-04 "퀀트 쪽은 전략 공장이기 때문에 실험도구를
#       무조건적으로 보장해야 함"
#
# ▶ 빌드 컨텍스트가 **저장소 루트**다 (리서치 이미지와 다르다).
#   quant-api 는 리서치의 source_registry(.env 로더)와 research_bridge 가
#   읽는 리서치 스키마를 함께 쓴다. 컨텍스트를 04-quant-backtest 로 잡으면
#   그 파일들이 이미지에 없어 import 가 죽는다 - 실제로 처음에 그렇게 만들었다가
#   "Dockerfile not found" 를 거쳐 이 구조로 왔다.
#
# ▶ .env 는 이미지에 넣지 않는다. Credential 은 compose environment 로만
#   주입한다(리서치 이미지와 같은 규약).

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Seoul

# 퀀트 파이프라인은 표준 라이브러리 위주다 - 과적합 통계도 math 만 쓴다.
# psycopg2(Repository), pydantic(계약), dotenv(.env), fastapi/uvicorn(API).
# pyarrow is limited to the bounded discovery-sample cache: it prevents every
# evolved AST cohort from replaying the same external 92GB source. Remove it
# when a database/object-store feature service replaces that cache contract.
RUN pip install --no-cache-dir \
    psycopg2-binary==2.9.10 pydantic==2.11.7 python-dotenv==1.1.1 \
    fastapi==0.116.1 "uvicorn[standard]==0.35.0" \
    numpy==2.3.2 pyarrow==25.0.0

WORKDIR /app

COPY departments/04-quant-backtest/api ./departments/04-quant-backtest/api
COPY departments/04-quant-backtest/pipeline ./departments/04-quant-backtest/pipeline
COPY departments/04-quant-backtest/contracts ./departments/04-quant-backtest/contracts
COPY departments/04-quant-backtest/agents ./departments/04-quant-backtest/agents
# .env 로더. 퀀트가 리서치 모듈을 읽기만 한다(쓰기는 리서치만).
COPY departments/01-research/collectors/source_registry.py \
     ./departments/01-research/collectors/source_registry.py
# The intraday runner imports these shared, unit-safe research contracts as
# top-level modules. The API image did not need them, but the resident worker's
# forward-confirmation sweep does; omitting them made the worker look healthy
# while logging ModuleNotFoundError on every sweep.
COPY departments/01-research/contracts ./departments/01-research/contracts

ENV PYTHONPATH=/app/departments/04-quant-backtest/pipeline:/app/departments/04-quant-backtest/contracts:/app/departments/01-research/contracts:/app/departments/01-research/collectors

WORKDIR /app/departments/04-quant-backtest/api
CMD ["uvicorn", "quant_api:app", "--host", "0.0.0.0", "--port", "8037"]
