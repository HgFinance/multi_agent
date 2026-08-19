from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evidence.production_ingestion import (
    IngestionError,
    PgvectorEvidenceRepository,
    _vector_literal,
    build_policy_chunks,
    require_legacy_ingestion_enabled,
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


def test_legacy_ingestion_is_disabled_by_default() -> None:
    with pytest.raises(IngestionError, match="retrieved through MCP"):
        require_legacy_ingestion_enabled({})


@pytest.mark.parametrize(
    "environment",
    (
        {"QA_ENABLE_LEGACY_EVIDENCE_INGESTION": "true"},
        {"QA_INGEST_MODE": "legacy-manual"},
        {
            "QA_ENABLE_LEGACY_EVIDENCE_INGESTION": "false",
            "QA_INGEST_MODE": "legacy-manual",
        },
    ),
)
def test_legacy_ingestion_requires_both_explicit_switches(
    environment: dict[str, str],
) -> None:
    with pytest.raises(IngestionError, match="disabled"):
        require_legacy_ingestion_enabled(environment)


def test_legacy_ingestion_can_only_be_explicitly_enabled() -> None:
    require_legacy_ingestion_enabled(
        {
            "QA_ENABLE_LEGACY_EVIDENCE_INGESTION": "true",
            "QA_INGEST_MODE": "legacy-manual",
        }
    )


def test_repository_write_path_is_guarded_even_for_direct_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QA_ENABLE_LEGACY_EVIDENCE_INGESTION", raising=False)
    monkeypatch.delenv("QA_INGEST_MODE", raising=False)

    with pytest.raises(IngestionError, match="retrieved through MCP"):
        PgvectorEvidenceRepository(object()).ingest(
            source_id=UUID("00000000-0000-4000-8000-000000000001"),
            chunks=(),
            embedder=object(),  # type: ignore[arg-type]
        )
