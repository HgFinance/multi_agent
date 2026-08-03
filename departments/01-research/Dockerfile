# 리서치 수집기 Container Image
#
# 담당: 재일 (리서치/퀀트)
# 근거: 재일님 지시(2026-07-31) "수집기들 Docker 컨테이너로 띄워 DB 즉시 적재"
#
# ▶ 하나의 Image, 여러 entrypoint. 기본 CMD 는 상주형 뉴스 수집이며 compose 가
#   command: 로 다른 수집기를 지정할 수 있다 (수집기마다 Image 를 만들지 않는다).
#
# ▶ .env 는 Image 에 넣지 않는다. Credential 은 compose 의 environment 로만
#   주입한다 - load_project_env 가 "환경변수 우선" 이라 파일 없이 동작한다.
#
# ▶ 의존성은 셋뿐이다 - psycopg2(Repository), pydantic(source_registry 의 Source
#   계약), python-dotenv(.env 파싱 - Image 에는 .env 가 없지만 코드 경로가 있다).
#   NAVER 수집기·Stream 계약·Sink 자체는 표준 라이브러리만 쓴다(urllib).
#   버전은 루트 requirements.txt 선언(pydantic>=2, 나머지 미고정)을 따르되 Image
#   재현성을 위해 여기서 고정한다. 새 라이브러리를 늘리기 전에 CLAUDE.md 개발
#   원칙 8 을 본다.

FROM python:3.12-slim

# pyarrow 는 06:50 market-archive Job(Raw -> 검증된 Parquet) 전용이다 (DoD
# "Raw Market Data가 검증된 Parquet로 Archive된다", 2026-08-01 추가)
# mcp: 리서치본부 도구 면(api/mcp_server.py)을 Hermes 에 붙이는 다리.
# 버전 고정하지 않는 이유 - MCP SDK 가 빠르게 바뀌고 우리는 MCPServer/tool/
# streamable-http 라는 안정된 최소 표면만 쓴다. 깨지면 자체점검이 먼저 잡는다.
# xlrd: GPR 일별 지수(.xls) 파싱. 2026-08-02 실측 - 이게 없어 geopolitical Job 이
# 매일 실패하고 있었다(pandas 를 쓰던 코드를 xlrd 직접 읽기로 바꿨다 - 열 4개를
# 읽자고 pandas 를 넣는 것보다 가볍다).
RUN pip install --no-cache-dir psycopg2-binary==2.9.10 pydantic==2.11.7 python-dotenv==1.1.1 websockets==15.0.1 fastapi==0.116.1 "uvicorn[standard]==0.35.0" pyarrow==25.0.0 mcp xlrd==2.0.2

# 저장소의 departments/01-research 경로 구조를 그대로 유지한다 - 수집기들이
# Path(__file__) 상대로 contracts/repository 를 sys.path 에 넣기 때문이다.
WORKDIR /app/departments/01-research
COPY collectors ./collectors
COPY contracts ./contracts
COPY repository ./repository
COPY api ./api
# evidence/ 는 research-api /evidence/stories 가 story_cluster(결정론 군집)를
# import 하기 위해 필요하다 - 표준 라이브러리만 써서 의존성은 늘지 않는다
COPY evidence ./evidence
# agents/ 와 scripts.py: MCP 도구 run_research_packet 이 부서 파이프라인
# (분석가 6인 -> Packet)을 subprocess 로 돌린다. 2026-08-02 실측 - 이 두 줄이
# 없어 컨테이너에서 "No such file: scripts.py" 로 실패했다. LangGraph 는
# 아래에서 별도 설치한다(무거워서 다른 서비스와 레이어를 나눈다).
COPY agents ./agents
COPY scripts.py ./scripts.py

# 파이프라인 전용 의존성. 수집기·API 컨테이너는 안 쓰지만 같은 이미지를
# 공유하므로 여기 둔다(계획서 3.1 "같은 본부는 하나의 Code Image").
RUN pip install --no-cache-dir langgraph==0.6.7

ENV PYTHONUNBUFFERED=1

CMD ["python", "collectors/news_watch_service.py"]
