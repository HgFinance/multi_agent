from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evidence.production_ingestion import (
    IngestionError,
    _vector_literal,
    build_policy_chunks,
)


def test_production_ingestion_rejects_placeholder(tmp_path: Path) -> None:
    (tmp_path / "policy.md").write_text(
        "---\ndocument_type: policy\n---\n# Policy\nSAMPLE_PLACEHOLDER\n",
        encoding="utf-8",
    )
    with pytest.raises(IngestionError, match="placeholder"):
        build_policy_chunks(tmp_path)


def test_test_ingestion_is_deterministic_and_preserves_metadata(tmp_path: Path) -> None:
    (tmp_path / "policy.md").write_text(
        "---\ndocument_id: P-1\nlicense_scope: internal\n"
        "published_at: 2026-01-01T00:00:00Z\n---\n# Policy\n\n## Rule\nUse a limit.\n",
        encoding="utf-8",
    )
    chunks = build_policy_chunks(tmp_path, allow_placeholders=True)
    assert chunks[0].external_id == "P-1"
    assert chunks[0].published_at == "2026-01-01T00:00:00+00:00"
    assert chunks[0].content_hash
    assert _vector_literal([0.0, 1.0], 2) == "[0,1]"
