from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
sys.modules.pop("app", None)
import app


def test_qa_check_is_open_in_test_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RISK_QA_RUNTIME", raising=False)
    monkeypatch.delenv("QA_CHECK_CONTRACT_APPROVED", raising=False)
    assert app._qa_check_contract_is_approved() is True


def test_qa_check_requires_explicit_production_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_QA_RUNTIME", "production")
    monkeypatch.setenv("QA_CHECK_CONTRACT_APPROVED", "false")
    assert app._qa_check_contract_is_approved() is False
    monkeypatch.setenv("QA_CHECK_CONTRACT_APPROVED", "true")
    assert app._qa_check_contract_is_approved() is True
