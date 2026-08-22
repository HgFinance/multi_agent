#!/usr/bin/env python3
"""Build an auditable LLM-Wiki-shaped corpus and a scoped RAG glossary.

The source is a text extraction of the Bank of Korea's 2026 ``경제금융용어
800선`` PDF.  The extraction is intentionally source-preserving: this script
does not ask an LLM to rewrite definitions.  It writes one Markdown page per
source entry with EXTRACTED provenance, then selects only deterministic
financial-arithmetic entries for the quantization RAG glossary.  Risk terms are
excluded fail-closed because the AWQ+RAG benchmark does not permit them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


SOURCE_URL = (
    "https://www.bok.or.kr/portal/bbs/B0000249/view.do?"
    "depth=200765&menuNo=200765&nttId=10096081&oldMenuNo=201150&"
    "programType=newsData&relate=Y"
)
SOURCE_FILE_URL = (
    "https://www.bok.or.kr/fileSrc/portal/5cbf35f51f3842dd9ed1fba7cef5199a/"
    "1/74ac2f04b15c4debac64fd6931aea9fd.pdf"
)
WIKI_VERSION = "bok-800-2026-source-preserving-v1"
GLOSSARY_VERSION = "bok-800-arithmetic-glossary-v1"

RISK_TERMS = (
    "리스크",
    "위험",
    "건전성",
    "부실",
    "취약성",
    "금융위기",
    "신용위험",
    "유동성위험",
    "운영위험",
    "시스템위험",
    "감독",
    "규제",
    "대손",
    "VaR",
    "Value at Risk",
    "FSI",
    "FVI",
    "HDRI",
    "NPL",
)
ARITHMETIC_TERMS = (
    "공식",
    "계산",
    "분모",
    "분자",
    "비율",
    "수익률",
    "저축률",
    "성장률",
    "금리",
    "이자",
    "원금",
    "환율",
    "소득",
    "지출",
    "저축",
    "매출",
    "이익",
    "비용",
    "자산",
    "부채",
    "자본",
    "가격",
    "단위",
    "평균",
    "할인",
    "배당",
    "스프레드",
    "베이시스포인트",
    "bp",
    "%",
    "백분율",
)
NOISE_EXACT = {
    "I",
    "i",
    "ii",
    "iii",
    "iv",
    "v",
    "vi",
    "vii",
    "viii",
    "ix",
    "x",
    "xi",
    "xii",
    "xiii",
    "xiv",
    "xv",
    "xvi",
    "xvii",
}


@dataclass(frozen=True)
class TocEntry:
    term: str
    printed_page: int


@dataclass(frozen=True)
class Entry:
    ordinal: int
    term: str
    printed_page: int
    body: str
    related_terms: tuple[str, ...]
    source_sha256: str


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def compact(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("’", "'").replace("‘", "'").replace("ㆍ", "·")
    return re.sub(r"[^0-9A-Za-z가-힣]+", "", value).casefold()


def display_normalize(value: str) -> str:
    value = value.replace("·", " ").replace("ㆍ", " ")
    return re.sub(r"\s+", " ", value).strip()


def is_noise(line: str) -> bool:
    value = line.strip()
    if not value:
        return True
    if value in NOISE_EXACT or re.fullmatch(r"[ㄱ-ㅎ]", value):
        return True
    if re.fullmatch(r"\d{1,3}", value):
        return True
    if "경제금융용어" in value and "800선" in value:
        return True
    if "찾아보기" in value:
        return True
    return False


def parse_toc(pages: list[str]) -> tuple[int, list[TocEntry]]:
    content_start = next(
        index
        for index, page in enumerate(pages)
        if "가계부실위험지수(HDRI)" in page and "가구의 소득 흐름" in page
    )
    entries: list[TocEntry] = []
    for page in pages[:content_start]:
        buffer = ""
        for raw_line in page.splitlines():
            line = raw_line.strip()
            if is_noise(line):
                continue
            buffer += line
            match = re.search(r"((?:\s*\d){1,3})\s*$", buffer)
            if not match:
                continue
            before_page = buffer[: match.start()].rstrip()
            leader = re.search(r"(?:·\s*){3,}", before_page)
            if leader is None:
                buffer = ""
                continue
            term = display_normalize(before_page[: leader.start()])
            printed_page = int(match.group(1).replace(" ", ""))
            if term:
                entries.append(TocEntry(term=term, printed_page=printed_page))
            buffer = ""
    unique: list[TocEntry] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.term not in seen:
            unique.append(entry)
            seen.add(entry.term)
    return content_start, unique


def page_header_line_indices(page: str) -> set[int]:
    lines = page.splitlines()
    nonempty = [(i, line.strip()) for i, line in enumerate(lines) if line.strip()]
    if len(nonempty) < 3:
        return set()
    first_index, first_value = nonempty[0]
    first_three = [value for _, value in nonempty[:4]]
    has_section_marker = any(re.fullmatch(r"[ㄱ-ㅎ]", value) for value in first_three)
    has_page_number = any(re.fullmatch(r"\d{1,3}", value) for value in first_three)
    return {first_index} if has_section_marker and has_page_number else set()


def find_headings(pages: list[str], content_start: int, toc: list[TocEntry]) -> list[tuple[TocEntry, int, int, int]]:
    """Find exact source headings, allowing a heading to wrap over 2-3 lines."""

    lines: list[tuple[int, int, str]] = []
    header_indices: set[tuple[int, int]] = set()
    for page_index in range(content_start, len(pages)):
        page_lines = pages[page_index].splitlines()
        header_indices.update((page_index, index) for index in page_header_line_indices(pages[page_index]))
        lines.extend((page_index, index, line.strip()) for index, line in enumerate(page_lines))

    found: list[tuple[TocEntry, int, int, int]] = []
    cursor = 0
    for toc_entry in toc:
        target = compact(toc_entry.term)
        candidate: tuple[int, int, int] | None = None
        for position in range(cursor, len(lines)):
            page_index, line_index, line = lines[position]
            if (page_index, line_index) in header_indices or is_noise(line):
                continue
            for width in (1, 2, 3):
                end = position + width
                if end > len(lines):
                    continue
                window = lines[position:end]
                if any(item[0] != page_index for item in window):
                    continue
                if any(is_noise(item[2]) for item in window):
                    continue
                if compact("".join(item[2] for item in window)) != target:
                    continue
                candidate = (position, page_index, line_index)
                break
            if candidate is not None:
                break
        # A small number of TOC labels are abbreviated while the body heading
        # includes the expanded label (for example, the body adds
        # ``평가등급제도``).  Accept only a short, same-line prefix match as a
        # fail-closed parser fallback; definitions are never accepted here.
        if candidate is None:
            for position in range(cursor, len(lines)):
                page_index, line_index, line = lines[position]
                if (page_index, line_index) in header_indices or is_noise(line):
                    continue
                joined = compact(line)
                if joined.startswith(target) and 0 < len(joined) - len(target) <= 30:
                    candidate = (position, page_index, line_index)
                    break
        if candidate is None:
            continue
        position, page_index, line_index = candidate
        found.append((toc_entry, position, page_index, line_index))
        cursor = position + 1
    return found


def clean_body(lines: list[str], header_indices: set[int], term_set: set[str]) -> str:
    kept: list[str] = []
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line or index in header_indices or is_noise(line):
            continue
        if compact(line) in term_set and index == 0:
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def extract_entries(pages: list[str], content_start: int, toc: list[TocEntry]) -> list[Entry]:
    found = find_headings(pages, content_start, toc)
    if len(found) != len(toc):
        missing = [entry.term for entry in toc if entry not in {item[0] for item in found}]
        raise ValueError(f"could not locate {len(missing)} source headings: {missing}")

    all_lines: list[tuple[int, int, str]] = []
    page_offsets: dict[int, int] = {}
    for page_index in range(content_start, len(pages)):
        page_offsets[page_index] = len(all_lines)
        all_lines.extend((page_index, index, line) for index, line in enumerate(pages[page_index].splitlines()))
    term_set = {compact(entry.term) for entry in toc}
    results: list[Entry] = []
    for ordinal, (toc_entry, position, page_index, line_index) in enumerate(found, 1):
        next_position = found[ordinal][1] if ordinal < len(found) else len(all_lines)
        start = position
        end = next_position
        width = 1
        target = compact(toc_entry.term)
        for candidate_width in (1, 2, 3):
            joined = compact("".join(all_lines[start + i][2] for i in range(candidate_width)))
            if joined == target:
                width = candidate_width
                break
        body_lines = [line for _, _, line in all_lines[start + width : end]]
        local_header_indices = {
            index
            for index, line in enumerate(body_lines)
            if is_noise(line) or "경제금융용어" in line or "찾아보기" in line
        }
        body = clean_body(body_lines, local_header_indices, term_set)
        related: tuple[str, ...] = ()
        related_match = re.search(r"연관검색어\s+(.+)$", body, flags=re.MULTILINE)
        if related_match:
            related = tuple(
                item.strip(" ·,，")
                for item in re.split(r"[,，]", related_match.group(1))
                if item.strip(" ·,，")
            )
            body = body[: related_match.start()].rstrip()
        source_sha = sha256_bytes(body.encode("utf-8"))
        results.append(
            Entry(
                ordinal=ordinal,
                term=toc_entry.term,
                printed_page=toc_entry.printed_page,
                body=body,
                related_terms=related,
                source_sha256=source_sha,
            )
        )
    return results


def is_risk_entry(entry: Entry) -> bool:
    haystack = f"{entry.term}\n{entry.body}".casefold()
    return any(term.casefold() in haystack for term in RISK_TERMS)


def is_risk_text(value: str) -> bool:
    haystack = value.casefold()
    return any(term.casefold() in haystack for term in RISK_TERMS)


def is_arithmetic_entry(entry: Entry) -> bool:
    if is_risk_entry(entry):
        return False
    haystack = f"{entry.term}\n{entry.body}".casefold()
    return any(term.casefold() in haystack for term in ARITHMETIC_TERMS)


def slug(entry: Entry) -> str:
    return f"{entry.ordinal:03d}-{entry.source_sha256[:10]}"


def render_entity(entry: Entry, source_pdf_sha256: str, rag_allowed: bool) -> str:
    related = "\n".join(f"- {term}" for term in entry.related_terms) or "- 없음"
    return "\n".join(
        [
            "---",
            f"wiki_version: {WIKI_VERSION}",
            f"entry_id: bok800-{entry.ordinal:03d}",
            f"term: {entry.term}",
            f"source_pdf_page: {entry.printed_page}",
            f"source_pdf_sha256: {source_pdf_sha256}",
            f"entry_text_sha256: {entry.source_sha256}",
            "confidence: EXTRACTED",
            f"rag_allowed: {str(rag_allowed).lower()}",
            "---",
            "",
            f"# {entry.term}",
            "",
            "## 원문 기반 정의",
            "",
            entry.body,
            "",
            "## 연관검색어",
            "",
            related,
            "",
            "## 출처",
            "",
            f"- 한국은행 『경제금융용어 800선』 2026, PDF p. {entry.printed_page}",
            f"- entry_text_sha256: `{entry.source_sha256}`",
            "",
        ]
    )


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_glossary(entries: list[Entry]) -> list[dict[str, object]]:
    glossary: list[dict[str, object]] = []
    for entry in entries:
        if not is_arithmetic_entry(entry):
            continue
        ascii_aliases = re.findall(
            r"\b(?:[A-Z]{2,}[A-Za-z0-9-]*|[A-Z][A-Za-z]*\d+[A-Za-z0-9-]*)\b",
            f"{entry.term} {entry.body}",
        )
        aliases = [
            alias
            for alias in dict.fromkeys([*entry.related_terms, *ascii_aliases])
            if not is_risk_text(alias)
        ]
        glossary.append(
            {
                "term": entry.term,
                "definition": entry.body,
                "scope": "financial_arithmetic",
                "aliases": aliases,
                "source_page": entry.printed_page,
                "source_entry_sha256": entry.source_sha256,
            }
        )
    return glossary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", type=Path, required=True, help="pypdf text extraction with form-feed page separators")
    parser.add_argument("--source-pdf", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-url", default=SOURCE_URL)
    parser.add_argument("--source-file-url", default=SOURCE_FILE_URL)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pages = args.text.read_text(encoding="utf-8").split("\f")
    content_start, toc = parse_toc(pages)
    entries = extract_entries(pages, content_start, toc)
    source_pdf_sha256 = sha256_file(args.source_pdf)
    source_text_sha256 = sha256_file(args.text)
    glossary = build_glossary(entries)
    glossary_bytes = (json.dumps(glossary, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    glossary_sha256 = sha256_bytes(glossary_bytes)
    selected_terms = {row["term"] for row in glossary}
    selected_entries = [entry for entry in entries if entry.term in selected_terms]
    if any(is_risk_entry(entry) for entry in selected_entries):
        raise ValueError("risk entry leaked into glossary selection")
    if any(is_risk_text(alias) for row in glossary for alias in row["aliases"]):
        raise ValueError("risk alias leaked into glossary selection")
    if not glossary:
        raise ValueError("arithmetic glossary selection is empty")

    wiki_root = args.output_root / "wiki"
    entity_root = wiki_root / "entities"
    entity_root.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        (entity_root / f"{slug(entry)}.md").write_text(
            render_entity(entry, source_pdf_sha256, is_arithmetic_entry(entry)), encoding="utf-8"
        )

    index_lines = [
        "# 한국은행 경제금융용어 800선 Wiki",
        "",
        f"- wiki_version: `{WIKI_VERSION}`",
        f"- source PDF SHA256: `{source_pdf_sha256}`",
        f"- extracted entries: `{len(entries)}`",
        f"- RAG arithmetic entries: `{len(glossary)}`",
        "- 생성 방식: 원문 보존형 deterministic extraction; LLM 요약 없음",
        "",
        "## 사용 규칙",
        "",
        "각 페이지의 `confidence: EXTRACTED`와 PDF 페이지를 근거로 사용한다. RAG benchmark에는 `rag_allowed: true`인 arithmetic subset만 사용하며, Risk 관련 용어는 포함하지 않는다.",
        "",
        "## 항목",
        "",
    ]
    for entry in entries:
        allowed = "RAG" if is_arithmetic_entry(entry) else "Wiki only"
        index_lines.append(f"- [[entities/{slug(entry)}]] — {entry.term} ({allowed}, p. {entry.printed_page})")
    (wiki_root / "index.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    glossary_path = args.output_root / "glossary_rag_v1.json"
    glossary_path.write_bytes(glossary_bytes)
    manifest = {
        "schema_version": "glossary-manifest.v1",
        "name": "HgFinance BOK 800 arithmetic glossary",
        "version": GLOSSARY_VERSION,
        "sha256": glossary_sha256,
        "source_documents": [
            {
                "name": "한국은행 경제금융용어 800선",
                "version": "2026-01-29",
                "source_url": args.source_url,
                "file_url": args.source_file_url,
                "local_pdf": "not committed; source artifact verified outside the repository",
                "sha256": source_pdf_sha256,
                "license_provenance": "Official Bank of Korea public download; preserve source citation and do not redistribute the PDF artifact.",
            }
        ],
        "scope": ["financial_arithmetic"],
        "excluded_scope": ["risk", "risk_terminology", "structured_output_not_present_in_source_pdf"],
        "entries": len(glossary),
        "selection": {
            "rule": "deterministic keyword allowlist minus deterministic risk-term denylist",
            "risk_terms_checked": list(RISK_TERMS),
            "source_entries_total": len(entries),
            "source_entries_selected": len(glossary),
        },
        "artifacts": {
            "wiki_index": str(wiki_root / "index.md"),
            "wiki_entities": str(entity_root),
            "glossary": str(glossary_path),
        },
    }
    write_json(args.output_root / "glossary_rag_v1_manifest.json", manifest)

    source_manifest = {
        "schema_version": "bok800-wiki-source.v1",
        "wiki_version": WIKI_VERSION,
        "source_pdf_sha256": source_pdf_sha256,
        "source_text_sha256": source_text_sha256,
        "source_url": args.source_url,
        "source_file_url": args.source_file_url,
        "source_pdf_pages": len(pages),
        "toc_entries": len(toc),
        "wiki_entries": len(entries),
        "rag_arithmetic_entries": len(glossary),
        "extraction": "pypdf text extraction; source-preserving deterministic heading/body parser",
        "llm_rewrite": False,
        "confidence_policy": "EXTRACTED only; no inferred definition is written",
    }
    write_json(args.output_root / "source_manifest.json", source_manifest)

    comparison_plan = {
        "schema_version": "five-axis-comparison-plan.v1",
        "runtime_profile": "L4-fp8KV-v1",
        "primary_variants": ["FP8", "AWQ", "AWQ+Finetune", "AWQ+Reasoning", "AWQ+RAG"],
        "wiki_role": "offline knowledge compilation and diagnostic retrieval baseline; not a sixth primary model variant",
        "rag_role": "runtime deterministic glossary injection using glossary_rag_v1.json",
        "fairness_contract": {
            "gpu": "same NVIDIA L4",
            "max_model_len": 8192,
            "gpu_memory_utilization": 0.85,
            "kv_cache_dtype": "fp8_e4m3",
            "prefix_caching": True,
            "temperature": 0,
            "stream": False,
            "sequential_quality": True,
        },
        "wiki_vs_rag_diagnostic": {
            "same_questions": True,
            "same_base_model": "Qwen2.5-14B-Instruct-AWQ",
            "wiki_context": "bounded source-preserving entity pages selected by deterministic BM25/term match",
            "rag_context": "deterministic glossary entries selected by glossary_rag.py",
            "separate_metrics": ["model_only_latency", "retrieval_latency", "full_pipeline_latency", "hit_rate", "citation_grounding"],
            "status": "READY_FOR_RUNTIME_EXECUTION",
        },
        "artifacts": {
            "source_manifest": str(args.output_root / "source_manifest.json"),
            "glossary_manifest": str(args.output_root / "glossary_rag_v1_manifest.json"),
            "comparison_plan": str(args.output_root / "five_axis_comparison_plan.json"),
        },
    }
    write_json(args.output_root / "five_axis_comparison_plan.json", comparison_plan)

    print(json.dumps({
        "status": "READY",
        "content_start_page_index": content_start,
        "wiki_entries": len(entries),
        "rag_arithmetic_entries": len(glossary),
        "source_pdf_sha256": source_pdf_sha256,
        "glossary_sha256": glossary_sha256,
        "output_root": str(args.output_root),
        "primary_variants": comparison_plan["primary_variants"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
