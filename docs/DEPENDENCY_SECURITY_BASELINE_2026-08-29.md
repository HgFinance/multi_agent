# Python 의존성 보안 기준선

기준일: 2026-08-29 UTC

## 적용 범위

`requirements.txt`를 입력으로 사용하는 공유 Python runtime과 CI, `apps/api/Dockerfile`을 대상으로 한다. 기존 의존성은 삭제하지 않았고, `langsmith`, `psycopg`, `pyarrow`, `langfuse`도 lock 대상에 포함했다.

## 생성물

- `requirements.lock`: uv가 생성한 98개 package의 exact version 및 distribution hash
- `docs/dependency-python-sbom.cdx.json`: CycloneDX 1.4 Python SBOM
- `docs/dependency-cve-audit.json`: `pip-audit 2.10.1` JSON 결과

2026-08-29 실행 결과 `pip-audit`는 **No known vulnerabilities found**를 반환했다. 이 결과는 Python package 기준이며, Docker base image/OS package CVE 결과를 의미하지 않는다.

## 적용 경계

- CI는 `requirements.txt`가 아니라 `requirements.lock`을 hash 검증으로 설치한다.
- `apps/api/Dockerfile`도 동일한 lock을 사용한다.
- Research/Quant/Agent 계열의 독립 Dockerfile은 의도적으로 별도 최소 의존성과 명시적 pin을 사용한다. 이 파일들은 root lock으로 강제 통합하지 않았으며, 별도 image SBOM/CVE 검사가 다음 보안 게이트다.
- 새 package를 추가할 때는 `requirements.txt`를 먼저 검토하고 lock을 재생성한다. 패키지 삭제는 import graph, runtime smoke, 전체 테스트 확인 후 별도 변경으로 한다.

## 재생성 명령

~~~bash
uv pip compile requirements.txt \
  --python-version 3.12 \
  --generate-hashes \
  --output-file requirements.lock

uvx --from pip-audit pip-audit \
  --disable-pip --require-hashes \
  -r requirements.lock \
  --format json \
  --output docs/dependency-cve-audit.json \
  --progress-spinner off

uvx --from pip-audit pip-audit \
  --disable-pip --require-hashes \
  -r requirements.lock \
  --format cyclonedx-json \
  --output docs/dependency-python-sbom.cdx.json \
  --progress-spinner off
~~~
