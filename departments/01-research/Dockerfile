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
RUN pip install --no-cache-dir psycopg2-binary==2.9.10 pydantic==2.11.7 python-dotenv==1.1.1 websockets==15.0.1 fastapi==0.116.1 "uvicorn[standard]==0.35.0" pyarrow==25.0.0

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

ENV PYTHONUNBUFFERED=1

CMD ["python", "collectors/news_watch_service.py"]
