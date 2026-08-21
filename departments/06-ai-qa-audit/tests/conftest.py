"""QA/감사 부서 테스트 환경 — 테스트 실행이 실제 관측 데이터가 되지 않게 막는다.

2026-08-20: Worker 실행 계측이 공용 런타임과 Risk/QA 실행기에 붙으면서, **테스트가
Worker 를 돌리는 것만으로** Langfuse 에 실행 이벤트가 쌓이는 경로가 생겼다.
`set -a; . .env; pytest` 처럼 자격증명이 살아 있는 셸에서 스위트를 돌리면 HR 의 유휴
리포트가 사람의 작업이 아니라 **CI 를 관측하고** ACTIVE 라고 답한다 - 인사 판단의
근거가 조용히 오염된다.

setdefault 가 아니라 강제 설정이다 - .env 를 export 한 셸에서 도는 경우를 막는 것이
목적이라, 기존 값이 있으면 그게 바로 막아야 할 값이다.

계측 자체를 검증하는 테스트는 클라이언트를 가짜로 바꾸거나 monkeypatch.setenv 로
스위치를 직접 켠다 - 여기서 끄는 것은 **전역 기본값**이지 그 테스트들의 명시적 설정이
아니다.

▶ 저장소 루트에 두지 않는다(2026-08-20 실측). 루트 conftest.py 는 pytest 의 수집
  경로 순서를 바꿔 tests/api/* 가 엉뚱한 모듈을 집게 만들었다(20건 실패). 부서
  테스트는 tests/conftest.py 가 못 덮으므로 각 테스트 디렉터리에 같은 가드를 둔다.
"""

from __future__ import annotations

import os

os.environ["LANGFUSE_TRACING"] = "false"
