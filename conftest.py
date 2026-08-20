"""저장소 루트 pytest 설정 — 테스트 실행이 실제 관측 데이터가 되지 않게 막는다.

2026-08-20 신규. Worker 실행 계측(orchestration/llm_observability.py)이 공용 런타임과
Risk/QA 실행기에 붙으면서, **테스트가 Worker 를 돌리는 것만으로 Langfuse 에 실행
이벤트가 쌓이는** 경로가 생겼다. `set -a; . .env; pytest` 처럼 자격증명이 살아 있는
셸에서 스위트를 돌리면 HR 의 유휴 리포트가 사람의 작업이 아니라 **CI 를 관측하고**
ACTIVE 라고 답한다 - 인사 판단의 근거가 조용히 오염된다.

`tests/conftest.py` 는 `tests/` 아래에만 적용되므로 부서 테스트(departments/03-risk/
tests/ 등)를 못 덮는다. 그래서 루트에 둔다.

계측 자체를 검증하는 테스트는 클라이언트를 가짜로 바꾸거나 monkeypatch.setenv 로
스위치를 직접 켠다(tests/test_worker_activity_instrumentation.py,
tests/orchestration/test_llm_observability.py) - 여기서 끄는 것은 **전역 기본값**이지
그 테스트들이 쓰는 명시적 설정이 아니다.
"""

from __future__ import annotations

import os

# setdefault 가 아니라 강제 설정이다 - .env 를 export 한 셸에서 도는 경우를 막는 것이
# 이 파일의 목적이라, 기존 값이 있으면 그게 바로 막아야 할 값이다.
os.environ["LANGFUSE_TRACING"] = "false"
