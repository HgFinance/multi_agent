"""Repository-wide deterministic test environment.

Tests that use the portfolio BFF must opt into its explicit local fixture
identity. The repository has no browser login or external user-auth mode.
"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("PORTFOLIO_AUTH_MODE", "fixture")
os.environ.setdefault("PORTFOLIO_DATA_MODE", "test")
os.environ.setdefault(
    "PORTFOLIO_RUNTIME_STORE_PATH",
    os.path.join(
        tempfile.gettempdir(), f"hgfinance-portfolio-pytest-{os.getpid()}.sqlite3"
    ),
)

# 2026-08-20: 테스트가 Worker 를 돌리는 것만으로 Langfuse 에 실행 이벤트가 쌓이면
# HR 유휴 리포트가 사람이 아니라 CI 를 관측한다 - 인사 판단 근거가 조용히 오염된다.
# setdefault 가 아니라 강제 설정이다(.env 를 export 한 셸에서 도는 경우가 막을 대상).
# 계측 테스트는 monkeypatch 로 직접 켜므로 영향받지 않는다.
os.environ["LANGFUSE_TRACING"] = "false"
