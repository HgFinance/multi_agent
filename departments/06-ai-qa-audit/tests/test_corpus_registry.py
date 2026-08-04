from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evidence.corpus_registry import (
    PolicyCorpusError,
    inspect_policy_corpus,
    require_production_policy_corpus,
)
from evidence.pgvector_retriever import PgvectorRetrievalError, _vector_literal


def test_placeholder_corpus_is_not_production_ready(tmp_path: Path) -> None:
    (tmp_path / "policy.md").write_text("# Policy\nSAMPLE_PLACEHOLDER\n", encoding="utf-8")

    status = inspect_policy_corpus(tmp_path)

    assert status.ready is False
    assert status.placeholder_count == 1
    with pytest.raises(PolicyCorpusError, match="placeholder"):
        require_production_policy_corpus(tmp_path)


def test_real_corpus_has_stable_hash(tmp_path: Path) -> None:
    (tmp_path / "policy.md").write_text("# Policy\nActual policy text.\n", encoding="utf-8")

    first = require_production_policy_corpus(tmp_path)
    second = inspect_policy_corpus(tmp_path)

    assert first.ready is True
    assert first.corpus_hash == second.corpus_hash


def test_vector_literal_rejects_empty_embedding() -> None:
    with pytest.raises(PgvectorRetrievalError, match="empty"):
        _vector_literal([])
