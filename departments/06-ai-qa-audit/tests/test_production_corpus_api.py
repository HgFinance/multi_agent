from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient


API_DIR = Path(__file__).resolve().parent.parent / "api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))
sys.modules.pop("app", None)
import app as qa_app  # noqa: E402


def test_qa_evidence_endpoint_blocks_placeholder_corpus_in_production(tmp_path: Path, monkeypatch) -> None:
    corpus = tmp_path / "evidence"
    corpus.mkdir()
    (corpus / "sample.md").write_text("# Evidence\nSAMPLE_PLACEHOLDER\n", encoding="utf-8")
    monkeypatch.setenv("RISK_QA_RUNTIME", "production")
    monkeypatch.setenv("QA_EVIDENCE_CORPUS_DIR", str(corpus))

    response = TestClient(qa_app.app).post(
        "/qa/v1/evidence/check",
        json={"query": "check evidence", "as_of": "2026-08-04"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == "QA_EVIDENCE_CORPUS_NOT_READY"
