#!/usr/bin/env python3
"""호환 Wrapper — 실제 구현은 departments/02-trading/broker/paper_broker.py로 이동했다.

REPOSITORY_DEPARTMENT_STRUCTURE.md 11절 "단계적 이전 계획" 단계 3의 임시 CLI 호환 경로다.
2026-10-31 이후 제거 예정. 새 코드는 departments/02-trading/broker/paper_broker.py를 직접 참조할 것.
"""
from __future__ import annotations

import runpy
from pathlib import Path

_TARGET = Path(__file__).resolve().parent.parent / "departments" / "02-trading" / "broker" / "paper_broker.py"

globals().update(runpy.run_path(str(_TARGET), run_name=__name__))
