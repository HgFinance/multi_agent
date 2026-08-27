from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "llm_wiki"))

from wiki_compiler import (
    RawDoc,
    load_raw_docs,
    render_wiki_page,
    validate_links,
)

_CURRENT = RawDoc(
    source="law",
    doc_id="law-1-178",
    page_id="page_178",
    clause_id="제178조",
    title="부정거래행위 등의 금지",
    authority="금융위원회",
    effective_from="2026-01-01",
    text="부정한 수단, 계획 또는 기교를 사용하는 행위를 금지한다.",
    origin_url="https://example.test/178",
    source_sha256="sha256:deadbeef",
)
_VALID_PAGE_IDS = {"page_178", "page_443"}


def test_validate_links_drops_dangling_target() -> None:
    links = [{"target_page": "page_999", "relation_type": "RELATED_TO", "snippet": "부정한 수단"}]
    assert validate_links(_CURRENT, links, _VALID_PAGE_IDS) == []


def test_validate_links_drops_disallowed_relation_type() -> None:
    links = [{"target_page": "page_443", "relation_type": "MADE_UP", "snippet": "부정한 수단"}]
    assert validate_links(_CURRENT, links, _VALID_PAGE_IDS) == []


def test_validate_links_drops_ungrounded_snippet() -> None:
    links = [
        {
            "target_page": "page_443",
            "relation_type": "PENALIZED_BY",
            "snippet": "본문에 없는 문장",
        }
    ]
    assert validate_links(_CURRENT, links, _VALID_PAGE_IDS) == []


def test_validate_links_drops_self_link() -> None:
    links = [
        {"target_page": "page_178", "relation_type": "RELATED_TO", "snippet": "부정한 수단"}
    ]
    assert validate_links(_CURRENT, links, _VALID_PAGE_IDS) == []


def test_validate_links_keeps_grounded_valid_link() -> None:
    links = [
        {"target_page": "page_443", "relation_type": "PENALIZED_BY", "snippet": "부정한 수단"}
    ]
    validated = validate_links(_CURRENT, links, _VALID_PAGE_IDS)
    assert len(validated) == 1
    assert validated[0].target_page == "page_443"
    assert validated[0].relation_type == "PENALIZED_BY"


def test_render_wiki_page_emits_grep_indexable_json_metadata() -> None:
    validated = validate_links(
        _CURRENT,
        [{"target_page": "page_443", "relation_type": "PENALIZED_BY", "snippet": "부정한 수단"}],
        _VALID_PAGE_IDS,
    )
    page_text = render_wiki_page(_CURRENT, validated)

    assert "[[page_443]]" in page_text
    json_block = page_text.split("```json\n", 1)[1].split("\n```", 1)[0]
    metadata = json.loads(json_block)
    assert metadata == {
        "current_page": "page_178",
        "outgoing_links": [
            {"target_page": "page_443", "relation_type": "PENALIZED_BY", "snippet": "부정한 수단"}
        ],
    }


def test_load_raw_docs_reads_json_files_from_directory(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    payload = {
        "source": "law",
        "doc_id": "law-1-1",
        "page_id": "page_1",
        "clause_id": "제1조",
        "title": "제목",
        "authority": "금융위원회",
        "effective_from": "2026-01-01",
        "text": "본문",
        "origin_url": "https://example.test/1",
        "source_sha256": "sha256:abc",
    }
    (raw_dir / "law-1-1.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    docs = load_raw_docs(raw_dir)

    assert len(docs) == 1
    assert docs[0].page_id == "page_1"
