"""Human-facing labels shared by both Risk Notion projection paths.

Canonical Python/DB keys stay unchanged.  Only the non-binding Notion view
uses these labels, and legacy column names remain readable during migration.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


RISK_PROPERTY_NAMES: dict[str, tuple[str, ...]] = {
    "title": ("제목",),
    "trade_case_id": ("거래 케이스 ID", "trade_case_id"),
    "verdict": ("리스크 판정", "판정"),
    "trading_state": ("거래 가능 상태", "trading_state"),
    "approved_quantity": ("승인 수량",),
    "reason_codes": ("판정 사유 코드", "reason_codes"),
    "escalate": ("상위·법무 검토 필요", "escalate"),
    "input_hash": ("입력 데이터 해시", "input_hash"),
    "calculation_version": ("계산 로직 버전", "calculation_version"),
    "check_results": ("리스크 검사 결과", "check_results"),
    "counterparty_health": ("거래상대방 상태", "counterparty_health(원본)"),
    "counterparty_narrative": ("거래상대방 검토", "counterparty_narrative"),
    "narrative": ("리스크 검토 요약", "서술"),
    "created_at": ("작성 시각", "생성 시각"),
    "compliance_verdict": ("법률·컴플라이언스 판정", "compliance_verdict"),
    "original_report": ("상세 검토 보고서", "원본 리포트"),
}

RISK_METADATA_LABELS: dict[str, str] = {
    "analysis_mode": "분석 방식",
    "rating": "종합 위험도",
    "verdict": "리스크 판정",
    "query_mode": "검토 유형",
    "llm_wiki_invoked": "법률 LLM-Wiki 사용",
    "cited_documents": "인용 법령·판례",
    "pages_visited": "확인한 Wiki 문서",
    "confidence": "판정 신뢰도",
    "escalate": "상위·법무 검토 필요",
    "mandate_version": "투자지침 버전",
    "mandate_snapshot_id": "투자지침 스냅샷 ID",
    "portfolio_as_of": "포트폴리오 기준 시각",
    "portfolio_authoritative": "포트폴리오 권위 데이터",
    "quality_status": "데이터 품질 상태",
    "fresh_external_fetches": "추가 외부 조회 횟수",
    "order_authorized": "주문 승인 여부",
    "failures": "재시도 전 실패 횟수",
    "retry_status": "재시도 상태",
    "calculation_version": "계산 로직 버전",
    "input_hash": "입력 데이터 해시",
    "trace_id": "추적 ID",
}

RISK_METADATA_ORDER = tuple(RISK_METADATA_LABELS)


def risk_property_name(
    field: str,
    properties_schema: Mapping[str, Any] | None = None,
) -> str:
    """Resolve a preferred Korean column, accepting its legacy alias."""

    names = RISK_PROPERTY_NAMES[field]
    if properties_schema is not None:
        for name in names:
            if name in properties_schema:
                return name
    return names[0]


def human_metadata_rows(metadata: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Return a bounded, deterministic set of human-readable audit rows."""

    rows: list[tuple[str, str]] = []
    for key in RISK_METADATA_ORDER:
        if key not in metadata or metadata[key] is None:
            continue
        rows.append((RISK_METADATA_LABELS[key], human_value(metadata[key])))
    return rows


def human_value(value: Any) -> str:
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return ", ".join(str(item) for item in value) or "없음"
    text = str(value).strip()
    return text or "없음"


__all__ = [
    "RISK_METADATA_LABELS",
    "RISK_PROPERTY_NAMES",
    "human_metadata_rows",
    "human_value",
    "risk_property_name",
]
