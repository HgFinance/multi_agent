"""CEO closed-loop supervisor policy and Hermes boundary tests."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import ClassVar

from orchestration.adapters.ceo_supervisor import (
    CeoSupervisorService,
    ChildTaskState,
    HermesKanbanClient,
    HermesKanbanCommandError,
    SupervisorAction,
    SupervisorDecision,
    SupervisorState,
    SupervisorValidationError,
    _analysis_execution_mode_from_root_body,
    _analysis_synthesis_decision,
    _append_accounting_delivery_scope,
    _append_research_source_coordinates,
    _append_trading_source_coordinates,
    _augment_accounting_reconciliation_answer,
    _augment_hr_final_answer,
    _canonicalize_research_source_urls,
    _compact_hr_qa_handoff,
    _deduplicate_research_conclusion_headings,
    _department_progress_text,
    _ensure_quant_retrieval_attempt,
    _expand_research_implicit_source_urls,
    _handoff_provenance,
    _initial_primary_materialization_decisions,
    _materialization_plan_body,
    _normalize_research_answer_headings,
    _normalize_research_handoff_provenance,
    _recover_compact_ceo_synthesis,
    _remove_research_duplicate_section,
    _research_answer_is_complete,
    _single_primary_passthrough_child,
    _synthesis_handoff_payload,
    _terminal_payload_mapping,
    cli_lane,
    decide_supervisor,
    parse_supervisor_output,
)
from orchestration.answer_contract import grade_answer, strip_bounded_retrieval_attempt
from orchestration.ceo_workflow_scope import (
    UserPaperOrderScope,
    build_root_body,
    build_scoped_task_body,
    infer_workflow_mode,
    primary_idempotency_key,
    selected_primary_profiles_from_body,
    selected_primary_profiles_from_task,
    validate_workflow_scope,
    workflow_mode_from_body,
)


def test_answer_contract_accepts_iso_timestamp_as_observation_time() -> None:
    grade = grade_answer(
        "검증 시각 2026-08-26T15:22:30Z 기준으로 관측 불가를 확인했습니다. "
        "근거 URL https://workforce.example.invalid/read"
    )

    assert grade.has_as_of is True
    assert grade.trustworthy is True


def test_research_source_coordinates_survive_short_ceo_synthesis() -> None:
    result = _append_research_source_coordinates(
        "### CEO 종합 판단\n리서치 요약",
        (
            {
                "assignee": "research-department",
                "workflow_role": "primary",
                "result": (
                    "2026-08-27 CBC뉴스는 산업 수요를 보도했습니다 "
                    "(https://example.com/cbc).\n"
                    "같은 날 기계신문은 배터리 업황을 보도했습니다 "
                    "(https://example.com/mt)."
                ),
            },
        ),
    )

    assert "CBC뉴스 · 발행일 2026-08-27: https://example.com/cbc" in result
    assert "기계신문 · 발행일 2026-08-27: https://example.com/mt" in result


def test_accounting_delivery_scope_is_user_readable_and_bounded() -> None:
    result = _append_accounting_delivery_scope(
        "### CEO 종합 판단\n회계 결과를 종합했습니다.",
        (
            {
                "assignee": "accounting-portfolio-department",
                "workflow_role": "primary",
                "comments": [
                    {
                        "body": (
                            "hgfinance.department-notion-delivery.v1 "
                            "delivery_status=DELIVERED readback_status=VERIFIED"
                        )
                    }
                ],
            },
        ),
    )

    assert "### 시스템 전달 상태" in result
    assert "Notion: 회계 보고서 저장 및 재확인 완료." in result
    assert "LangSmith:" in result
    assert "Discord:" in result
    assert "delivery_status" not in result


def test_research_source_coordinates_prefer_url_domain_over_adjacent_dart_label() -> (
    None
):
    result = _append_research_source_coordinates(
        "### CEO 종합 판단\n리서치 요약",
        (
            {
                "assignee": "research-department",
                "workflow_role": "primary",
                "result": (
                    "2026-08-27 DART 공시를 확인했으며 출처: "
                    "https://www.pinpointnews.co.kr/news/articleView.html?idxno=479097"
                ),
            },
        ),
    )

    assert "Pinpoint News · 발행일 2026-08-27" in result
    assert "DART · 발행일" not in result


def test_answer_contract_recognizes_explicit_unverified_source_limit() -> None:
    grade = grade_answer(
        "자료 기준 시각은 2026-08-27입니다. 공식 원문은 이번 검토에서 "
        "직접 확인하지 못했습니다. 근거 URL https://example.invalid/source"
    )

    assert grade.states_unknowns is True
    assert (
        "한계·미확인 항목 언급 없음(모르는 것을 밝혔는지 확인 필요)" not in grade.gaps
    )


def test_answer_contract_accepts_bounded_unavailable_retrieval_attempt() -> None:
    grade = grade_answer(
        "검증 보류: 원본 시계열이 없어 성과지표를 계산하지 않았습니다.\n"
        "retrieval_attempt:\n"
        "instrument=069500.KS\n"
        "requested_window=2025-08-27/2026-08-27\n"
        "source=ls-securities\n"
        "tr=UNAVAILABLE\n"
        "status=UNAVAILABLE\n"
        "queried_at=2026-08-27T00:00:00Z\n"
        "extracted_at=2026-08-27T00:00:02Z\n"
        "snapshot_hash=UNAVAILABLE"
    )

    assert grade.has_evidence is True
    assert grade.has_as_of is True
    assert grade.trustworthy is True


def test_answer_contract_rejects_incomplete_retrieval_attempt() -> None:
    grade = grade_answer(
        "검증 보류: 데이터가 없습니다.\n"
        "retrieval_attempt:\n"
        "instrument=069500.KS\n"
        "status=UNAVAILABLE\n"
        "extracted_at=2026-08-27T00:00:02Z"
    )

    assert grade.has_evidence is False


def test_accounting_synthesis_correction_preserves_bridge_direction_and_coordinates() -> (
    None
):
    root_body = (
        "## Accounting Engine snapshot (read-only, hgfinance.accounting-snapshot.v1)\n"
        '{"as_of":"2026-08-27T02:23:48Z","nav":"506468430",'
        '"cash":"475759362","securities_value":"24893460"}\n\n'
        "## User request\nPAPER 읽기 전용"
    )
    result = _augment_accounting_reconciliation_answer(
        "현금과 증권가액 합계가 NAV보다 5,815,608원 많아 확정할 수 없습니다.",
        (
            {"body": root_body},
            {
                "assignee": "accounting-portfolio-department",
                "result": "조회 근거: CSPAQ12300, t0424",
            },
        ),
    )

    assert "5,815,608원 부족합니다" in result
    assert "5,815,608원 많아" not in result


def test_trading_synthesis_uses_only_frozen_root_snapshot_coordinates() -> None:
    root_body = (
        "hgfinance.mandate-snapshot.v1\n"
        "content_hash=abcdef0123456789abcdef0123456789\n"
        "\n## Accounting Engine snapshot (read-only, hgfinance.accounting-snapshot.v1)\n"
        '{"as_of":"2026-08-27T02:23:48Z","nav":"506468430"}\n\n'
        "## User request\n삼성전자 PAPER 읽기 전용 검증"
    )
    payloads = (
        {
            "profile": "trading-department",
            "workflow_role": "primary",
        },
    )
    answer = _append_trading_source_coordinates(
        "2026-08-27 기준 주문 조건이 없어 OrderIntent 후보를 만들 수 없습니다.",
        root_body=root_body,
        task_payloads=payloads,
    )

    assert "### 거래 부서 검증 근거" in answer
    assert "동결 투자한도 스냅샷" in answer
    assert "회계 읽기 전용 스냅샷" in answer
    assert "기록 해시: abcdef0123456789abcdef0123456789" in answer
    assert (
        _append_trading_source_coordinates(
            answer,
            root_body=root_body,
            task_payloads=payloads,
        )
        == answer
    )
    assert grade_answer(answer).trustworthy is True


def test_trading_handoff_and_qa_grade_share_root_evidence_projection() -> None:
    root_body = (
        "hgfinance.mandate-snapshot.v1\n"
        "content_hash=abcdef0123456789abcdef0123456789\n"
        "\n## Accounting Engine snapshot (read-only, hgfinance.accounting-snapshot.v1)\n"
        '{"as_of":"2026-08-27T02:23:48Z","nav":"506468430"}\n\n'
        "## User request\n삼성전자 PAPER 읽기 전용 검증"
    )
    child = ChildTaskState.from_hermes(
        {
            "id": "trading-task",
            "assignee": "trading-department",
            "status": "done",
            "body": (
                "workflow_root_task_id=root\n"
                "workflow_role=primary\n"
                "workflow_mode=analysis"
            ),
            "result": (
                "2026-08-27 기준 주문 방향·수량·주문 유형이 없어 후보를 만들 수 없습니다. "
                "실제 주문·체결·원장 변경은 수행하지 않았습니다."
            ),
        }
    )

    handoff = _synthesis_handoff_payload(child, root_body=root_body)

    assert handoff["answer_trustworthy"] is True
    assert "기록 해시:" in handoff["final_answer"]
    assert len(handoff["provenance"]["source_references"]) == 2


def test_accounting_synthesis_correction_recovers_embedded_handoff() -> None:
    synthesis_body = (
        "instructions\n"
        '[{"task_id":"t_accounting","profile":"accounting-portfolio-department",'
        '"result":"근거 CSPAQ12200, t0424"}]'
    )
    result = _augment_accounting_reconciliation_answer(
        "- 🔬 **Research:** Accounting Engine와 LS CSPAQ12200 기준 NAV 506,468,430원, 현금 475,759,362원입니다.",
        ({"body": synthesis_body},),
    )

    assert "📒 **Accounting / Portfolio:**" in result
    assert "CSPAQ12200" in result
    assert "### 회계 근거 좌표" in result
    assert "t0424" in result


def test_accounting_synthesis_correction_repairs_mislabelled_research_and_keeps_liquidity_details() -> (
    None
):
    result = _augment_accounting_reconciliation_answer(
        "- 📒 **Accounting / Portfolio:** 6개 포지션의 단순 평가손익은 약 +1,722,308원이며 출처 URL·인용 좌표가 없어 외부 검증은 제한됩니다.\n"
        "- 📒 **Accounting / Portfolio:** Preliminary NAV 506,468,430원입니다.",
        (
            {
                "assignee": "accounting-portfolio-department",
                "result": (
                    "LS 예수금: KRW 477,504,522. D+1 예수금 KRW 477,504,430, "
                    "D+2 예수금 KRW 473,399,477. 인출가능액 및 현금주문가능액: "
                    "각각 KRW 476,609,219. D+2 예상 결제: KRW -4,104,953. "
                    "신용·대출·미수금: 모두 0으로 보고됐고 담보부족액도 0입니다. "
                    "당일 체결 상태: 주문수량 344, 체결수량 344, 미체결 0, "
                    "총 거래대금 KRW 5,875,856. 총 수수료 KRW 876, 총비용 KRW 2,645."
                ),
            },
        ),
    )

    assert "- 🔬 **Research:**" in result
    assert "### 회계·포트폴리오 세부 상태" in result
    assert "인출가능액·현금주문가능액: 각각 476,609,219원" in result
    assert "D+2 예상 결제: -4,104,953원" in result
    assert "당일 거래비용: 수수료 876원, 총비용 2,645원" in result


def test_accounting_synthesis_correction_handles_broker_payload_variants() -> None:
    result = _augment_accounting_reconciliation_answer(
        "- 📊 **Accounting/Portfolio:** 회계 결과를 확인했습니다.",
        (
            {
                "assignee": "accounting-portfolio-department",
                "result": (
                    "예수금: KRW 477,504,522. D+1 예수금 KRW 477,504,430. "
                    "D+2 예수금 KRW 473,399,477. 출금가능액 및 현금 주문가능액: "
                    "각각 KRW 476,609,219. D+2 예상결제: KRW -4,104,953. "
                    "금일 체결: 주문수량 344, 미체결 0, 체결금액 KRW 5,875,856. "
                    "commission KRW 876, total cost KRW 2,645. "
                    "FOCCQ33600은 ERROR(모의투자에서 해당 업무 제공되지 않음)."
                ),
            },
        ),
    )

    assert "- 📒 **Accounting / Portfolio:**" in result
    assert "인출가능액·현금주문가능액: 각각 476,609,219원" in result
    assert "D+2 예상 결제: -4,104,953원" in result
    assert "당일 체결 수량: 주문 344주, 체결 344주, 미체결 0주" in result
    assert "당일 거래비용: 수수료 876원, 총비용 2,645원" in result
    assert "기간 수익률 자료는 증권사에서 제공되지 않아" in result


def test_accounting_synthesis_correction_preserves_full_live_accounting_summary() -> (
    None
):
    result = _augment_accounting_reconciliation_answer(
        "### CEO 종합 판단\n회계 결과를 종합했습니다.",
        (
            {
                "assignee": "accounting-portfolio-department",
                "result": (
                    "- Preliminary NAV: KRW 506,468,430.\n"
                    "- 현금: KRW 475,759,362.\n"
                    "- 유가증권 평가액: KRW 24,893,460.\n"
                    "- 실현 PnL: KRW 106,469.7322.\n"
                    "- 미실현 PnL: KRW 1,722,308.2678.\n"
                    "- 비용: 수수료 KRW 196,093, 세금 KRW 5,759.\n"
                    "- LS 현금성 예수금: KRW 477,504,522. D+1 예상 정산: KRW -92, "
                    "D+2 예상 정산: KRW -4,104,953.\n"
                    "- LS 당일 집계: 수수료 KRW 876, 총 비용 KRW 2,645.\n"
                    "- 체결 상태는 총 주문 344주 중 344주 체결, 미체결 0.\n"
                    "- 삼성전자 (005930): 29주, 평가액 KRW 7,757,500.\n"
                    "* 005930: 28주 대 29주"
                ),
            },
        ),
    )

    assert "순자산 가치 506,468,430원" in result
    assert "현금 475,759,362원" in result
    assert "실현손익 106,469.7322원" in result
    assert "D+1 예수금: -92원" in result
    assert "당일 거래비용: 수수료 876원, 총비용 2,645원" in result
    assert "삼성전자(005930) 수량 대사 차이" in result


def test_accounting_synthesis_includes_named_vs_breaks_and_empty_quality_source() -> (
    None
):
    result = _augment_accounting_reconciliation_answer(
        "### CEO 종합 판단\n회계 결과를 종합했습니다.",
        (
            {
                "assignee": "accounting-portfolio-department",
                "result": (
                    "- 삼성전자(005930): CSPAQ12300 28주 vs t0424 29주.\n"
                    "- 한온시스템(018880): CSPAQ12300 0주 vs t0424 300주.\n"
                    "- 두산에너빌리티(034020): 0주 vs 10주.\n"
                    "- 레인보우로보틱스(277810): 0주 vs 2주.\n"
                    "- 두산로보틱스(454910): 0주 vs 20주.\n"
                    "FOCCQ33600은 ERROR이며 t0151은 EMPTY입니다."
                ),
            },
        ),
    )

    assert result.count("수량 대사 차이") == 5
    assert "전일 거래·수수료 조회 자료는 비어 있어" in result
    assert "기간 수익률 자료는 증권사에서 제공되지 않아" in result


def test_compact_ceo_synthesis_recovery_keeps_one_bullet_per_primary() -> None:
    result = _recover_compact_ceo_synthesis(
        "PAPER 읽기 전용 요약만 저장되었습니다.",
        (
            {
                "assignee": "research-department",
                "body": "workflow_role=primary",
                "result": "### 핵심 판단\nResearch 근거와 확인 한계를 정리했습니다.",
            },
            {
                "assignee": "risk-management",
                "body": "workflow_role=primary",
                "result": "### 종합 위험도\n자료 공백으로 한도 검증은 제한됩니다.",
            },
            {
                "assignee": "accounting-portfolio-department",
                "body": "workflow_role=primary",
                "result": "상태 WARN. NAV 100원, 현금 80원, 대사 BREAK입니다.",
            },
        ),
    )

    assert result.count("### 부서별 핵심 의견") == 1
    assert result.count("**Research:**") == 1
    assert result.count("**Risk:**") == 1
    assert result.count("**Accounting / Portfolio:**") == 1


def test_compact_ceo_synthesis_recovery_keeps_quant_evidence() -> None:
    result = _recover_compact_ceo_synthesis(
        "HOLD / NOT_VERIFIABLE; no orders or executions.",
        (
            {
                "assignee": "quant-backtest-department",
                "body": "workflow_role=primary",
                "result": (
                    "### 핵심 판단\n069500.KS 데이터셋 시계열을 확인하지 못했습니다. "
                    "성과지표는 HOLD입니다.\nTR 카탈로그 결과가 없습니다."
                ),
            },
        ),
    )

    assert "**Quant:**" in result
    assert "069500.KS 데이터셋" in result
    assert "retrieval_attempt" not in result


def test_compact_ceo_synthesis_recovery_removes_nested_handoff_json() -> None:
    result = _recover_compact_ceo_synthesis(
        (
            "### CEO 종합 판단\n"
            "069500.KS는 데이터가 부족해 HOLD입니다.\n\n"
            "### 부서별 핵심 의견\n"
            '- 📊 **Quant:** {"final_answer":"중복된 Quant 본문",'
            '"retrieval_attempt":{"instrument":"069500.KS"}}\n'
            "\n### 결론\n검증 전까지 보류합니다."
        ),
        (
            {
                "assignee": "quant-backtest-department",
                "body": "workflow_role=primary",
                "result": (
                    "### 핵심 판단\n069500.KS 시계열을 확인하지 못해 "
                    "성과지표를 산출하지 않았습니다. HOLD입니다."
                ),
            },
        ),
    )

    assert "중복된 Quant 본문" not in result
    assert '{"final_answer":' not in result
    assert "069500.KS 시계열을 확인하지 못해" in result


def test_answer_contract_accepts_inline_json_retrieval_attempt() -> None:
    answer = (
        "검증된 수급 결과이며 기준 시각을 확인했습니다.\n"
        'retrieval_attempt: {"instrument":"005930.KS",'
        '"requested_window":"20 trading days","source":"LS MCP",'
        '"tr":"t1717","status":"AVAILABLE",'
        '"queried_at":"2026-08-27T09:15:42Z",'
        '"extracted_at":"2026-08-27T09:15:42Z",'
        '"snapshot_hash":"84aa0dcf79ac2096"}'
    )

    assert grade_answer(answer).trustworthy is True
    assert "retrieval_attempt:" not in strip_bounded_retrieval_attempt(answer)


def test_answer_contract_accepts_fenced_json_retrieval_attempt() -> None:
    answer = (
        "검증 보류: 원본 시계열이 없어 성과지표를 계산하지 않았습니다.\n"
        "### retrieval_attempt\n"
        "```json\n"
        '{"instrument":"069500.KS","requested_window":"UNSPECIFIED",'
        '"source":"LS OpenAPI","tr":"UNAVAILABLE",'
        '"status":"UNAVAILABLE","queried_at":"UNAVAILABLE",'
        '"extracted_at":"UNAVAILABLE","snapshot_hash":"UNAVAILABLE"}\n'
        "```"
    )

    assert grade_answer(answer).trustworthy is True
    assert "retrieval_attempt" not in strip_bounded_retrieval_attempt(answer)


def test_answer_contract_accepts_bare_json_retrieval_attempt() -> None:
    answer = (
        "검증 보류: 원본 시계열이 없어 성과지표를 계산하지 않았습니다.\n"
        "retrieval_attempt:\n"
        '{"instrument":"069500.KS","requested_window":"UNSPECIFIED",'
        '"source":"LS OpenAPI","tr":"UNAVAILABLE",'
        '"status":"UNAVAILABLE","queried_at":"UNAVAILABLE",'
        '"extracted_at":"UNAVAILABLE","snapshot_hash":"UNAVAILABLE"}'
    )

    assert grade_answer(answer).trustworthy is True
    assert "retrieval_attempt" not in strip_bounded_retrieval_attempt(answer)


def test_quant_synthesis_preserves_primary_retrieval_attempt() -> None:
    record = (
        "retrieval_attempt:\n"
        "instrument=069500.KS\n"
        "requested_window=2026-08-01/2026-08-27\n"
        "source=ls-securities\n"
        "tr=UNAVAILABLE\n"
        "status=UNAVAILABLE\n"
        "queried_at=2026-08-27T08:00:00Z\n"
        "extracted_at=2026-08-27T08:00:02Z\n"
        "snapshot_hash=UNAVAILABLE"
    )
    answer = _ensure_quant_retrieval_attempt(
        "Quant 결과를 HOLD로 보류합니다.",
        [
            {
                "assignee": "quant-backtest-department",
                "body": "workflow_root_task_id=root\nworkflow_role=primary",
                "result": f"검증 보류입니다.\n{record}",
            }
        ],
    )

    assert answer.count("retrieval_attempt:") == 1
    assert "instrument=069500.KS" in answer
    assert grade_answer(answer).trustworthy is True


def test_quant_synthesis_builds_retrieval_attempt_from_run_metadata() -> None:
    answer = _ensure_quant_retrieval_attempt(
        "Quant 결과를 확인했습니다.",
        [
            {
                "assignee": "quant-backtest-department",
                "body": "workflow_role=primary",
                "result": "Quant 결과를 확인했습니다.",
                "run_metadata": {
                    "instrument": "005930.KS",
                    "window": "20 trading days",
                    "source": "LS MCP",
                    "tr": "t1717",
                    "status": "AVAILABLE",
                    "queried_at": "2026-08-27T09:15:42Z",
                    "extracted_at": "2026-08-27T09:15:42Z",
                    "snapshot_hash": "84aa0dcf79ac2096",
                },
            }
        ],
    )

    assert answer.count("retrieval_attempt:") == 1
    assert "tr=t1717" in answer
    assert grade_answer(answer).trustworthy is True


def child(
    task_id: str,
    profile: str,
    status: str,
    *,
    body: str = "",
    block_kind: str = "",
    block_reason: str = "",
    retry_count: int = 0,
    summary: str = "summary",
    result: str = "",
) -> ChildTaskState:
    if "workflow_root_task_id=" not in body:
        body = (
            f"workflow_root_task_id=root\n{body}"
            if body
            else "workflow_root_task_id=root"
        )
    if "workflow_role=" not in body:
        role = "qa" if profile == "qa-department" else "primary"
        body = f"{body}\nworkflow_role={role}"
    return ChildTaskState(
        task_id=task_id,
        profile=profile,
        status=status,
        body=body,
        block_kind=block_kind,
        block_reason=block_reason,
        retry_count=retry_count,
        summary=summary,
        result=result,
    )


def test_terminal_payload_mapping_accepts_hydrated_child_state() -> None:
    state = child(
        "t_risk",
        "risk-management",
        "done",
        summary="요약",
        result="최종 답변",
    )

    payload = _terminal_payload_mapping(state)

    assert payload["id"] == "t_risk"
    assert payload["assignee"] == "risk-management"
    assert payload["status"] == "done"
    assert payload["result"] == "최종 답변"


def test_hr_handoff_discovers_bounded_workspace_evidence_artifact() -> None:
    with tempfile.TemporaryDirectory() as workspace:
        evidence = Path(workspace) / "hr_e2e_evidence.json"
        evidence.write_text('{"schema":"hgfinance.hr-workforce-evidence.v1"}\n')
        state = ChildTaskState(
            task_id="t_hr",
            profile="hr-department",
            status="done",
            body="workflow_root_task_id=root\nworkflow_role=primary",
            workspace_path=workspace,
            metadata={
                "result": {
                    "candidate_snapshot": {"http_status": 200, "candidate_count": 0},
                    "observability": {"http_status": 200},
                    "scorecard": {"http_status": 200},
                }
            },
        )

        provenance = _handoff_provenance(state)

    assert provenance["artifacts"][0]["name"] == "hr_e2e_evidence.json"
    assert len(provenance["artifacts"][0]["sha256"]) == 64


def test_research_handoff_links_string_source_labels_to_answer_urls() -> None:
    state = ChildTaskState(
        task_id="t_research",
        profile="research-department",
        status="done",
        result=(
            "근거: Samsung Newsroom (2026-07-30) "
            "https://news.example/newsroom 및 DNews (2026-08-26) "
            "https://news.example/dnews"
        ),
        metadata={
            "sources": [
                "Samsung Newsroom (2026-07-30)",
                "Naver news_search result citing DNews (2026-08-26)",
            ]
        },
    )

    provenance = _handoff_provenance(state)

    assert provenance["source_references"] == [
        {
            "title": "Samsung Newsroom (2026-07-30)",
            "published": "2026-07-30",
            "url": "https://news.example/newsroom",
        },
        {
            "title": "Naver news_search result citing DNews (2026-08-26)",
            "published": "2026-08-26",
            "url": "https://news.example/dnews",
        },
    ]


def test_research_handoff_provenance_uses_scoped_canonical_url() -> None:
    canonical = (
        "https://news.example/%EC%82%BC%EC%84%B1%EC%A0%84%EC%9E%90-%EC%8B%A4%EC%A0%81"
    )
    handoff = {
        "provenance": {
            "source_references": [
                {
                    "title": "Samsung Newsroom",
                    "published": "2026-07-30",
                    "url": "https://news.example/삼성전자-실적",
                },
                {
                    "title": "Samsung Newsroom",
                    "published": "2026-07-30",
                    "url": "https://news.example/%EC%82%BC%EC%84%B1%EC%A0%84%EC%9E%90-실적",
                },
            ]
        }
    }

    _normalize_research_handoff_provenance(
        handoff,
        ({"body": f"\n## User request\n공식 자료: {canonical}"},),
    )

    assert handoff["provenance"]["source_references"] == [
        {"title": "Samsung Newsroom", "published": "2026-07-30", "url": canonical}
    ]


def test_research_string_sources_match_urls_by_source_host() -> None:
    state = ChildTaskState(
        task_id="t_research",
        profile="research-department",
        status="done",
        result=(
            "삼성전자 뉴스룸 https://news.samsung.com/kr/official "
            "서울경제 https://n.news.naver.com/mnews/article/011/1"
        ),
        metadata={
            "sources": [
                "Samsung Newsroom (2026-07-30)",
                "서울경제 네이버 기사 (2026-07-08)",
            ]
        },
    )

    provenance = _handoff_provenance(state)

    assert [item["url"] for item in provenance["source_references"]] == [
        "https://news.samsung.com/kr/official",
        "https://n.news.naver.com/mnews/article/011/1",
    ]


def test_research_synthesis_canonicalizes_equivalent_url_encoding() -> None:
    primary = {
        "assignee": "research-department",
        "workflow_role": "primary",
        "result": ("출처: https://news.example/삼성전자-실적"),
    }

    result = _canonicalize_research_source_urls(
        "출처: https://news.example/%EC%82%BC%EC%84%B1%EC%A0%84%EC%9E%90-%EC%8B%A4%EC%A0%81",
        (primary,),
    )

    assert (
        result
        == "출처: https://news.example/%EC%82%BC%EC%84%B1%EC%A0%84%EC%9E%90-%EC%8B%A4%EC%A0%81"
    )


def test_research_synthesis_normalizes_partially_encoded_scoped_url() -> None:
    root = {
        "body": (
            "\n## User request\n공식 자료: "
            "https://news.samsung.com/kr/%EC%82%BC%EC%84%B1%EC%A0%84%EC%9E%90-2026%EB%85%84-"
            "%EB%B6%84기-%EC%8B%A4적-%EB%B0%9C%ED%91%9C"
        )
    }
    result = _canonicalize_research_source_urls(
        "출처: https://news.samsung.com/kr/%EC%82%BC%EC%84%B1%EC%A0%84%EC%9E%90-2026%EB%85%84-"
        "%EB%B6%84기-%EC%8B%A4적-%EB%B0%9C%ED%91%9C",
        (root,),
    )

    assert result == (
        "출처: https://news.samsung.com/kr/%EC%82%BC%EC%84%B1%EC%A0%84%EC%9E%90-2026%EB%85%84-"
        "%EB%B6%84%EA%B8%B0-%EC%8B%A4%EC%A0%81-%EB%B0%9C%ED%91%9C"
    )


def test_research_final_projection_removes_repeated_department_block() -> None:
    content = (
        "### 핵심 판단\n요약\n\n"
        "### 긍정 근거\n1. 근거\n\n"
        "### 반대 근거\n1. 반대\n\n"
        "### 관찰할 촉매·무효화 조건\n조건\n\n"
        "### 자료 기준과 확인하지 못한 자료\n한계\n\n"
        "### 부서별 핵심 의견\n- Research 원문 반복\n\n"
        "### 결론\nPAPER 읽기 전용입니다."
    )

    result = _remove_research_duplicate_section(content)

    assert "### 부서별 핵심 의견" not in result
    assert result.endswith("### 결론\nPAPER 읽기 전용입니다.")


def test_research_final_projection_expands_implicit_source_url() -> None:
    canonical = "https://news.samsung.com/kr/official-result"
    result = _expand_research_implicit_source_urls(
        "2. 계획 근거 URL: 위와 동일(삼성전자 뉴스룸, 2026-07-30)",
        ({"body": f"\n## User request\n공식 자료: {canonical}"},),
    )

    assert result == (
        "2. 계획 근거 URL: https://news.samsung.com/kr/official-result "
        "(삼성전자 뉴스룸, 2026-07-30)"
    )


def test_research_final_projection_deduplicates_conclusion_heading() -> None:
    result = _deduplicate_research_conclusion_headings(
        "### 핵심 판단\n요약\n\n### 결론\n판단입니다.\n\n"
        "### 결론\nPAPER 읽기 전용입니다."
    )

    assert result.count("### 결론") == 1
    assert "판단입니다." in result
    assert "PAPER 읽기 전용입니다." in result


def test_research_source_coordinates_are_not_appended_when_items_are_cited() -> None:
    content = (
        "### 긍정 근거\n1. 사실 (출처: 삼성전자 뉴스룸, 2026-07-30, "
        "https://news.example/official)\n\n"
        "### 반대 근거\n1. 한계 (출처: 보조 기사, 2026-07-08, "
        "https://news.example/secondary)\n\n"
        "### 결론\n조건부 판단"
    )

    assert _append_research_source_coordinates(content, ()) == content


def test_research_primary_plain_section_labels_are_normalized() -> None:
    result = _normalize_research_answer_headings(
        "긍정 근거\n1. 근거\n반대 근거\n1. 반대\n"
        "관찰할 촉매·무효화 조건\n조건\n자료 기준과 확인하지 못한 자료\n한계"
    )

    assert result.count("### 긍정 근거") == 1
    assert result.count("### 반대 근거") == 1
    assert result.count("### 관찰할 촉매·무효화 조건") == 1
    assert result.count("### 자료 기준과 확인하지 못한 자료") == 1


def test_research_synthesis_uses_single_scoped_source_for_malformed_repeats() -> None:
    canonical = (
        "https://news.example/%EC%82%BC%EC%84%B1%EC%A0%84%EC%9E%90-%EC%8B%A4%EC%A0%81"
    )
    primary = {
        "assignee": "research-department",
        "workflow_role": "primary",
        "result": (
            "출처: https://news.example/삼성전자-실적 "
            "반복: https://news.example/%EC%82%BC%EC%A0%84%EC%9E%90-실적"
        ),
    }
    root = {"body": f"\n## User request\n공식 자료: {canonical}"}

    result = _canonicalize_research_source_urls(
        "반복: https://news.example/삼성전자-실적 "
        "다시: https://news.example/%EC%82%BC%EC%A0%84%EC%9E%90-실적",
        (root, primary),
    )

    assert result == f"반복: {canonical} 다시: {canonical}"


def test_research_answer_completeness_rejects_corrupted_synthesis() -> None:
    complete = (
        "### 핵심 판단\n"
        "2026-08-27 확인 결과입니다.\n"
        "### 긍정 근거\n"
        "1. 출처: https://news.example/positive\n"
        "### 반대 근거\n"
        "1. 출처: https://news.example/counter\n"
        "### 관찰할 촉매·무효화 조건\n"
        "촉매와 무효화 조건을 관찰합니다.\n"
        "### 자료 기준과 확인하지 못한 자료\n"
        "확인하지 못한 자료는 없습니다."
    )

    assert _research_answer_is_complete(complete)
    assert not _research_answer_is_complete(complete[:80] + '{"error":""}')


def test_hr_handoff_preserves_timeout_receipt_status_and_error() -> None:
    with tempfile.TemporaryDirectory() as workspace:
        evidence = Path(workspace) / "hr_e2e_evidence.json"
        evidence.write_text(
            '{"schema":"hgfinance.hr-workforce-evidence.v1",'
            '"requests":['
            '{"path":"/workforce/v1/improvements","http_status":200,'
            '"duration_ms":20,"response":{"candidates":[]}},'
            '{"path":"/workforce/v1/departments/observability?lookback_hours=24",'
            '"http_status":null,"duration_ms":30016,"error":"TimeoutError"},'
            '{"path":"/workforce/v1/departments/scorecard-brief",'
            '"http_status":200,"duration_ms":64,"response":{}}],'
            '"summary":{"idle_state_counts":{}}}'
        )
        state = ChildTaskState(
            task_id="t_hr_timeout",
            profile="hr-department",
            status="done",
            body="workflow_root_task_id=root\nworkflow_role=primary",
            workspace_path=workspace,
            metadata={
                "result": {
                    "candidate_snapshot": {"http_status": 200, "candidate_count": 0},
                    "observability": {"http_status": 200, "window_start": "stale"},
                    "scorecard": {"http_status": 200},
                }
            },
        )

        provenance = _handoff_provenance(state, include_evidence_content=True)

    assert provenance["source_reads"]["observability"]["http_status"] is None
    assert provenance["source_reads"]["observability"]["error"] == "TimeoutError"
    assert provenance["normalized_result"]["observability"]["window_start"] is None


def test_hr_qa_handoff_compacts_raw_responses_but_keeps_receipt_coordinates() -> None:
    handoff = {
        "provenance": {
            "evidence_artifact": {
                "schema": "hgfinance.hr-workforce-evidence.v1",
                "summary": {"improvement_candidate_count": 0},
                "requests": [
                    {
                        "path": "/workforce/v1/improvements",
                        "method": "GET",
                        "duration_ms": 20,
                        "http_status": 200,
                        "response_sha256": "a" * 64,
                        "response_bytes": 17,
                        "response": {"candidates": []},
                    }
                ],
            }
        }
    }

    _compact_hr_qa_handoff(handoff)

    evidence = handoff["provenance"]["evidence_artifact"]
    assert evidence["raw_response_bodies_omitted"] is True
    assert evidence["requests"][0]["response_sha256"] == "a" * 64
    assert evidence["requests"][0]["response_bytes"] == 17
    assert "response" not in evidence["requests"][0]


def test_hr_status_normalization_preserves_fractional_iso_timestamp() -> None:
    content = (
        "Worker 상태는 ACTIVE 0·IDLE 2·UNOBSERVED 6·UNAVAILABLE 0으로 확인했습니다. "
        "IDLE은 competing-explanation-worker(2026-08-26T13:40:32.408Z, 11.7시간)입니다."
    )
    primary = {
        "id": "hr",
        "assignee": "hr-department",
        "status": "done",
        "body": "workflow_root_task_id=root\nworkflow_role=primary",
        "final_answer": content,
        "metadata": {
            "result": {
                "candidate_snapshot": {"http_status": 200, "candidate_count": 0},
                "observability": {
                    "http_status": 200,
                    "statuses": {"IDLE": 2, "UNOBSERVED": 6},
                    "window_start": "2026-08-26T00:00:00Z",
                    "window_end": "2026-08-27T00:00:00Z",
                },
                "scorecard": {"http_status": 200},
            }
        },
    }

    enriched = _augment_hr_final_answer(
        content,
        root_task_id="root",
        task_payloads=(primary,),
    )

    assert "2026-08-26T13:40:32.408Z" in enriched
    assert ").408Z" not in enriched
    assert "ACTIVE 0" not in enriched
    assert "UNAVAILABLE 0" not in enriched


def test_hr_projection_does_not_publish_unknown_delivery_status_without_receipt() -> (
    None
):
    primary = {
        "id": "hr",
        "assignee": "hr-department",
        "status": "done",
        "body": "workflow_root_task_id=root\nworkflow_role=primary",
        "comments": [
            {
                "body": (
                    "hgfinance.department-notion-delivery.v1 "
                    "delivery_status=DELIVERED readback_status=VERIFIED"
                )
            }
        ],
        "metadata": {
            "result": {
                "candidate_snapshot": {"http_status": 200, "candidate_count": 0},
                "observability": {
                    "http_status": 200,
                    "statuses": {"IDLE": 2, "UNOBSERVED": 6},
                    "window_start": "2026-08-26T00:00:00Z",
                    "window_end": "2026-08-27T00:00:00Z",
                },
                "scorecard": {
                    "http_status": 200,
                    "window_start": "2026-08-26T00:00:00Z",
                    "window_end": "2026-08-27T00:00:00Z",
                },
            }
        },
    }

    enriched = _augment_hr_final_answer(
        "검증 결과 PASS입니다.",
        root_task_id="root",
        task_payloads=(primary,),
    )

    assert "상태 확인 필요" not in enriched
    assert "### 전달 확인" not in enriched


def test_active_unscoped_child_does_not_poison_root_cache() -> None:
    class Client:
        environment: ClassVar[dict[str, object]] = {}

        def show(self, task_id: str):
            return {
                "id": task_id,
                "assignee": "qa-department",
                "status": "running",
                "body": "workflow_role=qa",
            }

    service = CeoSupervisorService(Client())

    root_id, _, _ = service._active_progress_payloads(
        task_id="qa-task",
        event={},
    )

    assert root_id is None
    assert service._cached_workflow_root("qa-task") is None


def test_startup_recovery_skips_roots_with_existing_synthesis() -> None:
    root_body = (
        build_root_body("query", "request")
        + "\nselected_primary_profiles=risk-management"
    )

    class Client:
        def recovery_candidate_rows(self):
            return (
                {
                    "id": "root",
                    "status": "done",
                    "created_at": 1,
                    "body": root_body,
                    "has_active_primary": False,
                    "has_analysis_child": True,
                    "has_terminal_primary": True,
                    "has_synthesis": True,
                },
            )

        def show(self, _task_id):
            raise AssertionError("an already-synthesized root must not be hydrated")

    service = CeoSupervisorService(Client())

    assert service.reconcile_existing_workflows() == ()


class NoAnalysisChildrenOriginGuardTest(unittest.TestCase):
    """자식 없는 루트에 '사용자에게 물어보라' 카드를 찍는 조건을 고정한다.

    회귀 근거(2026-08-14 실측): 공장 자동 생성 카드(공장 주기·공장 개선)는 자식
    없이 혼자 끝나는 게 정상인데 supervisor 가 그것까지 워크플로로 보고
    REQUEST_USER_INPUT 카드를 만들었다. CEO 에이전트는 "무엇을 물어야 하는지
    지시에 없다"며 blocked 로 보냈고, 같은 제목의 카드가 43 장 쌓였다.
    """

    def test_factory_root_does_not_get_user_input_card(self) -> None:
        decision = decide_supervisor(
            SupervisorState("root", (), root_is_user_query=False)
        )
        self.assertIsNone(decision)

    def test_user_query_root_still_asks(self) -> None:
        decision = decide_supervisor(
            SupervisorState("root", (), root_is_user_query=True)
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.REQUEST_USER_INPUT)

    def test_completed_synthesis_does_not_create_clarification(self) -> None:
        synthesis = child(
            "synthesis",
            "ceo-agent",
            "done",
            body=(
                "workflow_role=synthesis\nhgfinance.ceo-supervisor.v1 action=SYNTHESIZE"
            ),
        )

        decision = decide_supervisor(
            SupervisorState("root", (synthesis,), root_is_user_query=True)
        )

        self.assertIsNone(decision)


class UserPaperCanonicalSynthesisTest(unittest.TestCase):
    def _state(self, *, qa_enabled: bool = False) -> SupervisorState:
        primary = ChildTaskState(
            task_id="trading",
            profile="trading-department",
            status="done",
            result="trusted PAPER receipt",
            final_answer="trusted PAPER receipt",
            body=(
                "workflow_root_task_id=root\n"
                "workflow_role=primary\n"
                "hgfinance.user-paper-order-request.v1"
            ),
        )
        return SupervisorState(
            "root",
            (primary,),
            workflow_mode="binding",
            qa_enabled=qa_enabled,
            qa_blocks_response=qa_enabled,
            selected_primary_profiles=("trading-department",),
            root_is_user_query=True,
            allow_primary_passthrough=True,
            paper_order=True,
        )

    def test_non_gated_user_paper_receipt_uses_one_canonical_synthesis(self) -> None:
        state = self._state()

        self.assertIsNone(_single_primary_passthrough_child(state))
        decision = decide_supervisor(state)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(decision.reason, "binding_paper_structured_template")

    def test_qa_does_not_gate_binding_response(self) -> None:
        state = self._state(qa_enabled=True)

        self.assertIsNone(_single_primary_passthrough_child(state))
        decision = decide_supervisor(state)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(decision.reason, "binding_paper_structured_template")


class UserQueryPriorityTest(unittest.TestCase):
    """사람이 기다리는 카드가 공장 카드보다 대기열 앞에 서는지 고정한다.

    근거(2026-08-14 실측): Hermes ready 큐가 priority DESC 정렬인데 우리는
    아무 카드에도 우선순위를 안 줘서, 공장이 슬롯을 물고 ready 23 장이 쌓인
    사이 사용자 질의가 6 분 넘게 대기했다.
    """

    def test_user_query_body_gets_priority_over_factory_default(self) -> None:
        from orchestration.canonical_profiles import (
            USER_QUERY_PRIORITY,
            CanonicalKanbanTaskRequest,
        )

        self.assertGreater(USER_QUERY_PRIORITY, 0)
        factory = CanonicalKanbanTaskRequest(
            "research-department", "공장 주기", "body", "k1"
        )
        self.assertEqual(factory.priority, 0)
        query = CanonicalKanbanTaskRequest(
            "research-liaison",
            "질의",
            "origin=user-query\nbody",
            "k2",
            priority=USER_QUERY_PRIORITY,
        )
        self.assertGreater(query.priority, factory.priority)

    def test_priority_must_be_int(self) -> None:
        from orchestration.canonical_profiles import CanonicalKanbanTaskRequest

        with self.assertRaises(ValueError):
            CanonicalKanbanTaskRequest("qa-department", "t", "b", "k", priority="9")

    def test_research_user_child_gets_research_lane_priority(self) -> None:
        import json
        import subprocess

        calls: list[list[str]] = []

        def runner(args, **kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"id": "research"}), ""
            )

        from orchestration.canonical_profiles import RESEARCH_QUERY_PRIORITY

        with tempfile.TemporaryDirectory() as tmp:
            client = HermesKanbanClient(
                runner=runner,
                environment={"HERMES_KANBAN_HOME": tmp},
            )
            client.create_task(
                title="Research user child",
                body="origin=user-query\nworkflow_role=primary",
                assignee="research-department",
                parent_task_ids=("root",),
                idempotency_key="root:primary:research-department",
            )

        assert calls
        priority_index = calls[0].index("--priority")
        assert calls[0][priority_index + 1] == str(RESEARCH_QUERY_PRIORITY)


class AnswerBodyHandoffTest(unittest.TestCase):
    """부서가 만든 답변 본문이 QA·종합까지 살아서 가는지 고정한다.

    회귀 근거(2026-08-14 실측): 창구가 외국인 순매수 상위 10 표를 만들었는데
    QA·종합 카드에 summary 한 줄만 실려 사용자 응답이 result:null 로 나갔다.
    """

    # 실제 창구 답변의 형태 - 표 + 근거 좌표 + 기준일 + 한계 명시.
    # answer_contract 가 보는 네 항목이 전부 들어 있어야 gaps 가 비어야 한다.
    ANSWER = (
        "2026-08-13 기준 외국인 순매수 상위\n"
        "| 1 | SK하이닉스 | 000660 | 649,842주 |\n"
        "근거: investor_flow TR=t1717 citation=150ae2d8b8c1849e\n"
        "2026-08-14 는 장중 미집계라 제외했다."
    )

    def _bodies_for(self, **child_kwargs: str) -> list[str]:
        bodies = []
        for role_body in ("workflow_role=primary", ""):
            state = SupervisorState(
                "root",
                (
                    child(
                        "r",
                        "research-department",
                        "done",
                        body=role_body,
                        **child_kwargs,
                    ),
                ),
            )
            decision = decide_supervisor(state)
            self.assertIsNotNone(decision)
            bodies.append(decision.body or "")
        return bodies

    def test_answer_body_reaches_downstream_cards(self) -> None:
        for body in self._bodies_for(result=self.ANSWER):
            self.assertIn("SK하이닉스", body)
            self.assertNotIn("answer_body_missing", body)

    def test_complete_answer_is_graded_trustworthy(self) -> None:
        # 본문·근거·기준일·한계가 모두 있으면 QA 가 의심할 항목이 없어야 한다.
        for body in self._bodies_for(result=self.ANSWER):
            self.assertIn('"answer_trustworthy": true', body)
            self.assertNotIn("answer_gaps", body)

    def test_answer_without_evidence_is_flagged_for_qa(self) -> None:
        # 본문은 있는데 근거가 없는 답이 조용히 통과하는 것이 가장 위험하다.
        bare = "삼성전자 외국인 순매수는 649,842주로 집계됐습니다. 상위 종목입니다."
        for body in self._bodies_for(result=bare):
            self.assertIn('"answer_usable": true', body)
            self.assertIn('"answer_trustworthy": false', body)
            self.assertIn("근거 좌표 없음", body)

    def test_missing_answer_body_is_flagged_not_papered_over(self) -> None:
        # 요약만 있고 본문이 없으면 그 사실이 그대로 보여야 한다.
        for body in self._bodies_for(result=""):
            self.assertIn("answer_body_missing", body)

    def test_run_metadata_result_is_used_as_canonical_answer_body(self) -> None:
        # Production-shaped handoff: kanban_complete(result=...) is surfaced
        # through the latest run metadata before task-level hydration.
        state = SupervisorState(
            "root",
            (
                ChildTaskState.from_hermes(
                    {
                        "id": "r",
                        "assignee": "research-department",
                        "status": "done",
                        "body": (
                            "workflow_root_task_id=root\n"
                            "workflow_role=primary\n"
                            "workflow_mode=analysis"
                        ),
                        "runs": [{"metadata": {"result": self.ANSWER}}],
                    }
                ),
            ),
            selected_primary_profiles=("research-department",),
            workflow_mode="analysis",
        )

        self.assertEqual(state.usable_analysis_children[0].result, self.ANSWER)
        self.assertEqual(len(state.usable_analysis_children), 1)


class SupervisorPolicyTest(unittest.TestCase):
    def test_latest_run_failure_and_missing_dependencies_are_preserved(self) -> None:
        task = ChildTaskState.from_hermes(
            {
                "id": "r",
                "assignee": "research-department",
                "status": "blocked",
                "body": ("workflow_root_task_id=root\nworkflow_role=primary"),
                "runs": [
                    {
                        "status": "crashed",
                        "outcome": "crashed",
                        "error": (
                            "worker exited cleanly without calling "
                            "kanban_complete — protocol violation"
                        ),
                        "metadata": {
                            "missing_dependencies": ["current_price"],
                        },
                    }
                ],
            }
        )

        self.assertEqual(task.outcome, "crashed")
        self.assertEqual(task.failure_kind, "protocol")
        self.assertEqual(task.missing_dependencies, ("current_price",))
        self.assertIn("kanban_complete", task.error)

    def test_department_progress_distinguishes_dependency_and_protocol(self) -> None:
        dependency = _department_progress_text(
            "risk-management",
            "blocked",
            missing_dependencies=("current_position",),
        )
        protocol = _department_progress_text(
            "research-department",
            "gave_up",
            failure_kind="protocol",
        )

        self.assertIn("포트폴리오 노출 확인 대기", dependency)
        self.assertIn("정상적으로 인계되지 않았", protocol)
        self.assertNotIn("의존성이 부족", protocol)

    def test_provider_quota_blocked_without_result_is_not_labeled_partial(self) -> None:
        content = _department_progress_text(
            "research-department",
            "blocked",
            summary="PROVIDER_QUOTA internal detail must stay hidden",
            failure_category="PROVIDER_QUOTA",
        )

        self.assertIsNotNone(content)
        self.assertIn("분석을 완료하지 못했습니다", content)
        self.assertIn("쿼터 제한", content)
        self.assertIn("확보한 분석", content)
        self.assertIn("없음", content)
        self.assertNotIn("제한된 결과", content)
        self.assertNotIn("internal detail", content)

    def test_provider_auth_blocked_without_result_is_safe(self) -> None:
        content = _department_progress_text(
            "risk-management",
            "blocked",
            summary="PROVIDER_AUTH raw credential detail must stay hidden",
            failure_category="PROVIDER_AUTH",
        )

        self.assertIsNotNone(content)
        self.assertIn("인증 문제", content)
        self.assertIn("확보한 분석", content)
        self.assertIn("없음", content)
        self.assertNotIn("제한된 결과", content)
        self.assertNotIn("credential detail", content)

    def test_blocked_partial_result_keeps_partial_body_and_label(self) -> None:
        content = _department_progress_text(
            "quant-backtest-department",
            "blocked",
            summary="PROVIDER_QUOTA",
            analysis_result="partial trend evidence",
            failure_category="PROVIDER_QUOTA",
        )

        self.assertIsNotNone(content)
        self.assertIn("제한된 결과", content)
        self.assertIn("partial trend evidence", content)
        self.assertIn("미확보", content)
        self.assertNotIn("분석을 완료하지 못했습니다", content)

    def test_analysis_synthesis_labels_partial_result_usability(self) -> None:
        usable = (
            "2026-08-20 기준 분석입니다. result body with citation=12345678 "
            "and unavailable limits are stated."
        )
        selected = (
            "research-department",
            "quant-backtest-department",
            "risk-management",
        )
        cases = (
            (3, "complete"),
            (2, "partial"),
            (1, "limited_confidence"),
            (0, "blocked"),
        )

        for usable_count, expected in cases:
            children = tuple(
                child(
                    f"task-{index}",
                    profile,
                    "done" if index < usable_count else "blocked",
                    result=usable if index < usable_count else "",
                    summary="" if index >= usable_count else "usable",
                )
                for index, profile in enumerate(selected)
            )
            state = SupervisorState(
                "root",
                children,
                selected_primary_profiles=selected,
                workflow_mode="analysis",
            )

            decision = _analysis_synthesis_decision(state)

            self.assertIsNotNone(decision)
            self.assertIn(
                f"synthesis_availability={expected}",
                decision.body,
            )
            self.assertIn(
                f"usable_primary_count={usable_count}",
                decision.body,
            )

    def test_dynamic_routing_runs_qa_for_selected_primary_children_only(self) -> None:
        state = SupervisorState(
            "root",
            (
                child("r", "research-department", "done"),
                child("risk", "risk-management", "done"),
            ),
        )
        decision = decide_supervisor(state)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(decision.assignee, "ceo-agent")
        self.assertEqual(decision.parent_task_ids, ("r", "risk"))

    def test_primary_results_ready_creates_async_qa_and_fast_synthesis(self) -> None:
        state = SupervisorState(
            "root",
            (
                child(
                    "research",
                    "research-department",
                    "done",
                    body="workflow_role=primary",
                    result=(
                        "2026-08-22 analysis body citation=12345678 "
                        "with stated limitations."
                    ),
                ),
                child(
                    "accounting",
                    "accounting-portfolio-department",
                    "done",
                    body="workflow_role=primary",
                    result=(
                        "2026-08-22 accounting body citation=87654321 "
                        "with stated limitations."
                    ),
                ),
            ),
        )

        first = decide_supervisor(state)
        self.assertIsNotNone(first)
        self.assertEqual(first.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(first.reason, "primary_results_ready_fast_path")
        self.assertEqual(first.parent_task_ids, ("research", "accounting"))
        self.assertIn("QA runs independently", first.body)
        self.assertIn("not a synthesis prerequisite", first.body)

        with_qa = SupervisorState(
            "root",
            state.children
            + (
                child(
                    "qa",
                    "qa-department",
                    "running",
                    body="workflow_role=qa",
                ),
            ),
        )
        second = decide_supervisor(with_qa)
        self.assertIsNotNone(second)
        self.assertEqual(second.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(second.reason, "primary_results_ready_fast_path")
        self.assertEqual(second.parent_task_ids, ("research", "accounting"))
        self.assertNotIn("qa", second.parent_task_ids)
        self.assertIn("QA runs independently", second.body)
        self.assertIn("not a synthesis prerequisite", second.body)
        self.assertNotIn("after QA", second.body.casefold())

    def test_all_blocked_primary_results_skip_qa_and_synthesize_failure_aware(
        self,
    ) -> None:
        state = SupervisorState(
            "root",
            (
                child(
                    "research",
                    "research-department",
                    "blocked",
                    block_kind="capability",
                    summary="PROVIDER_QUOTA",
                    result="",
                ),
                child(
                    "quant",
                    "quant-backtest-department",
                    "blocked",
                    block_kind="capability",
                    summary="PROVIDER_QUOTA",
                    result="",
                ),
                child(
                    "risk",
                    "risk-management",
                    "blocked",
                    block_kind="capability",
                    summary="PROVIDER_AUTH",
                    result="",
                ),
            ),
            selected_primary_profiles=(
                "research-department",
                "quant-backtest-department",
                "risk-management",
            ),
            workflow_mode="analysis",
            qa_required=True,
        )

        decision = decide_supervisor(state)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(decision.reason, "primary_results_ready_fast_path")
        self.assertEqual(decision.parent_task_ids, ())
        self.assertIn("synthesis_availability=blocked", decision.body)

    def test_non_root_synthesis_does_not_poison_root_cache(self) -> None:
        class SynthesisClient:
            def show(self, task_id: str):
                return {
                    "id": task_id,
                    "assignee": "ceo-agent",
                    "status": "done",
                    "body": (
                        "workflow_root_task_id=root\n"
                        "workflow_role=synthesis\n"
                        "workflow_mode=analysis\n"
                    ),
                }

        service = CeoSupervisorService(SynthesisClient())

        handled, decision = service._materialize_completed_analysis_root_fast(
            task_id="synthesis",
            kind="completed",
        )

        self.assertFalse(handled)
        self.assertIsNone(decision)
        self.assertIsNone(service._cached_workflow_root("synthesis"))

    def test_replan_is_scope_bound_without_root_execution_dependency(self) -> None:
        client = FakeClient()
        client.payloads[0].update(status="blocked", block_reason="source unavailable")
        decision = CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "replan-1", "task_id": "r", "kind": "blocked"}
        )
        self.assertEqual(decision.action, SupervisorAction.CREATE_TASK)
        self.assertEqual(client.created, [])
        self.assertEqual(client.unblocked, ["r"])

    def test_blocked_transient_can_retry_and_other_blocked_can_replan(self) -> None:
        retry = decide_supervisor(
            SupervisorState(
                "root",
                (child("r", "research-department", "blocked", block_kind="transient"),),
            )
        )
        self.assertEqual(retry.action, SupervisorAction.RETRY_TASK)

        replan = decide_supervisor(
            SupervisorState(
                "root",
                (
                    child(
                        "r",
                        "research-department",
                        "blocked",
                        block_reason="source unavailable",
                    ),
                ),
            )
        )
        self.assertEqual(replan.action, SupervisorAction.CREATE_TASK)
        self.assertEqual(replan.assignee, "research-department")

    def test_triage_qa_is_terminal_and_never_auto_unblocked(self) -> None:
        state = SupervisorState(
            "root",
            (
                child(
                    "trading",
                    "trading-department",
                    "done",
                    body="workflow_root_task_id=root\nworkflow_role=primary",
                    result="공식 조회 결과",
                ),
                child(
                    "qa",
                    "qa-department",
                    "triage",
                    body="workflow_root_task_id=root\nworkflow_role=qa",
                    block_reason="repeated provider credential failure",
                ),
            ),
            workflow_mode="binding",
            root_is_user_query=True,
            selected_primary_profiles=("trading-department",),
        )

        decision = decide_supervisor(state)

        self.assertTrue(state.qa_children[0].terminal)
        self.assertTrue(state.qa_children[0].failed)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(decision.reason, "binding_primary_completed_final_synthesis")

    def test_blocked_needs_input_is_not_treated_as_failure(self) -> None:
        decision = decide_supervisor(
            SupervisorState(
                "root",
                (
                    child(
                        "r",
                        "research-department",
                        "blocked",
                        block_kind="needs_input",
                        block_reason="user input required",
                    ),
                ),
            )
        )
        self.assertEqual(decision.action, SupervisorAction.REQUEST_USER_INPUT)

    def test_retry_and_wakeup_limits_abort(self) -> None:
        retry_limit = decide_supervisor(
            SupervisorState(
                "root",
                (child("r", "research-department", "failed", retry_count=2),),
            )
        )
        self.assertEqual(retry_limit.action, SupervisorAction.BLOCK_ABORT)
        wakeup_limit = decide_supervisor(
            SupervisorState(
                "root",
                (child("r", "research-department", "done"),),
                wakeups=8,
            )
        )
        self.assertEqual(wakeup_limit.action, SupervisorAction.BLOCK_ABORT)

    def test_binding_partial_primary_synthesizes_from_successful_dependencies(
        self,
    ) -> None:
        state = SupervisorState(
            "root",
            (
                child(
                    "research",
                    "research-department",
                    "done",
                    result="authoritative partial result",
                ),
                child(
                    "accounting",
                    "accounting-portfolio-department",
                    "blocked",
                    block_reason="worker cgroup exhausted",
                    retry_count=2,
                ),
            ),
            replan_count=2,
            workflow_mode="binding",
            root_is_user_query=True,
            selected_primary_profiles=(
                "research-department",
                "accounting-portfolio-department",
            ),
        )

        decision = decide_supervisor(state)

        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(decision.parent_task_ids, ("research",))
        self.assertIn("defer_reason=primary_department_partial_failure", decision.body)

    def test_binding_partial_primary_becomes_deterministic_defer_after_qa(self) -> None:
        state = SupervisorState(
            "root",
            (
                child(
                    "research",
                    "research-department",
                    "done",
                    result="SK하이닉스 시장 자료는 확인됨",
                ),
                child(
                    "accounting",
                    "accounting-portfolio-department",
                    "blocked",
                    block_reason="worker cgroup exhausted",
                    retry_count=2,
                ),
                child(
                    "qa",
                    "qa-department",
                    "done",
                    result="부분 결과와 실패 범위를 검증함",
                ),
            ),
            replan_count=2,
            workflow_mode="binding",
            root_is_user_query=True,
            selected_primary_profiles=(
                "research-department",
                "accounting-portfolio-department",
            ),
        )

        decision = decide_supervisor(state)

        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(decision.reason, "binding_partial_defer_template")
        self.assertEqual(decision.parent_task_ids, ("research",))
        self.assertEqual(decision.initial_status, "blocked")

    def test_binding_qa_failure_still_returns_fail_closed_defer(self) -> None:
        state = SupervisorState(
            "root",
            (
                child("research", "research-department", "done", result="result"),
                child(
                    "qa",
                    "qa-department",
                    "failed",
                    retry_count=2,
                    block_reason="worker terminated",
                ),
            ),
            workflow_mode="binding",
            root_is_user_query=True,
            selected_primary_profiles=("research-department",),
        )

        decision = decide_supervisor(state)

        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(decision.reason, "binding_primary_completed_final_synthesis")
        self.assertEqual(decision.parent_task_ids, ("research",))

    def test_qa_done_triggers_final_synthesis(self) -> None:
        decision = decide_supervisor(
            SupervisorState(
                "root",
                (
                    child("r", "research-department", "done"),
                    child("qa", "qa-department", "done"),
                ),
            )
        )
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(decision.assignee, "ceo-agent")

    def test_ceo_can_explicitly_skip_default_qa(self) -> None:
        decision = decide_supervisor(
            SupervisorState(
                "root",
                (child("trading", "trading-department", "done"),),
                qa_required=False,
            )
        )
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)

    def test_unknown_structured_assignee_is_rejected(self) -> None:
        with self.assertRaises(SupervisorValidationError):
            parse_supervisor_output(
                {
                    "action": "CREATE_TASK",
                    "parent_task_id": "root",
                    "assignee": "risk-department",
                    "title": "risk",
                    "body": "body",
                }
            )

    def test_analysis_synthesis_is_eligible_while_qa_runs(self) -> None:
        decision = decide_supervisor(
            SupervisorState(
                "root",
                (
                    child("r", "research-department", "done"),
                    child("risk", "risk-management", "done"),
                    child("qa", "qa-department", "running"),
                ),
            )
        )
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(decision.parent_task_ids, ("r", "risk"))
        self.assertNotIn("qa", decision.parent_task_ids)

    def test_unselected_primary_does_not_trigger_retry_or_block(self) -> None:
        decision = decide_supervisor(
            SupervisorState(
                "root",
                (
                    child(
                        "research",
                        "research-department",
                        "done",
                        body="workflow_role=primary",
                    ),
                    child(
                        "quant",
                        "quant-backtest-department",
                        "failed",
                        body="workflow_role=primary",
                    ),
                ),
                selected_primary_profiles=("research-department",),
            )
        )
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(decision.parent_task_ids, ("research",))

    def test_binding_synthesis_does_not_wait_for_qa(self) -> None:
        decision = decide_supervisor(
            SupervisorState(
                "root",
                (
                    child("r", "research-department", "done"),
                    child("qa", "qa-department", "running"),
                ),
                workflow_mode="binding",
            )
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(decision.parent_task_ids, ("r",))
        self.assertIn("post-response asynchronous", decision.body)

    def test_analysis_qa_failure_does_not_block_response_synthesis(self) -> None:
        decision = decide_supervisor(
            SupervisorState(
                "root",
                (
                    child("r", "research-department", "done"),
                    child("qa", "qa-department", "failed"),
                ),
            )
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(decision.parent_task_ids, ("r",))

    def test_analysis_qa_terminal_outcomes_do_not_enter_fast_path_failure(self) -> None:
        for qa_status in ("blocked", "crashed", "timed_out", "gave_up", "failed"):
            with self.subTest(qa_status=qa_status):
                decision = decide_supervisor(
                    SupervisorState(
                        "root",
                        (
                            child("r", "research-department", "done"),
                            child("qa", "qa-department", qa_status),
                        ),
                    )
                )
                self.assertIsNotNone(decision)
                self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
                self.assertEqual(decision.parent_task_ids, ("r",))

    def test_explicit_roles_prevent_control_or_qa_from_becoming_analysis(self) -> None:
        state = SupervisorState(
            "root",
            (
                child(
                    "primary",
                    "accounting-portfolio-department",
                    "done",
                    body="workflow_role=primary",
                ),
                child(
                    "qa",
                    "qa-department",
                    "running",
                    body="workflow_role=qa",
                ),
                child(
                    "synthesis",
                    "ceo-agent",
                    "todo",
                    body="workflow_role=synthesis",
                ),
            ),
        )
        self.assertEqual([c.task_id for c in state.analysis_children], ["primary"])
        self.assertEqual([c.task_id for c in state.qa_children], ["qa"])

    def test_root_selection_is_machine_readable_and_legacy_prose_is_supported(
        self,
    ) -> None:
        self.assertEqual(
            selected_primary_profiles_from_body(
                "selected_primary_profiles=research-department,risk-management"
            ),
            ("research-department", "risk-management"),
        )
        self.assertEqual(
            selected_primary_profiles_from_body(
                "Dynamic departments selected:\n"
                "Research, Risk Management, and Accounting/Portfolio."
            ),
            (
                "research-department",
                "risk-management",
                "accounting-portfolio-department",
            ),
        )

    def test_root_selection_falls_back_to_root_comment(self) -> None:
        selected = selected_primary_profiles_from_task(
            {
                "id": "root",
                "body": "hgfinance.ceo-workflow-scope.v1\nworkflow_mode=analysis",
                "comments": [
                    {
                        "body": (
                            "selected_primary_profiles=research-department,"
                            "quant-backtest-department,risk-management,"
                            "accounting-portfolio-department"
                        )
                    }
                ],
            }
        )
        self.assertEqual(
            selected,
            (
                "research-department",
                "quant-backtest-department",
                "risk-management",
                "accounting-portfolio-department",
            ),
        )

    def test_precise_root_comment_overrides_legacy_body_prose(self) -> None:
        selected = selected_primary_profiles_from_task(
            {
                "id": "root",
                "body": (
                    "Dynamic departments selected:\n"
                    "Research and Risk Management.\n"
                    "After all primary tasks complete, run independent QA by default."
                ),
                "comments": [
                    {
                        "body": (
                            "selected_primary_profiles=research-department,"
                            "quant-backtest-department,risk-management,"
                            "accounting-portfolio-department"
                        )
                    }
                ],
            }
        )
        self.assertEqual(
            selected,
            (
                "research-department",
                "quant-backtest-department",
                "risk-management",
                "accounting-portfolio-department",
            ),
        )

    def test_selected_primary_set_excludes_foreign_and_background_tasks(self) -> None:
        selected = (
            "research-department",
            "risk-management",
            "accounting-portfolio-department",
        )
        scoped = "workflow_root_task_id=root\nworkflow_role=primary"
        background = (
            "workflow_root_task_id=root\n"
            "workflow_plane=continuous_research\n"
            "workflow_role=background_research"
        )
        state = SupervisorState(
            "root",
            (
                child("research", "research-department", "done", body=scoped),
                child("risk", "risk-management", "done", body=scoped),
                child(
                    "accounting", "accounting-portfolio-department", "done", body=scoped
                ),
                child(
                    "foreign",
                    "research-department",
                    "done",
                    body="workflow_root_task_id=other-root\nworkflow_role=primary",
                ),
                child("background", "continuous-research", "done", body=background),
            ),
            selected_primary_profiles=selected,
        )
        decision = decide_supervisor(state)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(decision.parent_task_ids, ("research", "risk", "accounting"))

    def test_duplicate_primary_set_is_not_hidden_by_readiness_selector(self) -> None:
        selected = (
            "research-department",
            "risk-management",
            "accounting-portfolio-department",
        )
        body = "workflow_root_task_id=root\nworkflow_role=primary"
        children = tuple(
            child(f"{profile}-{suffix}", profile, "done", body=body)
            for suffix in ("a", "b")
            for profile in selected
        )
        state = SupervisorState("root", children, selected_primary_profiles=selected)
        self.assertEqual(state.duplicate_primary_profiles, selected)
        self.assertEqual(state.ready_count, 0)
        self.assertFalse(state.primary_ready)
        with self.assertLogs(
            "orchestration.adapters.ceo_supervisor", level="WARNING"
        ) as logs:
            self.assertIsNone(decide_supervisor(state))
        self.assertTrue(
            any("primary-duplicate-detected" in message for message in logs.output)
        )
        self.assertFalse(any("primary-ready" in message for message in logs.output))

    def test_readiness_is_unique_profile_count_and_never_exceeds_selected(self) -> None:
        selected = (
            "research-department",
            "quant-backtest-department",
            "risk-management",
            "accounting-portfolio-department",
        )
        state = SupervisorState(
            "root",
            tuple(
                child(f"{profile}-1", profile, "done", body="workflow_role=primary")
                for profile in selected
            ),
            selected_primary_profiles=selected,
        )
        self.assertEqual(state.ready_count, 4)
        self.assertLessEqual(state.ready_count, len(selected))
        self.assertTrue(state.primary_ready)

    def test_failed_primary_retry_is_not_ready_until_same_task_recovers(self) -> None:
        selected = (
            "research-department",
            "quant-backtest-department",
            "risk-management",
            "accounting-portfolio-department",
        )
        children = (
            child(
                "research",
                "research-department",
                "running",
                body="workflow_role=primary",
            ),
            child(
                "quant",
                "quant-backtest-department",
                "done",
                body="workflow_role=primary",
            ),
            child("risk", "risk-management", "done", body="workflow_role=primary"),
            child(
                "accounting",
                "accounting-portfolio-department",
                "done",
                body="workflow_role=primary",
            ),
        )
        state = SupervisorState("root", children, selected_primary_profiles=selected)
        self.assertEqual(state.ready_count, 3)
        self.assertFalse(state.primary_ready)
        self.assertIsNone(decide_supervisor(state))

    def test_failed_primary_retries_same_task_without_creating_duplicate(self) -> None:
        client = FakeClient()
        client.payloads[0].update(
            status="failed",
            runs=[{"outcome": "failed"}],
        )

        decision = CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "same-primary-retry", "task_id": "r", "kind": "crashed"}
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.RETRY_TASK)
        self.assertEqual(client.unblocked, ["r"])
        self.assertEqual(client.created, [])

    def test_analysis_synthesis_waits_for_every_selected_primary(self) -> None:
        selected = (
            "research-department",
            "quant-backtest-department",
            "risk-management",
            "accounting-portfolio-department",
        )
        body = "workflow_root_task_id=root\nworkflow_role=primary"
        state = SupervisorState(
            "root",
            (
                child("research", "research-department", "done", body=body),
                child("risk", "risk-management", "done", body=body),
            ),
            qa_required=False,
            selected_primary_profiles=selected,
        )

        self.assertEqual(
            state.missing_primary_profiles,
            ("quant-backtest-department", "accounting-portfolio-department"),
        )
        self.assertIsNone(decide_supervisor(state))

    def test_analysis_synthesis_requires_unique_complete_selected_set(self) -> None:
        selected = (
            "research-department",
            "quant-backtest-department",
            "risk-management",
            "accounting-portfolio-department",
        )
        body = "workflow_root_task_id=root\nworkflow_role=primary"
        children = tuple(
            child(profile.split("-")[0], profile, "done", body=body)
            for profile in selected
        )

        decision = decide_supervisor(
            SupervisorState(
                "root",
                children,
                qa_required=False,
                selected_primary_profiles=selected,
            )
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(
            decision.parent_task_ids,
            tuple(item.task_id for item in children),
        )

    def test_analysis_synthesis_stays_async_while_qa_runs(self) -> None:
        selected = ("research-department", "risk-management")
        body = "workflow_root_task_id=root\nworkflow_role=primary"
        state = SupervisorState(
            "root",
            (
                child("research", "research-department", "done", body=body),
                child("risk", "risk-management", "done", body=body),
                child(
                    "qa",
                    "qa-department",
                    "running",
                    body="workflow_root_task_id=root\nworkflow_role=qa",
                ),
            ),
            selected_primary_profiles=selected,
        )

        decision = decide_supervisor(state)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertNotIn("qa", decision.parent_task_ids)

    def test_analysis_synthesis_includes_terminal_blocked_primary(self) -> None:
        selected = (
            "research-department",
            "quant-backtest-department",
            "risk-management",
            "accounting-portfolio-department",
        )
        body = "workflow_root_task_id=root\nworkflow_role=primary"
        children = tuple(
            child(
                profile.split("-")[0],
                profile,
                "blocked" if profile == "accounting-portfolio-department" else "done",
                body=body,
                block_reason=(
                    "financial statement unavailable"
                    if profile == "accounting-portfolio-department"
                    else ""
                ),
            )
            for profile in selected
        )

        decision = decide_supervisor(
            SupervisorState(
                "root",
                children,
                qa_required=False,
                selected_primary_profiles=selected,
            )
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(
            decision.parent_task_ids,
            tuple(item.task_id for item in children if item.done),
        )
        self.assertIn("financial statement unavailable", decision.body)

    def test_four_selected_primary_parents_exclude_unmarked_duplicates(self) -> None:
        selected = (
            "research-department",
            "quant-backtest-department",
            "risk-management",
            "accounting-portfolio-department",
        )
        marked_body = "workflow_root_task_id=root\nworkflow_role=primary"
        unmarked_body = "workflow_root_task_id=root\nworkflow_role="
        children = tuple(
            child(f"marked-{index}", profile, "done", body=marked_body)
            for index, profile in enumerate(selected)
        ) + tuple(
            child(f"legacy-{index}", profile, "done", body=unmarked_body)
            for index, profile in enumerate(selected)
        )
        state = SupervisorState("root", children, selected_primary_profiles=selected)

        self.assertEqual(len(state.analysis_children), 4)
        first = decide_supervisor(state)
        self.assertEqual(first.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(first.parent_task_ids, tuple(f"marked-{i}" for i in range(4)))

        with_qa = SupervisorState(
            "root",
            children
            + (child("qa", "qa-department", "running", body="workflow_role=qa"),),
            selected_primary_profiles=selected,
        )
        synthesis = decide_supervisor(with_qa)
        self.assertEqual(synthesis.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(
            synthesis.parent_task_ids,
            tuple(f"marked-{i}" for i in range(4)),
        )

    def test_selected_set_waits_without_request_user_input_when_children_are_late(
        self,
    ) -> None:
        state = SupervisorState(
            "root",
            (),
            selected_primary_profiles=("research-department",),
        )
        self.assertIsNone(decide_supervisor(state))

    def test_primary_idempotency_key_is_stable(self) -> None:
        self.assertEqual(
            primary_idempotency_key("root", "research-department"),
            "root:primary:research-department",
        )

    def test_continuous_research_is_excluded_even_with_research_profile(self) -> None:
        background_tasks = tuple(
            child(
                f"background-{index}",
                "research-department",
                "done",
                body=(
                    "workflow_root_task_id=root\n"
                    "workflow_plane=continuous_research\n"
                    "workflow_role=background_research\n"
                    "hgfinance.continuous-research.v1"
                ),
            )
            for index in range(100)
        )
        state = SupervisorState(
            "root",
            (
                child("request-research", "research-department", "done"),
                child(
                    "foreign-primary",
                    "research-department",
                    "done",
                    body="workflow_root_task_id=old-root\nworkflow_role=primary",
                ),
                *background_tasks,
            ),
        )

        self.assertEqual(
            [item.task_id for item in state.analysis_children], ["request-research"]
        )
        decision = decide_supervisor(state)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(decision.parent_task_ids, ("request-research",))

        fast_path = decide_supervisor(
            SupervisorState(
                "root",
                state.children
                + (child("qa", "qa-department", "running", body="workflow_role=qa"),),
            )
        )
        self.assertEqual(fast_path.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(fast_path.parent_task_ids, ("request-research",))

    def test_background_research_can_use_unregistered_future_profile(self) -> None:
        task = ChildTaskState.from_hermes(
            {
                "id": "background",
                "assignee": "research-intelligence",
                "status": "done",
                "body": (
                    "workflow_plane=continuous_research\n"
                    "workflow_role=background_research"
                ),
            }
        )
        self.assertTrue(task.is_background_research)
        self.assertFalse(task.is_analysis)

    def test_background_research_failure_does_not_enter_fast_loop(self) -> None:
        state = SupervisorState(
            "root",
            (
                child("request-research", "research-department", "done"),
                child(
                    "continuous-failure",
                    "research-intelligence",
                    "crashed",
                    body=(
                        "workflow_plane=continuous_research\n"
                        "workflow_role=background_research"
                    ),
                ),
            ),
        )

        decision = decide_supervisor(state)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(decision.parent_task_ids, ("request-research",))


class HermesCreateBoundaryTest(unittest.TestCase):
    def test_supervisor_rejects_invalid_qa_primary_before_cli(self) -> None:
        import json
        import subprocess

        calls: list[list[str]] = []

        def runner(args, **kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps({"id": "task-created"}),
                "",
            )

        with tempfile.TemporaryDirectory() as tmp:
            client = HermesKanbanClient(
                runner=runner,
                environment={"HERMES_KANBAN_HOME": tmp},
            )
            with self.assertRaises(SupervisorValidationError):
                client.create_task(
                    title="QA primary",
                    body="workflow_root_task_id=root\nworkflow_role=primary",
                    assignee="qa-department",
                    parent_task_ids=("root",),
                    idempotency_key="root:primary:qa-department",
                )

        self.assertEqual(calls, [])

    def test_late_create_after_root_done_still_rejects_qa_primary(self) -> None:
        import json
        import subprocess

        calls: list[list[str]] = []

        def runner(args, **kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, json.dumps({"id": "late"}), "")

        with tempfile.TemporaryDirectory() as tmp:
            client = HermesKanbanClient(
                runner=runner,
                environment={"HERMES_KANBAN_HOME": tmp},
            )
            with self.assertRaises(SupervisorValidationError):
                client.create_task(
                    title="Late QA primary",
                    body=(
                        "workflow_root_task_id=root\n"
                        "workflow_role=primary\n"
                        "root_status=done"
                    ),
                    assignee="qa-department",
                    parent_task_ids=("root",),
                    idempotency_key="root:primary:qa-department",
                )

        self.assertEqual(calls, [])

    def test_supervisor_allows_governance_qa_and_valid_primary(self) -> None:
        import json
        import subprocess

        calls: list[list[str]] = []

        def runner(args, **kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps({"id": f"task-{len(calls)}"}),
                "",
            )

        with tempfile.TemporaryDirectory() as tmp:
            client = HermesKanbanClient(
                runner=runner,
                environment={"HERMES_KANBAN_HOME": tmp},
            )
            governance = client.create_task(
                title="QA governance",
                body="workflow_root_task_id=root\nworkflow_role=qa",
                assignee="qa-department",
                parent_task_ids=("root",),
                idempotency_key="root:qa:qa-department",
            )
            primary = client.create_task(
                title="Research primary",
                body="workflow_root_task_id=root\nworkflow_role=primary",
                assignee="research-department",
                parent_task_ids=("root",),
                idempotency_key="root:primary:research-department",
            )

        self.assertEqual(governance["id"], "task-1")
        self.assertEqual(primary["id"], "task-2")
        self.assertEqual(len(calls), 2)


class FakeClient:
    def __init__(self) -> None:
        self.payloads = [
            {
                "id": "r",
                "assignee": "research-department",
                "status": "done",
                "summary": "research",
                "body": "workflow_root_task_id=root\nworkflow_role=primary",
            },
            {
                "id": "risk",
                "assignee": "risk-management",
                "status": "done",
                "summary": "risk",
                "body": "workflow_root_task_id=root\nworkflow_role=primary",
            },
        ]
        self.created: list[dict[str, object]] = []
        self.unblocked: list[str] = []
        self.completed: list[dict[str, object]] = []
        self.blocked: list[str] = []
        self.comments: list[dict[str, str]] = []
        self.root_body = ""

    def workflow(self, task_id: str):
        return "root", tuple(self.payloads)

    def show(self, task_id: str):
        payload = {"id": task_id, "comments": list(self.comments)}
        if task_id == "root":
            payload["body"] = self.root_body
        return payload

    def create_task(self, **kwargs):
        self.created.append(kwargs)
        task_id = f"new-{len(self.created)}"
        # Supervisor-created children are the durable action/idempotency
        # record.  Ordinary test fixtures remain easy to update in-place.
        self.payloads.append(
            {
                "id": task_id,
                "assignee": kwargs["assignee"],
                "status": "ready",
                "body": kwargs["body"],
            }
        )
        return {"id": task_id}

    def comment_task(self, task_id: str, text: str) -> None:
        self.comments.append({"task_id": task_id, "body": text})

    def unblock_task(self, task_id: str) -> None:
        self.unblocked.append(task_id)

    def complete_task(self, task_id: str, **kwargs: object) -> None:
        self.completed.append({"task_id": task_id, **kwargs})

    def block_task(self, task_id: str, reason: str) -> None:
        self.blocked.append(task_id)


def test_ceo_synthesis_receives_owned_self_review_guardrails() -> None:
    client = FakeClient()
    client.root_body = build_root_body(
        "삼성전자 분석",
        "req-ceo-self-review",
        ceo_self_improvement_hint={
            "schema_version": "hgfinance.memo-harness.ceo-self-improvement.v1",
            "owner": "ceo",
            "mode": "corrective_guardrails_only",
            "verified_qa_required": True,
            "raw_payloads_sent": False,
            "guardrails": [
                {
                    "id": "CEO_TRACE_EVIDENCE_RECHECK",
                    "rule": (
                        "Treat an unavailable authoritative execution trace as unverified. "
                        "A published receipt or metadata-only record is not proof that a "
                        "trace exists."
                    ),
                }
            ],
        },
    )
    service = CeoSupervisorService(client)
    state = SupervisorState(
        "root",
        (child("research", "research-department", "done", result="근거 포함 결과"),),
        root_body=client.root_body,
    )
    decision = SupervisorDecision(
        SupervisorAction.SYNTHESIZE,
        "root",
        assignee="ceo-agent",
        title="CEO final synthesis",
        body=(
            "hgfinance.ceo-supervisor.v1 action=SYNTHESIZE\n"
            "workflow_plane=response"
        ),
        parent_task_ids=("research",),
    )

    service._execute(decision, state)

    assert len(client.created) == 1
    synthesis_body = str(client.created[0]["body"])
    assert "CEO_TRACE_EVIDENCE_RECHECK" in synthesis_body
    assert "CEO self-improvement guardrails" in synthesis_body
    assert "D5_CHECK_" not in synthesis_body
    assert "QA does not command or mutate the CEO" in synthesis_body


def test_risk_primary_result_contract_repairs_transport_token_from_metadata():
    class EditingClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.edited: list[dict[str, object]] = []

        def edit_task(self, task_id: str, **kwargs: object) -> None:
            self.edited.append({"task_id": task_id, **kwargs})

    client = EditingClient()
    task = {
        "id": "t_risk_contract",
        "assignee": "risk-management",
        "status": "done",
        "summary": "Risk handoff",
        "result": "success",
        "run_metadata": {"final_answer": "리스크 최종 분석 결과입니다."},
        "body": "workflow_root_task_id=root\nworkflow_role=primary",
    }

    repaired = CeoSupervisorService(client)._repair_risk_primary_result_contract(task)

    assert repaired["result"] == "리스크 최종 분석 결과입니다."
    assert repaired["final_answer"] == "리스크 최종 분석 결과입니다."
    assert len(client.edited) == 1
    assert client.edited[0]["result"] == "리스크 최종 분석 결과입니다."
    assert (
        client.edited[0]["metadata"]["risk_result_contract_version"] == "risk.result.v1"
    )


def test_risk_primary_result_contract_accepts_durable_run_result():
    class EditingClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.edited: list[dict[str, object]] = []

        def edit_task(self, task_id: str, **kwargs: object) -> None:
            self.edited.append({"task_id": task_id, **kwargs})

    client = EditingClient()
    task = {
        "id": "t_risk_run_result",
        "assignee": "risk-management",
        "status": "done",
        "result": "",
        "latest_summary": "generic completion summary",
        "runs": [
            {
                "metadata": {
                    "result": "영속 run 결과에서 복구한 리스크 분석입니다.",
                }
            }
        ],
        "body": "workflow_root_task_id=root\nworkflow_role=primary",
    }

    repaired = CeoSupervisorService(client)._repair_risk_primary_result_contract(task)

    assert repaired["result"] == "영속 run 결과에서 복구한 리스크 분석입니다."
    assert len(client.edited) == 1


def test_risk_primary_result_contract_accepts_markerless_legacy_card():
    class EditingClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.edited: list[dict[str, object]] = []

        def edit_task(self, task_id: str, **kwargs: object) -> None:
            self.edited.append({"task_id": task_id, **kwargs})

    client = EditingClient()
    task = {
        "id": "t_risk_markerless_legacy",
        "assignee": "risk-management",
        "status": "done",
        "result": "",
        "runs": [
            {
                "metadata": {
                    "final_answer": "마커가 없는 legacy Risk 카드의 영속 답변입니다.",
                }
            }
        ],
        "body": "PAPER 및 읽기 전용 Risk 검증입니다.",
    }

    repaired = CeoSupervisorService(client)._repair_risk_primary_result_contract(task)

    assert repaired["result"] == "마커가 없는 legacy Risk 카드의 영속 답변입니다."
    assert repaired["metadata"]["risk_result_contract_version"] == "risk.result.v1"
    assert len(client.edited) == 1


class PostResponseQaAuditTest(unittest.TestCase):
    def test_supervisor_cannot_materialize_pre_response_qa(self) -> None:
        client = FakeClient()
        service = CeoSupervisorService(client)
        state = SupervisorState(
            "root",
            (child("research", "research-department", "done"),),
        )
        decision = SupervisorDecision(
            SupervisorAction.RUN_QA,
            "root",
            assignee="qa-department",
            title="legacy QA",
            body="hgfinance.ceo-supervisor.v1 action=RUN_QA",
            parent_task_ids=("research",),
        )

        with self.assertRaises(SupervisorValidationError):
            service._execute(decision, state)
        self.assertEqual(client.created, [])

    def test_qa_is_created_only_after_delivery_and_receives_ceo_input(self) -> None:
        timeline: list[str] = []

        class DeliverySpy:
            def deliver(self, **_kwargs):
                timeline.append("deliver")
                return "sent"

            def deliver_to_existing_thread(self, **_kwargs):
                timeline.append("deliver")
                return "sent"

        class OrderingClient(FakeClient):
            def create_task(self, **kwargs):
                timeline.append(f"create:{kwargs['assignee']}")
                return super().create_task(**kwargs)

        client = OrderingClient()
        root_id = "root"
        root = {
            "id": root_id,
            "assignee": "ceo-agent",
            "status": "done",
            "body": build_root_body(
                "삼성전자 위험과 근거를 설명해줘", "req-post-response"
            ),
        }
        primary = {
            "id": "research",
            "assignee": "research-department",
            "status": "done",
            "result": "공식 연구 근거",
            "final_answer": "공식 연구 근거",
            "body": (f"workflow_root_task_id={root_id}\nworkflow_role=primary"),
        }
        synthesis_body = (
            f"workflow_root_task_id={root_id}\n"
            "workflow_role=synthesis\n"
            "workflow_mode=analysis\n"
            "hgfinance.ceo-supervisor.v1 action=SYNTHESIZE\n"
            "CEO가 받은 연구 결과"
        )
        synthesis = {
            "id": "ceo-response",
            "assignee": "ceo-agent",
            "status": "done",
            "result": "CEO 최종 응답",
            "final_answer": "CEO 최종 응답",
            "body": synthesis_body,
        }

        service = CeoSupervisorService(
            client,
            discord_delivery=DeliverySpy(),
        )
        service._project_terminal_task(
            root_task_id=root_id,
            task_id="ceo-response",
            task_payloads=(root, primary, synthesis),
            event={
                "event_id": "post-response-1",
                "task_id": "ceo-response",
                "kind": "completed",
            },
        )

        self.assertEqual(timeline, ["deliver", "create:qa-department"])
        self.assertEqual(len(client.created), 1)
        qa = client.created[0]
        self.assertEqual(qa["parent_task_ids"], ("ceo-response",))
        self.assertIn("qa_phase=post_response", qa["body"])
        self.assertIn("qa_timing=after_ceo_response", qa["body"])
        self.assertIn("ceo_input_is_identical=true", qa["body"])
        self.assertIn('"workflow_observations": {', qa["body"])
        self.assertIn('"langsmith_evidence": {', qa["body"])
        self.assertIn('"trace_closed": false', qa["body"])
        self.assertIn('"raw_payloads_sent": false', qa["body"])
        self.assertIn('"metadata_only": true', qa["body"])
        self.assertIn("CEO가 받은 연구 결과", qa["body"])
        self.assertIn(synthesis_body.replace("\n", "\\n"), qa["body"])
        self.assertIn("CEO 최종 응답", qa["body"])

    def test_qa_receives_root_backed_trading_evidence_without_new_task(self) -> None:
        root_id = "root-trading-evidence"
        root = {
            "id": root_id,
            "assignee": "ceo-agent",
            "status": "done",
            "body": (
                f"workflow_root_task_id={root_id}\n"
                "workflow_role=control\n"
                "workflow_mode=analysis\n"
                "selected_primary_profiles=trading-department\n"
                "hgfinance.mandate-snapshot.v1\n"
                "content_hash=abcdef0123456789abcdef0123456789\n"
                "\n## Accounting Engine snapshot (read-only, hgfinance.accounting-snapshot.v1)\n"
                '{"as_of":"2026-08-27T02:23:48Z","nav":"506468430"}\n\n'
                "## User request\n삼성전자 PAPER 읽기 전용 검증"
            ),
        }
        trading = {
            "id": "trading-primary",
            "assignee": "trading-department",
            "status": "done",
            "body": (
                f"workflow_root_task_id={root_id}\n"
                "workflow_role=primary\n"
                "workflow_mode=analysis"
            ),
            "result": (
                "2026-08-27 기준 주문 방향·수량·주문 유형이 없어 후보를 만들 수 없습니다. "
                "실제 주문·체결·원장 변경은 수행하지 않았습니다."
            ),
        }
        synthesis = {
            "id": "ceo-trading-response",
            "assignee": "ceo-agent",
            "status": "done",
            "result": "CEO 최종 응답입니다.",
            "final_answer": "CEO 최종 응답입니다.",
            "body": (
                f"workflow_root_task_id={root_id}\n"
                "workflow_role=synthesis\n"
                "workflow_mode=analysis\n"
                "hgfinance.ceo-supervisor.v1 action=SYNTHESIZE"
            ),
        }

        class DeliverySpy:
            def deliver(self, **_kwargs):
                return "sent"

            def deliver_to_existing_thread(self, **_kwargs):
                return "sent"

        client = FakeClient()
        service = CeoSupervisorService(client, discord_delivery=DeliverySpy())
        service._project_terminal_task(
            root_task_id=root_id,
            task_id="ceo-trading-response",
            task_payloads=(root, trading, synthesis),
            event={
                "event_id": "trading-evidence-1",
                "task_id": "ceo-trading-response",
                "kind": "completed",
            },
        )

        self.assertEqual(len(client.created), 1)
        qa_body = client.created[0]["body"]
        self.assertIn("거래 부서 검증 근거", qa_body)
        self.assertIn('"answer_trustworthy": true', qa_body)

    def test_web_synthesis_without_discord_context_does_not_fail_root(self) -> None:
        timeline: list[str] = []

        class DeliverySpy:
            def deliver(self, **_kwargs):
                timeline.append("deliver")
                return "missing_context"

            def deliver_to_existing_thread(self, **_kwargs):
                timeline.append("deliver")
                return "missing_context"

        class OrderingClient(FakeClient):
            def create_task(self, **kwargs):
                timeline.append(f"create:{kwargs['assignee']}")
                return super().create_task(**kwargs)

        root_id = "root-web"
        root = {
            "id": root_id,
            "assignee": "ceo-agent",
            "status": "done",
            "body": (build_root_body("웹 요청", "req-web") + "\nsource=web"),
        }
        synthesis = {
            "id": "web-response",
            "assignee": "ceo-agent",
            "status": "done",
            "result": "웹 최종 응답",
            "final_answer": "웹 최종 응답",
            "body": (
                f"workflow_root_task_id={root_id}\n"
                "workflow_role=synthesis\n"
                "workflow_mode=analysis\n"
                "hgfinance.ceo-supervisor.v1 action=SYNTHESIZE"
            ),
        }
        client = OrderingClient()
        service = CeoSupervisorService(
            client,
            discord_delivery=DeliverySpy(),
        )

        service._project_terminal_task(
            root_task_id=root_id,
            task_id="web-response",
            task_payloads=(root, synthesis),
            event={
                "event_id": "web-response-1",
                "task_id": "web-response",
                "kind": "completed",
            },
        )

        self.assertEqual(timeline, ["create:qa-department"])
        self.assertEqual(len(client.created), 1)
        self.assertIn('"delivery_status": "not_applicable"', client.created[0]["body"])
        self.assertIn('"not_applicable": true', client.created[0]["body"])

    def test_discord_delivery_failure_still_schedules_qa_audit(self) -> None:
        class DeliverySpy:
            def deliver(self, **_kwargs):
                return "missing_context"

            def deliver_to_existing_thread(self, **_kwargs):
                return "missing_thread"

        root_id = "root-discord-failure"
        root = {
            "id": root_id,
            "assignee": "ceo-agent",
            "status": "done",
            "body": build_root_body(
                "Discord 전달 실패를 점검해줘", "req-discord-failure"
            ),
        }
        synthesis = {
            "id": "discord-failure-response",
            "assignee": "ceo-agent",
            "status": "done",
            "result": "CEO 최종 응답",
            "final_answer": "CEO 최종 응답",
            "body": (
                f"workflow_root_task_id={root_id}\n"
                "workflow_role=synthesis\n"
                "workflow_mode=analysis\n"
                "hgfinance.ceo-supervisor.v1 action=SYNTHESIZE"
            ),
        }
        client = FakeClient()
        service = CeoSupervisorService(client, discord_delivery=DeliverySpy())

        service._project_terminal_task(
            root_task_id=root_id,
            task_id="discord-failure-response",
            task_payloads=(root, synthesis),
            event={
                "event_id": "discord-failure-1",
                "task_id": "discord-failure-response",
                "kind": "completed",
            },
        )

        self.assertEqual(len(client.created), 1)
        qa = client.created[0]
        self.assertEqual(qa["parent_task_ids"], ("discord-failure-response",))
        self.assertIn('"delivery_status": "missing_context"', qa["body"])
        self.assertIn('"response_delivered": false', qa["body"])

    def test_completed_empty_synthesis_is_edited_instead_of_completed_again(
        self,
    ) -> None:
        class EditingClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.edited: list[dict[str, object]] = []

            def edit_task(self, task_id: str, **kwargs: object) -> None:
                self.edited.append({"task_id": task_id, **kwargs})

        client = EditingClient()
        root_id = "root"
        synthesis_id = "ceo-response"
        root = {
            "id": root_id,
            "assignee": "ceo-agent",
            "status": "done",
            "body": build_root_body("결과를 설명해줘", "req-repair"),
        }
        synthesis = {
            "id": synthesis_id,
            "assignee": "ceo-agent",
            "status": "done",
            "summary": "복구 가능한 CEO 요약",
            "result": "",
            "final_answer": "",
            "body": (
                f"workflow_root_task_id={root_id}\n"
                "workflow_role=synthesis\n"
                "workflow_mode=analysis\n"
                "hgfinance.ceo-supervisor.v1 action=SYNTHESIZE"
            ),
        }
        client.payloads = [root, synthesis]
        client.root_body = root["body"]

        service = CeoSupervisorService(client)
        service._project_terminal_task(
            root_task_id=root_id,
            task_id=synthesis_id,
            task_payloads=(root, synthesis),
            event={
                "event_id": "repair-1",
                "task_id": synthesis_id,
                "kind": "completed",
            },
        )

        self.assertEqual([item["task_id"] for item in client.edited], [synthesis_id])
        self.assertEqual(client.completed, [])


class WorkforceAdvisoryAttachmentTest(unittest.TestCase):
    def test_hr_primary_receives_existing_snapshot_without_a_second_scorecard(self):
        client = FakeClient()
        client.payloads = []
        service = CeoSupervisorService(client, qa_required=False)
        state = SupervisorState(
            "root",
            (),
            workflow_mode="analysis",
            selected_primary_profiles=("hr-department",),
            workforce_advisory_context=(
                '{"contract":"hgfinance.workforce-advisory.v1",'
                '"capacity":[{"department":"research","error_rate":0.13}]}'
            ),
        )
        decision = SupervisorDecision(
            SupervisorAction.CREATE_TASK,
            "root",
            assignee="hr-department",
            title="CEO delegated hr analysis",
            body="analysis_mode=fast_advisory\n가장 먼저 개선할 부서를 판단하십시오.",
            parent_task_ids=(),
        )

        service._execute(decision, state)

        body = str(client.created[0]["body"])
        self.assertIn("Authoritative Workforce API snapshot", body)
        self.assertIn("hgfinance.workforce-advisory.v1", body)
        self.assertIn('"department":"research"', body)
        self.assertIn("Do not repeat browser, terminal, file", body)

    def test_every_primary_create_gets_one_terminal_result_contract(self) -> None:
        client = FakeClient()
        service = CeoSupervisorService(client, qa_required=False)
        decision = SupervisorDecision(
            SupervisorAction.CREATE_TASK,
            "root",
            assignee="research-department",
            title="CEO delegated research analysis",
            body="analysis_mode=standard_analysis\n분석 결과를 작성하십시오.",
        )

        service._execute(decision, SupervisorState("root", ()))

        body = str(client.created[0]["body"])
        self.assertEqual(
            body.count("Terminal result persistence contract (required):"), 1
        )
        self.assertIn("complete user-facing answer in result", body)


class BindingPartialDeferExecutionTest(unittest.TestCase):
    def test_partial_defer_is_completed_without_an_llm_worker(self) -> None:
        client = FakeClient()
        service = CeoSupervisorService(client)
        state = SupervisorState(
            "root",
            (
                child(
                    "research",
                    "research-department",
                    "done",
                    result="시장 snapshot은 확인했으나 mandate는 unversioned",
                ),
                child(
                    "accounting",
                    "accounting-portfolio-department",
                    "blocked",
                    block_reason="worker cgroup exhausted",
                    retry_count=2,
                ),
                child("qa", "qa-department", "done", result="QA 검토 완료"),
            ),
            replan_count=2,
            workflow_mode="binding",
            root_is_user_query=True,
            selected_primary_profiles=(
                "research-department",
                "accounting-portfolio-department",
            ),
        )
        decision = decide_supervisor(state)

        service._execute(decision, state)

        self.assertEqual(client.created[0]["initial_status"], "blocked")
        self.assertEqual(len(client.completed), 1)
        completed = client.completed[0]
        self.assertIn("**결론: DEFER**", completed["result"])
        self.assertIn("Research 부서", completed["result"])
        self.assertIn("Accounting / Portfolio 부서", completed["result"])
        self.assertFalse(completed["metadata"]["orders_authorized"])

    def test_abort_does_not_mutate_an_already_completed_planning_root(self) -> None:
        client = FakeClient()
        service = CeoSupervisorService(client)
        state = SupervisorState("root", (), parent_status="done")
        decision = SupervisorDecision(
            SupervisorAction.BLOCK_ABORT,
            "root",
            reason="retry_limit_reached",
        )

        service._execute(decision, state)

        self.assertEqual(client.blocked, [])
        self.assertEqual(len(client.comments), 1)
        self.assertIn("recorded_without_root_mutation", client.comments[0]["body"])


class SynthesisTimingInstrumentationTest(unittest.TestCase):
    def test_synthesis_timing_logs_create_boundary_without_payload(self) -> None:
        client = FakeClient()
        client.root_body = build_root_body("analysis", "request-1")
        service = CeoSupervisorService(
            client,
            qa_required=False,
            decider=lambda state: SupervisorDecision(
                SupervisorAction.SYNTHESIZE,
                state.parent_task_id,
                assignee="ceo-agent",
                title="CEO final synthesis",
                body="synthesis body",
                parent_task_ids=tuple(
                    child.task_id for child in state.analysis_children
                ),
            ),
        )
        service._project_terminal_task = lambda **kwargs: None
        service._deliver_department_progress = lambda **kwargs: None
        service._reconcile_department_terminal_progress = lambda **kwargs: None

        with self.assertLogs(
            "orchestration.adapters.ceo_supervisor",
            level="INFO",
        ) as captured:
            service.handle_terminal_event(
                {
                    "event_id": "synthesis-timing-1",
                    "request_id": "request-1",
                    "task_id": "r",
                    "kind": "completed",
                    "_event_persisted_ms": 1_000_000,
                    "_event_detected_ms": 1_000_100,
                    "_event_consumed_ms": 1_000_200,
                }
            )

        timing = next(
            line for line in captured.output if "supervisor-synthesis-timing" in line
        )
        self.assertIn("request_id=request-1", timing)
        self.assertIn("root_id=root", timing)
        self.assertIn("synthesis_task_id=new-1", timing)
        self.assertIn("t7b_t7c_ms=", timing)
        self.assertIn("t0_t8_ms=", timing)
        self.assertNotIn("synthesis body", timing)


class UnmaterializedPrimaryFinalDeliveryTest(unittest.TestCase):
    @staticmethod
    def root_payload(*, final_answer: str = "") -> dict[str, object]:
        return {
            "id": "root-invalid-primary",
            "assignee": "ceo-agent",
            "status": "done",
            "final_answer": final_answer,
            "body": (
                "hgfinance.ceo-workflow-scope.v1\n"
                "workflow_role=planning\n"
                "root_task_role=scope_and_planning\n"
                "planning_terminal_state=done_after_child_creation\n"
                "workflow_mode=analysis\n"
                "origin=user-query\n"
                "analysis_mode=fast_advisory\n"
                "selected_primary_profiles=qa-department\n"
                "delegation_instruction.qa-department=Measure the system response latency.\n"
                "discord_request_id=discord:request-invalid-primary\n"
                "discord_channel_id=channel-invalid-primary\n"
                "discord_message_id=message-invalid-primary\n"
                "discord_thread_id=thread-invalid-primary\n"
            ),
        }

    class Delivery:
        def __init__(self) -> None:
            self.cards: list[dict[str, object]] = []
            self.finals: list[dict[str, object]] = []
            self._final_keys: set[str] = set()

        def upsert_thread_card(self, **kwargs):
            self.cards.append(kwargs)
            return "sent"

        def deliver_to_existing_thread(self, **kwargs):
            response_key = str(kwargs.get("response_key_suffix") or "")
            if response_key in self._final_keys:
                return "deduped"
            self._final_keys.add(response_key)
            self.finals.append(kwargs)
            return "sent"

    class Client:
        environment: ClassVar[dict[str, object]] = {
            "HERMES_HOME": "/tmp/ceo-invalid-primary-final"
        }

        def __init__(self, root: dict[str, object]) -> None:
            self.root = root
            self.created: list[dict[str, object]] = []
            self.completed: list[dict[str, object]] = []

        def workflow_root(self, task_id):
            return self.root["id"]

        def authoritative_workflow_snapshot(self, root_id, task_id):
            return root_id, (), self.root

        def show(self, task_id):
            return self.root

        def create_task(self, **kwargs):
            self.created.append(kwargs)
            return {"id": "empty-primary-synthesis"}

        def complete_task(self, task_id: str, **kwargs: object) -> None:
            self.completed.append({"task_id": task_id, **kwargs})

        def comment_task(self, task_id, text):
            return None

    class LateChildClient(Client):
        def __init__(self, root: dict[str, object], child: dict[str, object]) -> None:
            super().__init__(root)
            self.child = child

        def authoritative_workflow_snapshot(self, root_id, task_id):
            return root_id, (self.child,), self.root

        def show(self, task_id):
            if task_id == self.child["id"]:
                return self.child
            return super().show(task_id)

    @staticmethod
    def late_qa_child(root_id: str, *, status: str = "blocked") -> dict[str, object]:
        return {
            "id": "qa-late-child",
            "assignee": "qa-department",
            "status": status,
            "block_reason": "terminal marker missing after worker exit",
            "body": build_scoped_task_body(
                "QA terminal failure",
                root_id,
                role="qa",
                workflow_mode="analysis",
            ),
        }

    def test_invalid_primary_uses_delegation_card_and_one_direct_final(self) -> None:
        root = self.root_payload(final_answer="CEO usable final answer")
        client = self.Client(root)
        delivery = self.Delivery()
        service = CeoSupervisorService(client, discord_delivery=delivery)
        event = {
            "event_id": "invalid-primary-completed",
            "task_id": root["id"],
            "kind": "completed",
            "_event_persisted_ms": 1_000,
            "_event_detected_ms": 1_001,
            "_event_consumed_ms": 1_002,
        }

        service.handle_terminal_event(event)
        service.handle_terminal_event(event)

        # Planner metadata without a materialized child is not a delegation
        # projection.  The root still gets one final response through the
        # existing direct/failure response plane.
        self.assertEqual(len(delivery.cards), 0)
        self.assertEqual(len(delivery.finals), 1)
        self.assertEqual(delivery.finals[0]["title"], "🧠 CEO 답변")
        self.assertEqual(delivery.finals[0]["content"], "CEO usable final answer")
        self.assertEqual(client.created, [])

    def test_invalid_primary_without_result_creates_one_deferred_synthesis(
        self,
    ) -> None:
        root = self.root_payload()
        client = self.Client(root)
        delivery = self.Delivery()
        service = CeoSupervisorService(client, discord_delivery=delivery)

        status = service._reconcile_unmaterialized_primary_root(
            root_task_id=root["id"],
            root_payload=root,
            task_payloads=(root,),
        )

        self.assertEqual(status, "deferred")
        self.assertEqual(len(delivery.finals), 0)
        self.assertEqual(len(client.created), 1)
        self.assertEqual(client.created[0]["assignee"], "ceo-agent")
        self.assertEqual(client.created[0]["initial_status"], "blocked")
        self.assertIn("workflow_role=synthesis", client.created[0]["body"])
        self.assertIn(
            "synthesis_mode=deterministic_empty_primary_defer",
            client.created[0]["body"],
        )
        self.assertEqual(len(client.completed), 1)
        self.assertIn(
            "분석 primary가 생성되지 않아 부서 결과를 받지 못했습니다.",
            client.completed[0]["result"],
        )
        self.assertIn(
            "empty_primary_not_materialized",
            client.completed[0]["metadata"]["final_answer"],
        )

    def test_materialized_valid_primary_does_not_use_invalid_primary_fallback(
        self,
    ) -> None:
        root = self.root_payload(final_answer="CEO planner metadata")
        root["body"] = (
            str(root["body"])
            .replace(
                "selected_primary_profiles=qa-department",
                "selected_primary_profiles=research-department",
            )
            .replace(
                "delegation_instruction.qa-department=Measure the system response latency.",
                "delegation_instruction.research-department=Research the system response latency.",
            )
        )
        research = {
            "id": "research-primary",
            "assignee": "research-department",
            "status": "done",
            "body": (
                "workflow_root_task_id=root-invalid-primary\n"
                "workflow_role=primary\n"
                "workflow_mode=analysis"
            ),
        }
        client = self.Client(root)
        delivery = self.Delivery()
        service = CeoSupervisorService(client, discord_delivery=delivery)

        status = service._reconcile_unmaterialized_primary_root(
            root_task_id=root["id"],
            root_payload=root,
            task_payloads=(root, research),
        )

        self.assertIsNone(status)
        self.assertEqual(delivery.finals, [])
        self.assertEqual(client.created, [])

    def test_existing_synthesis_owns_final_delivery(self) -> None:
        root = self.root_payload(final_answer="CEO planner metadata")
        synthesis = {
            "id": "existing-synthesis",
            "assignee": "ceo-agent",
            "status": "done",
            "body": (
                "workflow_root_task_id=root-invalid-primary\n"
                "workflow_role=synthesis\n"
                "workflow_mode=analysis"
            ),
        }
        client = self.Client(root)
        delivery = self.Delivery()
        service = CeoSupervisorService(client, discord_delivery=delivery)

        status = service._reconcile_unmaterialized_primary_root(
            root_task_id=root["id"],
            root_payload=root,
            task_payloads=(root, synthesis),
        )

        self.assertIsNone(status)
        self.assertEqual(delivery.finals, [])
        self.assertEqual(client.created, [])

    def test_late_optional_qa_block_delivers_existing_ceo_result_once(self) -> None:
        root = self.root_payload(final_answer="CEO usable final answer")
        child = self.late_qa_child(str(root["id"]))
        client = self.LateChildClient(root, child)
        delivery = self.Delivery()
        service = CeoSupervisorService(client, discord_delivery=delivery)
        service._project_terminal_task = lambda **kwargs: None

        event = {
            "event_id": "late-qa-blocked-1",
            "task_id": child["id"],
            "kind": "blocked",
            "qa_required": False,
        }

        service.handle_terminal_event(event)

        self.assertEqual(len(delivery.finals), 1)
        self.assertEqual(delivery.finals[0]["title"], "🧠 CEO 답변")
        self.assertEqual(delivery.finals[0]["content"], "CEO usable final answer")

    def test_late_child_without_ceo_result_delivers_explicit_failure_once(self) -> None:
        root = self.root_payload()
        child = self.late_qa_child(str(root["id"]))
        client = self.LateChildClient(root, child)
        delivery = self.Delivery()
        service = CeoSupervisorService(client, discord_delivery=delivery)
        service._project_terminal_task = lambda **kwargs: None

        event = {
            "event_id": "late-qa-blocked-2",
            "task_id": child["id"],
            "kind": "blocked",
            "qa_required": False,
        }

        service.handle_terminal_event(event)

        self.assertEqual(len(delivery.finals), 1)
        self.assertEqual(delivery.finals[0]["title"], "⚠️ CEO 처리 결과")
        self.assertIn("완료되지 않았습니다", delivery.finals[0]["content"])
        self.assertIn("최종 분석 결과", delivery.finals[0]["content"])

    def test_planner_summary_is_not_reused_as_late_final(self) -> None:
        root = self.root_payload()
        root["runs"] = [
            {
                "metadata": {
                    "summary": "QA fast_advisory 점검으로 위임했습니다.",
                    "selected_primary_profiles": "qa-department",
                }
            }
        ]
        child = self.late_qa_child(str(root["id"]))
        client = self.LateChildClient(root, child)
        delivery = self.Delivery()
        service = CeoSupervisorService(client, discord_delivery=delivery)

        status = service._reconcile_late_child_finalization(
            root_task_id=str(root["id"]),
            root_payload=root,
            task_payloads=(root, child),
            task_id=str(child["id"]),
        )

        self.assertEqual(status, "sent")
        self.assertEqual(len(delivery.finals), 1)
        self.assertEqual(delivery.finals[0]["title"], "⚠️ CEO 처리 결과")
        self.assertNotIn("위임했습니다", delivery.finals[0]["content"])

    def test_late_child_terminal_replay_is_idempotent(self) -> None:
        root = self.root_payload(final_answer="CEO usable final answer")
        child = self.late_qa_child(str(root["id"]))
        client = self.LateChildClient(root, child)
        delivery = self.Delivery()
        service = CeoSupervisorService(client, discord_delivery=delivery)
        service._project_terminal_task = lambda **kwargs: None

        for event_id in ("late-qa-blocked-3a", "late-qa-blocked-3b"):
            service.handle_terminal_event(
                {
                    "event_id": event_id,
                    "task_id": child["id"],
                    "kind": "blocked",
                    "qa_required": False,
                }
            )

        self.assertEqual(len(delivery.finals), 1)

    def test_late_child_does_not_race_existing_synthesis(self) -> None:
        root = self.root_payload(final_answer="CEO planner metadata")
        child = self.late_qa_child(str(root["id"]))
        synthesis = {
            "id": "existing-synthesis",
            "assignee": "ceo-agent",
            "status": "ready",
            "body": build_scoped_task_body(
                "synthesis",
                str(root["id"]),
                role="synthesis",
                workflow_mode="analysis",
            ),
        }
        client = self.LateChildClient(root, child)
        delivery = self.Delivery()
        service = CeoSupervisorService(client, discord_delivery=delivery)
        service._project_terminal_task = lambda **kwargs: None

        status = service._reconcile_late_child_finalization(
            root_task_id=str(root["id"]),
            root_payload=root,
            task_payloads=(root, child, synthesis),
            task_id=str(child["id"]),
        )

        self.assertIsNone(status)
        self.assertEqual(delivery.finals, [])

    def test_materialized_eligible_primary_keeps_delegated_synthesis_path(self) -> None:
        root = self.root_payload(final_answer="CEO planner metadata")
        root["body"] = str(root["body"]).replace(
            "selected_primary_profiles=qa-department",
            "selected_primary_profiles=research-department",
        )
        research = {
            "id": "research-primary",
            "assignee": "research-department",
            "status": "done",
            "result": "research result",
            "body": build_scoped_task_body(
                "research result",
                str(root["id"]),
                role="primary",
                workflow_mode="analysis",
            ),
        }
        client = self.LateChildClient(root, research)
        delivery = self.Delivery()
        service = CeoSupervisorService(client, discord_delivery=delivery)

        status = service._reconcile_late_child_finalization(
            root_task_id=str(root["id"]),
            root_payload=root,
            task_payloads=(root, research),
            task_id="research-primary",
        )

        self.assertIsNone(status)
        self.assertEqual(delivery.finals, [])


class SupervisorWakeupTest(unittest.TestCase):
    def test_startup_reconciliation_recovers_direct_root_and_blocked_primary(
        self,
    ) -> None:
        class ExistingRootClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                selected = (
                    "research-department",
                    "quant-backtest-department",
                    "risk-management",
                    "accounting-portfolio-department",
                )
                self.root_body = (
                    "hgfinance.ceo-workflow-scope.v1\n"
                    "workflow_role=planning\n"
                    "root_task_role=scope_and_planning\n"
                    "planning_terminal_state=done_after_child_creation\n"
                    "producer=ceo-hermes-direct\n"
                    "request_class=non-binding advisory analysis\n"
                    "selected_primary_profiles=" + ",".join(selected)
                )
                self.payloads = [
                    {
                        "id": f"{profile}-task",
                        "assignee": profile,
                        "status": "blocked"
                        if profile.endswith("portfolio-department")
                        else "done",
                        "summary": profile,
                        "block_reason": "data gap"
                        if profile.endswith("portfolio-department")
                        else "",
                        "body": "workflow_root_task_id=root\nworkflow_role=primary",
                    }
                    for profile in selected
                ]

            def list_tasks(self):
                return ({"id": "root", "status": "done", "body": self.root_body},)

            def show(self, task_id: str):
                payload = super().show(task_id)
                if task_id == "root":
                    payload.update(status="done", body=self.root_body)
                return payload

        client = ExistingRootClient()
        decisions = CeoSupervisorService(client).reconcile_existing_workflows()

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, SupervisorAction.SYNTHESIZE)
        self.assertEqual([item["assignee"] for item in client.created], ["ceo-agent"])
        self.assertEqual(
            set(client.created[0]["parent_task_ids"]),
            {
                "research-department-task",
                "quant-backtest-department-task",
                "risk-management-task",
            },
        )

    def test_startup_reconciliation_recovers_modern_root_from_blocked_primary(
        self,
    ) -> None:
        now = int(time.time())

        class ModernRootClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.root_body = build_root_body(
                    "삼성전자 분석",
                    "discord:followup-101",
                ) + (
                    "\nanalysis_mode=fast_advisory\n"
                    "selected_primary_profiles="
                    "research-department,quant-backtest-department\n"
                    "delegation_instruction.research-department=Research Samsung.\n"
                    "delegation_instruction.quant-backtest-department=Quant Samsung."
                )
                self.payloads = [
                    {
                        "id": "research-task",
                        "assignee": "research-department",
                        "status": "done",
                        "body": (
                            "workflow_root_task_id=root\n"
                            "workflow_role=primary\n"
                            "workflow_mode=analysis"
                        ),
                    },
                    {
                        "id": "quant-task",
                        "assignee": "quant-backtest-department",
                        "status": "blocked",
                        "block_kind": "needs_input",
                        "block_reason": "target context missing",
                        "body": (
                            "workflow_root_task_id=root\n"
                            "workflow_role=primary\n"
                            "workflow_mode=analysis"
                        ),
                    },
                ]
                self.candidate_calls = 0
                self.list_calls = 0

            def recovery_candidate_rows(self):
                self.candidate_calls += 1
                return (
                    {
                        "id": "root",
                        "status": "done",
                        "created_at": now - 180,
                        "completed_at": now - 150,
                        "body": self.root_body,
                    },
                )

            def list_tasks(self):
                self.list_calls += 1
                return ()

            def show(self, task_id: str):
                if task_id == "root":
                    return {
                        "id": "root",
                        "status": "done",
                        "created_at": now - 180,
                        "completed_at": now - 150,
                        "body": self.root_body,
                    }
                return super().show(task_id)

        client = ModernRootClient()
        service = CeoSupervisorService(client)
        seen: list[dict[str, object]] = []

        def record(event):
            seen.append(dict(event))
            return SupervisorDecision(
                SupervisorAction.REQUEST_USER_INPUT,
                "root",
                target_task_id="quant-task",
            )

        service.handle_terminal_event = record

        decisions = service.reconcile_existing_workflows()

        self.assertEqual(len(decisions), 1)
        self.assertEqual(seen[0]["task_id"], "quant-task")
        self.assertEqual(seen[0]["kind"], "blocked")
        self.assertEqual(client.candidate_calls, 1)
        self.assertEqual(client.list_calls, 0)

    def test_startup_reconciliation_ignores_old_completed_roots(self) -> None:
        now = int(time.time())

        class OldRootClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.root_body = build_root_body("old", "old-request")
                self.show_calls = 0

            def list_tasks(self):
                return (
                    {
                        "id": "root",
                        "status": "done",
                        "created_at": now - 1200,
                        "completed_at": now - 601,
                        "body": self.root_body,
                    },
                )

            def show(self, task_id: str):
                self.show_calls += 1
                return super().show(task_id)

        client = OldRootClient()

        decisions = CeoSupervisorService(client).reconcile_existing_workflows()

        self.assertEqual(decisions, ())
        self.assertEqual(client.show_calls, 0)

    def test_completed_synthesis_reconciliation_replays_missed_terminal_event(
        self,
    ) -> None:
        class CompletedSynthesisClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.root_body = (
                    "hgfinance.ceo-workflow-scope.v1\n"
                    "workflow_role=planning\n"
                    "root_task_role=scope_and_planning\n"
                    "planning_terminal_state=done_after_child_creation\n"
                    "workflow_mode=analysis"
                )
                self.payloads = [
                    {
                        "id": "synthesis-done",
                        "assignee": "ceo-agent",
                        "status": "done",
                        "summary": "CEO final",
                        "body": (
                            "hgfinance.ceo-workflow-scope.v1\n"
                            "workflow_root_task_id=t_12345678\n"
                            "workflow_role=synthesis\n"
                            "workflow_mode=analysis\n"
                            "hgfinance.ceo-supervisor.v1 action=SYNTHESIZE"
                        ),
                    }
                ]

            def list_tasks(self):
                return (
                    {
                        "id": "synthesis-done",
                        "assignee": "ceo-agent",
                        "status": "done",
                        "completed_at": int(time.time()),
                        "body": self.payloads[0]["body"],
                    },
                )

            def show(self, task_id: str):
                if task_id == "t_12345678":
                    return {
                        "id": "t_12345678",
                        "assignee": "ceo-agent",
                        "status": "done",
                        "body": self.root_body,
                    }
                if task_id == "synthesis-done":
                    payload = dict(self.payloads[0])
                    payload["completed_at"] = int(time.time())
                    return payload
                return super().show(task_id)

            def workflow(self, task_id: str):
                return "t_12345678", tuple(self.payloads)

        client = CompletedSynthesisClient()
        service = CeoSupervisorService(client)

        seen = []

        original = service.handle_terminal_event

        def recording_handle(event):
            seen.append(dict(event))
            return original(event)

        service.handle_terminal_event = recording_handle

        recovered = service.reconcile_completed_syntheses()

        self.assertEqual(recovered, ("synthesis-done",))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["task_id"], "synthesis-done")
        self.assertEqual(seen[0]["kind"], "completed")
        self.assertEqual(
            seen[0]["event_id"],
            "reconcile-synthesis:synthesis-done:done",
        )

    def test_completed_synthesis_reconciliation_ignores_non_synthesis_tasks(
        self,
    ) -> None:
        class NonSynthesisClient(FakeClient):
            def list_tasks(self):
                return (
                    {
                        "id": "risk",
                        "assignee": "risk-management",
                        "status": "done",
                        "body": ("workflow_root_task_id=root\\nworkflow_role=primary"),
                    },
                )

        client = NonSynthesisClient()
        service = CeoSupervisorService(client)

        self.assertEqual(service.reconcile_completed_syntheses(), ())

    def test_thread_backed_synthesis_delivers_only_to_request_thread(self) -> None:
        timeline = []

        class DeliverySpy:
            def __init__(self) -> None:
                self.parent_calls = []
                self.thread_calls = []

            def deliver(self, **kwargs):
                self.parent_calls.append(kwargs)
                return "sent"

            def deliver_to_existing_thread(self, **kwargs):
                self.thread_calls.append(kwargs)
                timeline.append("discord")
                return "sent"

        class ProjectionSpy:
            def project(self, **kwargs):
                timeline.append("notion")

        class DeliveryClient(FakeClient):
            def __init__(self, home: str) -> None:
                super().__init__()
                self.environment = {"HERMES_HOME": home}

        root_id = "t_11111111"
        synth_id = "t_22222222"

        root = {
            "id": root_id,
            "assignee": "ceo-agent",
            "status": "done",
            "body": (
                "hgfinance.ceo-workflow-scope.v1\n"
                "workflow_role=planning\n"
                "workflow_mode=analysis\n"
                "discord_request_id=discord:1539501364021825556\n"
                "discord_channel_id=1536997434507657261\n"
                "discord_message_id=1539501364021825556\n"
                "discord_thread_id=1539501364021825556"
            ),
        }

        synthesis = {
            "id": synth_id,
            "assignee": "ceo-agent",
            "status": "done",
            "summary": "CEO final answer",
            "body": (
                "hgfinance.ceo-workflow-scope.v1\n"
                f"workflow_root_task_id={root_id}\n"
                "workflow_role=synthesis\n"
                "workflow_mode=analysis\n"
                "hgfinance.ceo-supervisor.v1 action=SYNTHESIZE"
            ),
        }

        with tempfile.TemporaryDirectory() as home:
            delivery = DeliverySpy()
            client = DeliveryClient(home)
            service = CeoSupervisorService(
                client,
                synthesis_projection=ProjectionSpy(),
                discord_delivery=delivery,
            )

            service._project_terminal_task(
                root_task_id=root_id,
                task_id=synth_id,
                task_payloads=(root, synthesis),
                event={
                    "event_id": "thread-final",
                    "task_id": synth_id,
                    "kind": "completed",
                },
            )

        self.assertEqual(len(delivery.parent_calls), 0)
        self.assertEqual(len(delivery.thread_calls), 1)
        self.assertEqual(
            delivery.thread_calls[0]["root_task"]["body"],
            root["body"],
        )
        self.assertEqual(
            delivery.thread_calls[0]["title"],
            "🧠 CEO 종합",
        )
        self.assertEqual(
            timeline,
            ["discord", "notion"],
            "non-binding projection must not delay final Discord delivery",
        )

    def test_synthesis_without_thread_keeps_parent_fallback(self) -> None:
        class DeliverySpy:
            def __init__(self) -> None:
                self.parent_calls = []
                self.thread_calls = []

            def deliver(self, **kwargs):
                self.parent_calls.append(kwargs)
                return "sent"

            def deliver_to_existing_thread(self, **kwargs):
                self.thread_calls.append(kwargs)
                return "missing_thread"

        class DeliveryClient(FakeClient):
            def __init__(self, home: str) -> None:
                super().__init__()
                self.environment = {"HERMES_HOME": home}

        root_id = "t_33333333"
        synth_id = "t_44444444"

        root = {
            "id": root_id,
            "assignee": "ceo-agent",
            "status": "done",
            "body": (
                "hgfinance.ceo-workflow-scope.v1\n"
                "workflow_role=planning\n"
                "workflow_mode=analysis\n"
                "discord_request_id=discord:message-1\n"
                "discord_channel_id=channel-1\n"
                "discord_message_id=message-1"
            ),
        }

        synthesis = {
            "id": synth_id,
            "assignee": "ceo-agent",
            "status": "done",
            "summary": "CEO final answer",
            "body": (
                "hgfinance.ceo-workflow-scope.v1\n"
                f"workflow_root_task_id={root_id}\n"
                "workflow_role=synthesis\n"
                "workflow_mode=analysis\n"
                "hgfinance.ceo-supervisor.v1 action=SYNTHESIZE"
            ),
        }

        with tempfile.TemporaryDirectory() as home:
            delivery = DeliverySpy()
            client = DeliveryClient(home)
            service = CeoSupervisorService(
                client,
                discord_delivery=delivery,
            )

            service._project_terminal_task(
                root_task_id=root_id,
                task_id=synth_id,
                task_payloads=(root, synthesis),
                event={
                    "event_id": "parent-fallback",
                    "task_id": synth_id,
                    "kind": "completed",
                },
            )

        self.assertEqual(len(delivery.parent_calls), 1)
        self.assertEqual(len(delivery.thread_calls), 1)

    def test_terminal_child_creates_parallel_qa_and_synthesis(self) -> None:
        client = FakeClient()
        service = CeoSupervisorService(client)

        first = service.handle_terminal_event(
            {"event_id": "e1", "task_id": "r", "kind": "completed"}
        )

        self.assertEqual(first.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(client.created[0]["assignee"], "ceo-agent")
        self.assertEqual(client.created[0]["parent_task_ids"], ("r", "risk"))
        self.assertIn("workflow_role=synthesis", client.created[0]["body"])
        self.assertNotIn(
            "qa-department",
            [item["assignee"] for item in client.created],
        )
        self.assertEqual(
            sum(item["assignee"] == "ceo-agent" for item in client.created),
            1,
        )

    def test_response_tasks_are_created_before_terminal_observers(self) -> None:
        timeline = []

        class OrderingClient(FakeClient):
            def create_task(self, **kwargs):
                timeline.append(f"create:{kwargs['assignee']}")
                return super().create_task(**kwargs)

        client = OrderingClient()
        service = CeoSupervisorService(client)
        service._project_terminal_task = lambda **kwargs: timeline.append(
            "terminal-observer"
        )
        service._deliver_department_progress = lambda **kwargs: None
        service._reconcile_department_terminal_progress = lambda **kwargs: None

        decision = service.handle_terminal_event(
            {
                "event_id": "response-before-observer",
                "task_id": "r",
                "kind": "completed",
            }
        )

        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(timeline, ["create:ceo-agent", "terminal-observer"])

    def test_terminal_observer_runs_after_root_lock_is_released(self) -> None:
        client = FakeClient()
        service = CeoSupervisorService(client)
        acquired_from_peer: list[bool] = []

        def observe(**_kwargs) -> None:
            root_lock = service._parent_lock("root")

            def acquire_from_peer() -> None:
                acquired = root_lock.acquire(timeout=0.2)
                acquired_from_peer.append(acquired)
                if acquired:
                    root_lock.release()

            peer = threading.Thread(target=acquire_from_peer)
            peer.start()
            peer.join(timeout=1)

        service._project_terminal_task = observe
        service._deliver_department_progress = lambda **kwargs: None
        service._reconcile_department_terminal_progress = lambda **kwargs: None
        service.handle_terminal_event(
            {
                "event_id": "observer-after-root-lock",
                "task_id": "r",
                "kind": "completed",
            }
        )

        self.assertEqual(acquired_from_peer, [True])

    def test_synthesis_does_not_wait_for_qa_visibility_after_qa_create(self) -> None:
        class StaleWorkflowClient(FakeClient):
            def create_task(self, **kwargs):
                self.created.append(kwargs)
                self.comments.append(
                    {"task_id": "root", "body": "created supervisor task"}
                )
                return f"new-{len(self.created)}"

        client = StaleWorkflowClient()
        decision = CeoSupervisorService(client).handle_terminal_event(
            {
                "event_id": "qa-create-visible-late",
                "task_id": "r",
                "kind": "completed",
            }
        )

        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(
            [item["assignee"] for item in client.created],
            ["ceo-agent"],
        )
        self.assertNotIn("qa", client.created[0]["parent_task_ids"])

    def test_supervisor_restores_selected_set_from_root_comment(self) -> None:
        client = FakeClient()
        client.payloads.append(
            {
                "id": "accounting",
                "assignee": "accounting-portfolio-department",
                "status": "done",
                "summary": "accounting",
                "body": "workflow_root_task_id=root\nworkflow_role=primary",
            }
        )
        client.comments = [
            {
                "task_id": "root",
                "body": (
                    "selected_primary_profiles=research-department,"
                    "risk-management,accounting-portfolio-department"
                ),
            }
        ]

        decision = CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "selected-comment", "task_id": "r", "kind": "completed"}
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(
            client.created[0]["parent_task_ids"],
            ("r", "risk", "accounting"),
        )

    def test_qa_create_refresh_prevents_duplicate_synthesis(self) -> None:
        """A sibling event may create synthesis while QA creation is in flight."""

        class ConcurrentSynthesisClient(FakeClient):
            def create_task(self, **kwargs):
                self.created.append(kwargs)
                if kwargs["assignee"] == "qa-department":
                    self.payloads.append(
                        {
                            "id": "qa-created",
                            "assignee": "qa-department",
                            "status": "ready",
                            "body": build_scoped_task_body("QA", "root", role="qa"),
                        }
                    )
                    # Model a concurrent sibling event that already created the
                    # response-plane task. The current handler must observe it
                    # on its post-create workflow refresh.
                    self.payloads.append(
                        {
                            "id": "synthesis-existing",
                            "assignee": "ceo-agent",
                            "status": "ready",
                            "body": build_scoped_task_body(
                                "hgfinance.ceo-supervisor.v1 action=SYNTHESIZE\n"
                                "CEO synthesis",
                                "root",
                                role="synthesis",
                            ),
                        }
                    )
                return {"id": f"created-{len(self.created)}"}

        client = ConcurrentSynthesisClient()
        decision = CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "qa-refresh", "task_id": "r", "kind": "completed"}
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(
            [item["assignee"] for item in client.created],
            ["ceo-agent"],
        )

    def test_indexed_synthesis_duplicate_check_does_not_list_board(self) -> None:
        class IndexedClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.list_calls = 0

            def authoritative_synthesis_exists(self, root_id: str) -> bool:
                self.asserted_root = root_id
                return True

            def list_tasks(self):
                self.list_calls += 1
                raise AssertionError("indexed terminal check must not list board")

        client = IndexedClient()
        decision = CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "indexed-duplicate-check", "task_id": "r", "kind": "completed"}
        )

        self.assertIsNotNone(decision)
        self.assertEqual(client.created[0]["assignee"], "ceo-agent")
        self.assertEqual(client.list_calls, 0)

    def test_binding_synthesis_is_parented_by_primary_not_qa(self) -> None:
        client = FakeClient()
        client.root_body = build_root_body(
            "Samsung order request", "req-binding", workflow_mode="binding"
        )
        client.payloads.append(
            {
                "id": "qa",
                "assignee": "qa-department",
                "status": "done",
                "summary": "qa passed",
                "body": "workflow_root_task_id=root\nworkflow_role=qa",
            }
        )

        decision = CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "binding-qa-done", "task_id": "qa", "kind": "completed"}
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(client.created[0]["parent_task_ids"], ("r", "risk"))
        self.assertIn("workflow_mode=binding", client.created[0]["body"])

    def test_user_paper_order_skips_strategy_qa_even_if_event_requests_it(self) -> None:
        client = FakeClient()
        client.root_body = build_root_body(
            "삼성전자 10주 시장가 매수",
            "req-paper-order",
            workflow_mode="binding",
            user_paper_order_scope=UserPaperOrderScope(
                order_request_id="order-request-1",
                raw_instruction_sha256="a" * 64,
                fund_id="fund-a",
                book_id="book-a",
            ),
        )
        client.payloads = [
            {
                "id": "trading",
                "assignee": "trading-department",
                "status": "done",
                "summary": "PAPER order rejected",
                "result": (
                    '{"order_submitted": false, "user_message": "market closed"}'
                ),
                "final_answer": "market closed",
                "body": "workflow_root_task_id=root\nworkflow_role=primary",
            }
        ]
        client.comments = [
            {
                "task_id": "root",
                "body": "selected_primary_profiles=trading-department",
            }
        ]

        decision = CeoSupervisorService(client).handle_terminal_event(
            {
                "event_id": "paper-order-done",
                "task_id": "trading",
                "kind": "completed",
                "qa_required": True,
            }
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual([item["assignee"] for item in client.created], ["ceo-agent"])
        synthesis_body = client.created[0]["body"]
        self.assertIn("synthesis_mode=structured_primary_template", synthesis_body)
        self.assertEqual(client.created[0]["initial_status"], "blocked")
        self.assertEqual(len(client.completed), 1)
        self.assertEqual(client.completed[0]["result"], "market closed")
        metadata = client.completed[0]["metadata"]
        self.assertIsInstance(metadata, dict)
        self.assertTrue(metadata["preserved_primary_final_answer_verbatim"])

    def test_binding_template_failure_releases_same_synthesis_for_llm_fallback(
        self,
    ) -> None:
        class CompletionFailureClient(FakeClient):
            def complete_task(self, task_id: str, **kwargs: object) -> None:
                del task_id, kwargs
                raise RuntimeError("probe failure")

        client = CompletionFailureClient()
        client.root_body = build_root_body(
            "삼성전자 조건주문",
            "req-paper-template-fallback",
            workflow_mode="binding",
            user_paper_order_scope=UserPaperOrderScope(
                order_request_id="order-request-fallback",
                raw_instruction_sha256="b" * 64,
                fund_id="fund-a",
                book_id="book-a",
            ),
        )
        client.payloads = [
            {
                "id": "trading",
                "assignee": "trading-department",
                "status": "done",
                "result": "활성화 완료",
                "final_answer": "조건주문이 활성화되었습니다.",
                "body": "workflow_root_task_id=root\nworkflow_role=primary",
            }
        ]
        client.comments = [
            {
                "task_id": "root",
                "body": "selected_primary_profiles=trading-department",
            }
        ]

        decision = CeoSupervisorService(client).handle_terminal_event(
            {
                "event_id": "paper-template-fallback",
                "task_id": "trading",
                "kind": "completed",
            }
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(client.unblocked, ["new-1"])
        self.assertEqual(len(client.created), 1)

    def test_root_body_declares_scope_only_planning_contract(self) -> None:
        body = build_root_body("Samsung", "req-1")
        self.assertIn("root_task_role=scope_and_planning", body)
        self.assertIn("primary_execution_parent=none", body)
        self.assertIn("planning_terminal_state=done_after_child_creation", body)
        self.assertNotIn("child_parent_required=current_root_task_id", body)
        self.assertIn("workflow_mode=analysis", body)
        self.assertIn(
            "analysis_response_rule=primary_results_ready_allows_immediate_ceo_synthesis",
            body,
        )
        self.assertNotIn("then CEO synthesis", body)
        self.assertIn("response_plane=primary_results_ready", body)
        self.assertIn("governance_plane=async_qa", body)
        self.assertIn("qa_is_not_synthesis_prerequisite=true", body)
        self.assertNotIn("QA then synthesis", body)

    def test_binding_mode_is_explicit_and_legacy_scoped_roots_remain_gated(
        self,
    ) -> None:
        self.assertEqual(infer_workflow_mode("삼성전자 분석"), "analysis")
        self.assertEqual(infer_workflow_mode("삼성전자 주문을 집행해"), "binding")
        self.assertEqual(infer_workflow_mode("삼성전자 매수해도 될까?"), "analysis")
        self.assertEqual(infer_workflow_mode("삼성전자 팔아도 안전해?"), "analysis")
        self.assertEqual(
            infer_workflow_mode(
                "애플을 지금 투자 관점에서 분석해줘. "
                "리스크 관리와 회계·포트폴리오 관점만 사용하고 "
                "Research와 Quant는 사용하지 마. "
                "주문이나 매매 실행은 하지 마."
            ),
            "analysis",
        )
        self.assertEqual(infer_workflow_mode("매매 실행은 하지 마"), "analysis")
        self.assertEqual(
            infer_workflow_mode("주문은 하지 말고 분석만 해줘"), "analysis"
        )
        self.assertEqual(
            infer_workflow_mode(
                "PAPER 읽기 전용 E2E 검증이다. 주문 제출·원장 변경·설정 변경은 "
                "절대 수행하지 말라."
            ),
            "analysis",
        )
        self.assertEqual(
            infer_workflow_mode("삼성전자 주문이나 집행은 하지 말고 분석만 해줘"),
            "analysis",
        )
        self.assertEqual(workflow_mode_from_body(build_root_body("q", "r")), "analysis")
        self.assertEqual(
            workflow_mode_from_body("hgfinance.ceo-workflow-scope.v1"), "binding"
        )

    def test_direct_non_binding_root_without_workflow_mode_is_analysis(self) -> None:
        body = (
            "hgfinance.ceo-workflow-scope.v1\n"
            "workflow_role=planning\n"
            "request_class=non-binding advisory analysis\n"
            "selected_primary_profiles=research-department"
        )
        self.assertEqual(workflow_mode_from_body(body), "analysis")

    def test_invalid_workflow_mode_aborts_only_current_workflow(self) -> None:
        client = FakeClient()
        client.root_body = (
            "hgfinance.ceo-workflow-scope.v1\nworkflow_mode=unsupported\n"
        )

        decision = CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "invalid-mode", "task_id": "r", "kind": "completed"}
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.BLOCK_ABORT)
        self.assertEqual(client.blocked, ["root"])
        self.assertTrue(
            any(
                "ceo-workflow-scope-error" in comment["body"]
                for comment in client.comments
            )
        )

    def test_legacy_scope_error_does_not_mutate_terminal_root_and_dedupes(self) -> None:
        client = FakeClient()
        client.root_body = (
            "hgfinance.ceo-workflow-scope.v1\nworkflow_mode=unsupported\n"
        )
        original_show = client.show

        def show(task_id: str):
            payload = original_show(task_id)
            if task_id == "root":
                payload["status"] = "done"
            return payload

        client.show = show
        event = {"event_id": "legacy-invalid-mode", "task_id": "r", "kind": "completed"}

        first = CeoSupervisorService(client).handle_terminal_event(event)
        second = CeoSupervisorService(client).handle_terminal_event(event)

        self.assertEqual(first.action, SupervisorAction.BLOCK_ABORT)
        self.assertIsNone(second)
        self.assertEqual(client.blocked, [])
        scope_comments = [
            item
            for item in client.comments
            if "ceo-workflow-scope-error" in item["body"]
        ]
        self.assertEqual(len(scope_comments), 1)

    def test_reclaimed_does_not_wake_supervisor(self) -> None:
        client = FakeClient()
        service = CeoSupervisorService(client)

        self.assertIsNone(
            service.handle_terminal_event(
                {"event_id": "reclaimed-1", "task_id": "r", "kind": "reclaimed"}
            )
        )
        self.assertEqual(client.created, [])
        self.assertEqual(client.comments, [])

    def test_planning_root_terminal_event_does_not_wake_supervisor(self) -> None:
        client = FakeClient()
        client.root_body = build_root_body("Samsung", "req-1")
        service = CeoSupervisorService(client)

        self.assertIsNone(
            service.handle_terminal_event(
                {"event_id": "root-done", "task_id": "root", "kind": "completed"}
            )
        )
        self.assertEqual(client.created, [])

    def test_one_terminal_child_does_not_synthesize_before_sibling(self) -> None:
        client = FakeClient()
        client.payloads[1]["status"] = "running"
        service = CeoSupervisorService(client)

        self.assertIsNone(
            service.handle_terminal_event(
                {"event_id": "r-only", "task_id": "r", "kind": "completed"}
            )
        )
        self.assertEqual(client.created, [])

    def test_concurrent_sibling_events_only_wake_once(self) -> None:
        client = FakeClient()
        service = CeoSupervisorService(client)
        events = [
            {"event_id": "r-concurrent", "task_id": "r", "kind": "completed"},
            {"event_id": "risk-concurrent", "task_id": "risk", "kind": "completed"},
        ]

        with ThreadPoolExecutor(max_workers=2) as executor:
            decisions = list(executor.map(service.handle_terminal_event, events))

        self.assertEqual(sum(decision is not None for decision in decisions), 1)
        self.assertEqual(len(client.created), 1)
        self.assertEqual(
            [item["assignee"] for item in client.created],
            ["ceo-agent"],
        )

    def test_duplicate_event_is_idempotent_across_service_restart(self) -> None:
        client = FakeClient()
        event = {"event_id": "duplicate-1", "task_id": "r", "kind": "completed"}

        first = CeoSupervisorService(client).handle_terminal_event(event)
        second = CeoSupervisorService(client).handle_terminal_event(event)

        self.assertEqual(first.action, SupervisorAction.SYNTHESIZE)
        self.assertIsNone(second)
        self.assertEqual(len(client.created), 1)
        self.assertEqual(
            [item["assignee"] for item in client.created],
            ["ceo-agent"],
        )
        self.assertEqual(
            sum(
                "event=duplicate-1" in comment["body"]
                and "state=done" in comment["body"]
                for comment in client.comments
            ),
            1,
        )

    def test_distinct_wakeups_for_same_terminal_transition_coalesce(self) -> None:
        client = FakeClient()
        service = CeoSupervisorService(client)
        projected = []
        service._project_terminal_task = lambda **kwargs: projected.append(
            kwargs["task_id"]
        )
        service._deliver_department_progress = lambda **kwargs: None
        service._reconcile_department_terminal_progress = lambda **kwargs: None
        events = (
            {"event_id": "watch-r", "task_id": "r", "kind": "completed"},
            {"event_id": "recovery-r", "task_id": "r", "kind": "done"},
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            decisions = list(executor.map(service.handle_terminal_event, events))

        self.assertEqual(sum(decision is not None for decision in decisions), 1)
        self.assertEqual(projected, ["r"])
        self.assertEqual(
            sum(item["assignee"] == "ceo-agent" for item in client.created),
            1,
        )

    def test_cold_terminal_event_uses_root_lookup_then_one_fresh_workflow(self) -> None:
        class RootLookupClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.root_lookup_calls = 0
                self.workflow_calls = 0

            def workflow_root(self, task_id: str) -> str:
                self.root_lookup_calls += 1
                return "root"

            def workflow(self, task_id: str):
                self.workflow_calls += 1
                return super().workflow(task_id)

            def list_tasks(self):
                return tuple(self.payloads)

        client = RootLookupClient()
        decision = CeoSupervisorService(client).handle_terminal_event(
            {
                "event_id": "cold-terminal-r",
                "task_id": "r",
                "kind": "completed",
            }
        )

        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(client.root_lookup_calls, 1)
        self.assertEqual(
            client.workflow_calls,
            1,
            "only the lock-protected workflow read may hydrate siblings",
        )
        self.assertEqual(
            [item["assignee"] for item in client.created],
            ["ceo-agent"],
            "the optimization must not change synthesis fan-out",
        )

    def test_restart_preserves_wakeup_guard(self) -> None:
        client = FakeClient()
        client.comments = [
            {
                "task_id": "root",
                "body": f"hgfinance.ceo-supervisor.wakeup.v1 event=old-{i} state=done action=NONE",
            }
            for i in range(8)
        ]

        decision = CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "ninth", "task_id": "r", "kind": "completed"}
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(client.blocked, [])

    def test_retry_budget_still_blocks_after_restart(self) -> None:
        client = FakeClient()
        client.payloads[0].update(status="failed")
        client.comments = [
            {
                "task_id": "root",
                "body": (
                    "hgfinance.ceo-supervisor.wakeup.v1 "
                    f"event=old-{i} state=done action=RETRY_TASK "
                    "budget_consumed=true"
                ),
            }
            for i in range(8)
        ]

        decision = CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "ninth-retry", "task_id": "r", "kind": "crashed"}
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, SupervisorAction.BLOCK_ABORT)
        self.assertEqual(client.blocked, ["root"])

    def test_restart_preserves_retry_guard_from_hermes_runs(self) -> None:
        client = FakeClient()
        client.payloads[0].update(
            status="failed",
            runs=[{"outcome": "failed"}, {"outcome": "failed"}],
        )

        decision = CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "failed-after-restart", "task_id": "r", "kind": "crashed"}
        )

        self.assertEqual(decision.action, SupervisorAction.BLOCK_ABORT)
        self.assertEqual(client.unblocked, [])
        self.assertEqual(client.blocked, ["root"])

    def test_invalid_persisted_assignee_aborts_only_workflow(self) -> None:
        client = FakeClient()
        client.payloads[1]["assignee"] = "risk-department"

        decision = CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "bad-assignee", "task_id": "r", "kind": "completed"}
        )

        self.assertEqual(decision.action, SupervisorAction.BLOCK_ABORT)
        self.assertEqual(client.blocked, ["root"])

    def test_hermes_show_json_task_projection_is_flattened(self) -> None:
        import json
        import subprocess

        completed = subprocess.CompletedProcess(
            args=["hermes"],
            returncode=0,
            stdout=json.dumps(
                {
                    "task": {
                        "id": "r",
                        "assignee": "research-department",
                        "status": "done",
                        "body": "research",
                    },
                    "parents": ["root"],
                    "children": [],
                    "runs": [],
                    "latest_summary": "research summary",
                }
            ),
            stderr="",
        )

        def runner(*args, **kwargs):
            return completed

        task = HermesKanbanClient(runner=runner).show("r")
        self.assertEqual(task["id"], "r")
        self.assertEqual(task["assignee"], "research-department")
        self.assertEqual(task["parents"], ["root"])
        self.assertEqual(task["latest_summary"], "research summary")

    def test_invalid_hermes_json_is_a_command_error(self) -> None:
        import subprocess

        completed = subprocess.CompletedProcess(
            args=["hermes"], returncode=0, stdout="not-json", stderr=""
        )

        def runner(*args, **kwargs):
            return completed

        with self.assertRaises(HermesKanbanCommandError):
            HermesKanbanClient(runner=runner).show("r")

    def test_scope_marker_discovers_parentless_primary_tasks(self) -> None:
        import json
        import subprocess

        root = "t_aaaaaaaa"
        research = "t_bbbbbbbb"
        risk = "t_cccccccc"
        qa = "t_dddddddd"
        synthesis = "t_eeeeeeee"
        old_research = "t_ffffffff"
        payloads = {
            root: {
                "id": root,
                "assignee": "ceo-agent",
                "status": "done",
                "body": build_root_body("Samsung", "req-1"),
                "parents": [],
                "children": [],
            },
            research: {
                "id": research,
                "assignee": "research-department",
                "status": "done",
                "body": build_scoped_task_body("research", root, role="primary"),
                "parents": [],
                "children": [],
            },
            risk: {
                "id": risk,
                "assignee": "risk-management",
                "status": "done",
                "body": build_scoped_task_body("risk", root, role="primary"),
                "parents": [],
                "children": [],
            },
            qa: {
                "id": qa,
                "assignee": "qa-department",
                "status": "ready",
                "body": build_scoped_task_body("qa", root, role="qa"),
                "parents": [research, risk],
                "children": [],
            },
            synthesis: {
                "id": synthesis,
                "assignee": "ceo-agent",
                "status": "todo",
                "body": build_scoped_task_body("synthesis", root, role="synthesis"),
                "parents": [qa],
                "children": [],
            },
            old_research: {
                "id": old_research,
                "assignee": "research-department",
                "status": "done",
                "body": build_scoped_task_body(
                    "old workflow", "t_11111111", role="primary"
                ),
                "parents": [],
                "children": [],
            },
        }

        def runner(args, **kwargs):
            command = list(args)
            if command[1:3] == ["kanban", "list"]:
                stdout = json.dumps(list(payloads.values()))
            else:
                task_id = command[3]
                stdout = json.dumps({"task": payloads[task_id]})
            return subprocess.CompletedProcess(args, 0, stdout, "")

        client = HermesKanbanClient(runner=runner)
        discovered_root, children = client.workflow(research)
        self.assertEqual(discovered_root, root)
        self.assertEqual(
            {task["id"] for task in children}, {research, risk, qa, synthesis}
        )
        self.assertNotIn(old_research, {task["id"] for task in children})
        self.assertEqual(payloads[research]["parents"], [])
        self.assertEqual(payloads[risk]["parents"], [])
        self.assertEqual(payloads[qa]["parents"], [research, risk])
        self.assertEqual(payloads[synthesis]["parents"], [qa])

    def test_scoped_workflow_root_lookup_does_not_scan_board_or_siblings(self) -> None:
        import json
        import subprocess

        root = "t_aaaaaaaa"
        primary = "t_bbbbbbbb"
        calls: list[tuple[str, ...]] = []
        payload = {
            "id": primary,
            "assignee": "research-department",
            "status": "done",
            "body": build_scoped_task_body("research", root, role="primary"),
            "parents": [],
        }

        def runner(args, **kwargs):
            command = tuple(args)
            calls.append(command)
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps({"task": payload}),
                "",
            )

        discovered_root = HermesKanbanClient(runner=runner).workflow_root(primary)

        self.assertEqual(discovered_root, root)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:3], ("kanban", "show"))

    def test_known_root_snapshot_lists_once_and_hydrates_each_task_once(self) -> None:
        import json
        import subprocess

        root = "t_aaaaaaaa"
        primary = "t_bbbbbbbb"
        sibling = "t_cccccccc"
        payloads = {
            root: {
                "id": root,
                "assignee": "ceo-agent",
                "status": "done",
                "body": build_root_body("Samsung", "req-1"),
            },
            primary: {
                "id": primary,
                "assignee": "research-department",
                "status": "done",
                "body": build_scoped_task_body("research", root, role="primary"),
            },
            sibling: {
                "id": sibling,
                "assignee": "risk-management",
                "status": "done",
                "body": build_scoped_task_body("risk", root, role="primary"),
            },
        }
        calls: list[tuple[str, ...]] = []

        def runner(args, **kwargs):
            command = tuple(args)
            calls.append(command)
            if command[1:3] == ("kanban", "list"):
                stdout = json.dumps(list(payloads.values()))
            else:
                stdout = json.dumps(
                    {
                        "task": payloads[command[3]],
                        "latest_summary": f"summary:{command[3]}",
                        "runs": [],
                    }
                )
            return subprocess.CompletedProcess(args, 0, stdout, "")

        discovered_root, children, root_payload = HermesKanbanClient(
            runner=runner
        ).authoritative_workflow_snapshot(root, primary)

        self.assertEqual(discovered_root, root)
        self.assertEqual(root_payload["id"], root)
        self.assertEqual({child["id"] for child in children}, {primary, sibling})
        self.assertTrue(all("latest_summary" in child for child in children))
        self.assertEqual(
            sum(command[1:3] == ("kanban", "list") for command in calls),
            1,
        )
        shown = [command[3] for command in calls if command[1:3] == ("kanban", "show")]
        self.assertCountEqual(shown, [root, primary, sibling])

    def test_primary_scope_task_can_depend_on_scope_root(self) -> None:
        root = "t_aaaaaaaa"
        primary = build_scoped_task_body("research", root, role="primary")
        validate_workflow_scope(
            root_task_id=root,
            root_payload={"id": root, "body": build_root_body("q", "req")},
            descendants=[
                {
                    "id": "t_bbbbbbbb",
                    "assignee": "research-department",
                    "body": primary,
                    "parents": [root],
                }
            ],
        )

    def test_root_delegation_comment_does_not_become_second_scope_root(self) -> None:
        root = "t_aaaaaaaa"
        validate_workflow_scope(
            root_task_id=root,
            root_payload={
                "id": root,
                "body": build_root_body("q", "req"),
                "comments": [
                    {
                        "body": (
                            "workflow_root_task_id=t_aaaaaaaa "
                            "workflow_role=primary child=t_bbbbbbbb"
                        )
                    }
                ],
            },
            descendants=[
                {
                    "id": "t_bbbbbbbb",
                    "body": build_scoped_task_body("research", root, role="primary"),
                    "parents": [],
                }
            ],
        )


class ReadyPrimaryPlanRecoveryTest(unittest.TestCase):
    """Regression coverage for direct-CEO planning/root completion races."""

    selected = (
        "research-department",
        "quant-backtest-department",
        "risk-management",
    )

    @staticmethod
    def _root_body() -> str:
        return (
            "hgfinance.ceo-workflow-scope.v1\n"
            "origin=user-query\n"
            "workflow_mode=analysis\n"
            "root_task_role=scope_and_planning\n"
            "planning_terminal_state=done_after_child_creation\n"
        )

    @staticmethod
    def _ceo_plan_comment() -> str:
        return (
            "Delegation plan (request-scoped, non-binding analysis):\n"
            "selected_primary_profiles="
            "research-department,quant-backtest-department,risk-management\n"
            "analysis_mode=fast_advisory\n"
            "workflow_mode=analysis\n"
            "producer=ceo-hermes-direct\n"
            "qa_required=false\n"
            "delegation_instruction.research-department="
            "Assess Apple fundamentals and valuation.\n"
            "delegation_instruction.quant-backtest-department="
            "Assess Apple trend, volatility, valuation, and quantitative risk.\n"
            "delegation_instruction.risk-management="
            "Assess Apple downside, concentration, liquidity, and policy risks.\n"
        )

    def test_recently_completed_old_root_materializes_from_ceo_comment(self) -> None:
        """A long-running planner remains recoverable immediately after completion."""

        import time

        now = int(time.time())

        class Client(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.root_body = ReadyPrimaryPlanRecoveryTest._root_body()
                self.payloads = []
                self.ceo_comment = {
                    "id": 1002,
                    "task_id": "root",
                    "author": "ceo-agent",
                    "body": ReadyPrimaryPlanRecoveryTest._ceo_plan_comment(),
                    "created_at": now - 15,
                }

            def list_tasks(self):
                return (
                    {
                        "id": "root",
                        "status": "done",
                        # Production incident shape:
                        # root is older than the recovery TTL...
                        "created_at": now - 300,
                        # ...but planning completed only moments ago.
                        "completed_at": now - 10,
                        "body": self.root_body,
                    },
                )

            def show(self, task_id: str):
                if task_id == "root":
                    return {
                        "id": "root",
                        "status": "done",
                        "created_at": now - 300,
                        "completed_at": now - 10,
                        "body": self.root_body,
                        "comments": [self.ceo_comment, *self.comments],
                    }

                return super().show(task_id)

        client = Client()
        service = CeoSupervisorService(client)

        first = service.materialize_ready_primary_plans()

        self.assertEqual(
            tuple(decision.assignee for decision in first),
            self.selected,
        )
        self.assertEqual(
            tuple(item["assignee"] for item in client.created),
            self.selected,
        )
        self.assertTrue(
            all(item["parent_task_ids"] == ("root",) for item in client.created)
        )
        self.assertTrue(
            all("workflow_root_task_id=root" in item["body"] for item in client.created)
        )

        # Re-polling the same durable plan must be idempotent.
        second = service.materialize_ready_primary_plans()

        self.assertEqual(second, ())
        self.assertEqual(len(client.created), 3)

    def test_stale_completed_root_is_not_recovered(self) -> None:
        """The recovery TTL is measured from completion, not root creation."""

        import time

        now = int(time.time())

        class Client(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.root_body = ReadyPrimaryPlanRecoveryTest._root_body()
                self.payloads = []
                self.ceo_comment = {
                    "id": 1002,
                    "task_id": "root",
                    "author": "ceo-agent",
                    "body": ReadyPrimaryPlanRecoveryTest._ceo_plan_comment(),
                    "created_at": now - 310,
                }

            def list_tasks(self):
                return (
                    {
                        "id": "root",
                        "status": "done",
                        # Even a recently-created root must be excluded when
                        # its completion itself is stale.
                        "created_at": now - 10,
                        "completed_at": now - 300,
                        "body": self.root_body,
                    },
                )

            def show(self, task_id: str):
                if task_id == "root":
                    return {
                        "id": "root",
                        "status": "done",
                        "created_at": now - 10,
                        "completed_at": now - 300,
                        "body": self.root_body,
                        "comments": [self.ceo_comment, *self.comments],
                    }

                return super().show(task_id)

        client = Client()

        decisions = CeoSupervisorService(client).materialize_ready_primary_plans()

        self.assertEqual(decisions, ())
        self.assertEqual(client.created, [])

    def test_recovery_does_not_materialize_invalid_qa_primary(self) -> None:
        """Replay/recovery must apply the same primary-role guard."""

        import time

        now = int(time.time())

        class Client(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.root_body = ReadyPrimaryPlanRecoveryTest._root_body()
                self.ceo_comment = {
                    "id": 1003,
                    "task_id": "root",
                    "author": "ceo-agent",
                    "body": (
                        "selected_primary_profiles=qa-department\n"
                        "analysis_mode=fast_advisory\n"
                        "workflow_mode=analysis\n"
                        "producer=ceo-hermes-direct\n"
                        "qa_required=false\n"
                        "delegation_instruction.qa-department=Audit the analysis.\n"
                    ),
                    "created_at": now - 5,
                }

            def list_tasks(self):
                return (
                    {
                        "id": "root",
                        "status": "done",
                        "created_at": now - 20,
                        "completed_at": now - 5,
                        "body": self.root_body,
                    },
                )

            def show(self, task_id: str):
                if task_id == "root":
                    return {
                        "id": "root",
                        "status": "done",
                        "created_at": now - 20,
                        "completed_at": now - 5,
                        "body": self.root_body,
                        "metadata": {
                            "selected_primary_profiles": "qa-department",
                        },
                        "comments": [self.ceo_comment],
                    }
                return super().show(task_id)

        client = Client()
        service = CeoSupervisorService(client)

        with self.assertLogs(
            "orchestration.adapters.ceo_supervisor", level="WARNING"
        ) as captured:
            first = service.materialize_ready_primary_plans()
            # A second recovery pass must not revive or re-log the invalid plan.
            second = service.materialize_ready_primary_plans()

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(len(client.created), 1)
        self.assertEqual(client.created[0]["assignee"], "ceo-agent")
        self.assertIn("workflow_role=synthesis", client.created[0]["body"])
        self.assertEqual(second, ())
        self.assertEqual(len(client.created), 1)
        self.assertEqual(
            sum("invalid-primary-selection" in line for line in captured.output),
            1,
        )

    def test_handled_empty_primary_is_excluded_before_recovery_materialization(
        self,
    ) -> None:
        """A durable clarification removes the root from later recovery polls."""

        import time

        now = int(time.time())

        class Client(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.root_body = ReadyPrimaryPlanRecoveryTest._root_body()
                self.payloads = []
                self.ceo_comment = {
                    "id": 1004,
                    "task_id": "root",
                    "author": "ceo-agent",
                    "body": (
                        "selected_primary_profiles=qa-department\n"
                        "analysis_mode=fast_advisory\n"
                        "workflow_mode=analysis\n"
                        "producer=ceo-hermes-direct\n"
                        "qa_required=false\n"
                        "delegation_instruction.qa-department=Audit the analysis.\n"
                    ),
                    "created_at": now - 5,
                }
                self.show_calls = 0

            def list_tasks(self):
                rows = [
                    {
                        "id": "root",
                        "status": "done",
                        "created_at": now - 20,
                        "completed_at": now - 5,
                        "body": self.root_body,
                    }
                ]
                for index, payload in enumerate(self.payloads):
                    rows.append(
                        {
                            "id": payload["id"],
                            "status": payload["status"],
                            "created_at": now - 4 + index,
                            "body": payload["body"],
                            "idempotency_key": self.created[index]["idempotency_key"],
                        }
                    )
                return tuple(rows)

            def show(self, task_id: str):
                self.show_calls += 1
                if task_id == "root":
                    return {
                        "id": "root",
                        "status": "done",
                        "created_at": now - 20,
                        "completed_at": now - 5,
                        "body": self.root_body,
                        "metadata": {
                            "selected_primary_profiles": "qa-department",
                        },
                        "comments": [self.ceo_comment, *self.comments],
                    }
                return super().show(task_id)

        client = Client()
        service = CeoSupervisorService(client)

        with self.assertLogs(
            "orchestration.adapters.ceo_supervisor", level="WARNING"
        ) as captured:
            first = service.materialize_ready_primary_plans()
            second = service.materialize_ready_primary_plans()

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(second, ())
        self.assertEqual(len(client.created), 1)
        self.assertEqual(client.show_calls, 1)
        self.assertEqual(
            sum("invalid-primary-selection" in line for line in captured.output),
            1,
        )


class InitialPrimaryMaterializationTest(unittest.TestCase):
    """CEO one-pass delegation plan -> deterministic primary materialization."""

    selected = (
        "research-department",
        "quant-backtest-department",
        "risk-management",
    )

    @staticmethod
    def root_body() -> str:
        return (
            "origin=user-query\n"
            "workflow_role=root\n"
            "workflow_mode=analysis\n"
            "analysis_mode=fast_advisory\n"
            "selected_primary_profiles="
            "research-department,quant-backtest-department,risk-management\n"
            "delegation_instruction.research-department="
            "Assess AMZN fundamentals and valuation.\n"
            "delegation_instruction.quant-backtest-department="
            "Assess AMZN trend, valuation, and quantitative risk.\n"
            "delegation_instruction.risk-management="
            "Assess AMZN downside and company-specific risks.\n"
        )

    def test_materializes_all_three_missing_selected_primaries(self) -> None:
        state = SupervisorState(
            parent_task_id="root",
            children=(),
            workflow_mode="analysis",
            selected_primary_profiles=self.selected,
            root_is_user_query=True,
        )

        decisions = _initial_primary_materialization_decisions(
            state,
            self.root_body(),
        )

        self.assertEqual(
            tuple(decision.assignee for decision in decisions),
            self.selected,
        )
        self.assertTrue(
            all(decision.parent_task_ids == ("root",) for decision in decisions)
        )
        self.assertTrue(
            all(
                "analysis_mode=fast_advisory" in decision.body for decision in decisions
            )
        )
        self.assertTrue(
            all(
                "Never search unrelated Kanban tasks" in decision.body
                and "kanban_block with needs_input" in decision.body
                for decision in decisions
            )
        )
        quant_body = next(
            decision.body
            for decision in decisions
            if decision.assignee == "quant-backtest-department"
        )
        self.assertIn("retrieval_attempt", quant_body)
        self.assertIn("Do not call delegate_task", quant_body)

    def test_materializes_hr_for_workforce_improvement_analysis(self) -> None:
        body = (
            "origin=user-query\n"
            "workflow_role=root\n"
            "workflow_mode=analysis\n"
            "analysis_mode=fast_advisory\n"
            "selected_primary_profiles=hr-department\n"
            "delegation_instruction.hr-department="
            "Compare department improvement signals.\n"
        )
        state = SupervisorState(
            parent_task_id="root",
            children=(),
            workflow_mode="analysis",
            selected_primary_profiles=("hr-department",),
            root_is_user_query=True,
        )

        decisions = _initial_primary_materialization_decisions(state, body)

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, SupervisorAction.CREATE_TASK)
        self.assertEqual(decisions[0].assignee, "hr-department")

    def test_quant_standard_analysis_is_single_owner_and_root_linked(self) -> None:
        body = (
            "origin=user-query\n"
            "workflow_role=root\n"
            "workflow_mode=analysis\n"
            "analysis_mode=standard_analysis\n"
            "selected_primary_profiles=quant-backtest-department\n"
            "delegation_instruction.quant-backtest-department="
            "Assess AMZN trend and quantitative risk.\n"
        )
        state = SupervisorState(
            parent_task_id="root",
            children=(),
            workflow_mode="analysis",
            selected_primary_profiles=("quant-backtest-department",),
            root_is_user_query=True,
        )

        decisions = _initial_primary_materialization_decisions(state, body)

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].parent_task_ids, ("root",))
        self.assertIn("Do not call delegate_task", decisions[0].body)
        self.assertIn("retrieval_attempt", decisions[0].body)

    def test_standard_risk_primary_receives_terminal_result_contract(self) -> None:
        body = (
            "origin=user-query\n"
            "workflow_role=root\n"
            "workflow_mode=analysis\n"
            "analysis_mode=standard_analysis\n"
            "selected_primary_profiles=risk-management\n"
            "delegation_instruction.risk-management="
            "Assess the requested downside and policy risks.\n"
        )
        state = SupervisorState(
            parent_task_id="root",
            children=(),
            workflow_mode="analysis",
            selected_primary_profiles=("risk-management",),
            root_is_user_query=True,
        )

        decisions = _initial_primary_materialization_decisions(state, body)

        assert len(decisions) == 1
        assert "Terminal result persistence contract" in decisions[0].body
        assert "analysis_mode=fast_advisory" not in decisions[0].body

    def test_internal_workflow_failure_is_not_reported_as_provider_failure(
        self,
    ) -> None:
        child = ChildTaskState(
            task_id="control",
            profile="ceo-agent",
            status="blocked",
            block_reason=(
                "초기 실행 primary가 생성되지 않아 supervisor만 보완할 수 있습니다."
            ),
        )

        content = CeoSupervisorService._terminal_failure_content(child)

        self.assertIn("내부 작업이 중단", content)
        self.assertNotIn("provider", content)

    def test_qa_department_is_not_materialized_as_analysis_primary(self) -> None:
        body = (
            "origin=user-query\n"
            "workflow_role=root\n"
            "workflow_mode=analysis\n"
            "analysis_mode=fast_advisory\n"
            "selected_primary_profiles=qa-department\n"
            "delegation_instruction.qa-department=Audit the requested analysis.\n"
        )
        state = SupervisorState(
            parent_task_id="root",
            children=(),
            workflow_mode="analysis",
            selected_primary_profiles=("qa-department",),
            root_is_user_query=True,
        )

        with self.assertLogs(
            "orchestration.adapters.ceo_supervisor", level="WARNING"
        ) as captured:
            decisions = _initial_primary_materialization_decisions(state, body)

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(decisions[0].assignee, "ceo-agent")
        self.assertIn("action=SYNTHESIZE", decisions[0].body)
        self.assertIn(
            "synthesis_mode=deterministic_empty_primary_defer",
            decisions[0].body,
        )
        self.assertTrue(
            any(
                "invalid-primary-selection" in line
                and "profile=qa-department" in line
                and "ROLE_NOT_PRIMARY_ELIGIBLE" in line
                for line in captured.output
            )
        )

    def test_qa_only_without_delegation_plan_defers_without_control_card(self) -> None:
        body = (
            "origin=user-query\n"
            "workflow_role=root\n"
            "workflow_mode=analysis\n"
            "analysis_mode=fast_advisory\n"
            "selected_primary_profiles=qa-department\n"
        )
        state = SupervisorState(
            parent_task_id="root",
            children=(),
            workflow_mode="analysis",
            selected_primary_profiles=("qa-department",),
            root_is_user_query=True,
        )

        with self.assertLogs("orchestration.adapters.ceo_supervisor", level="WARNING"):
            decisions = _initial_primary_materialization_decisions(state, body)

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, SupervisorAction.SYNTHESIZE)
        self.assertEqual(decisions[0].assignee, "ceo-agent")
        self.assertIn("action=SYNTHESIZE", decisions[0].body)

    def test_mixed_selection_skips_qa_but_keeps_valid_primaries(self) -> None:
        body = (
            "origin=user-query\n"
            "workflow_role=root\n"
            "workflow_mode=analysis\n"
            "analysis_mode=fast_advisory\n"
            "selected_primary_profiles=research-department,qa-department,risk-management\n"
            "delegation_instruction.research-department=Assess the research evidence.\n"
            "delegation_instruction.qa-department=Audit the requested analysis.\n"
            "delegation_instruction.risk-management=Assess the downside risks.\n"
        )
        state = SupervisorState(
            parent_task_id="root",
            children=(),
            workflow_mode="analysis",
            selected_primary_profiles=(
                "research-department",
                "qa-department",
                "risk-management",
            ),
            root_is_user_query=True,
        )

        decisions = _initial_primary_materialization_decisions(state, body)

        self.assertEqual(
            tuple(decision.assignee for decision in decisions),
            ("research-department", "risk-management"),
        )

    def test_existing_primary_materializes_only_missing_profiles(self) -> None:
        research = child(
            "research",
            "research-department",
            "running",
            body=(
                "workflow_root_task_id=root\n"
                "workflow_role=primary\n"
                "workflow_mode=analysis"
            ),
        )
        state = SupervisorState(
            parent_task_id="root",
            children=(research,),
            workflow_mode="analysis",
            selected_primary_profiles=self.selected,
            root_is_user_query=True,
        )

        decisions = _initial_primary_materialization_decisions(
            state,
            self.root_body(),
        )

        self.assertEqual(
            tuple(decision.assignee for decision in decisions),
            ("quant-backtest-department", "risk-management"),
        )

    def test_incomplete_delegation_plan_fails_closed(self) -> None:
        body = self.root_body().replace(
            "delegation_instruction.risk-management="
            "Assess AMZN downside and company-specific risks.\n",
            "",
        )
        state = SupervisorState(
            parent_task_id="root",
            children=(),
            workflow_mode="analysis",
            selected_primary_profiles=self.selected,
            root_is_user_query=True,
        )

        self.assertEqual(
            _initial_primary_materialization_decisions(state, body),
            (),
        )

    def test_split_ceo_plan_and_mode_correction_are_recovered_once(self) -> None:
        root_body = "origin=user-query\nworkflow_role=root\nworkflow_mode=analysis\n"
        payload = {
            "body": root_body,
            "comments": [
                {
                    "author": "ceo-agent",
                    "body": (
                        "selected_primary_profiles=research-department\n"
                        "delegation_instruction.research-department="
                        "analysis_mode=standard_analysis; inspect the read-only evidence.\n"
                    ),
                },
                {
                    "author": "ceo-agent",
                    "body": "analysis_mode=fast_advisory\n",
                },
            ],
        }

        materialization_body = _materialization_plan_body(payload)
        self.assertEqual(
            _analysis_execution_mode_from_root_body(materialization_body),
            "fast_advisory",
        )
        decisions = _initial_primary_materialization_decisions(
            SupervisorState(
                parent_task_id="root",
                children=(),
                workflow_mode="analysis",
                selected_primary_profiles=("research-department",),
                root_is_user_query=True,
            ),
            materialization_body,
        )
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].assignee, "research-department")

    def test_binding_workflow_never_uses_fast_materializer(self) -> None:
        state = SupervisorState(
            parent_task_id="root",
            children=(),
            workflow_mode="binding",
            selected_primary_profiles=self.selected,
            root_is_user_query=True,
        )

        self.assertEqual(
            _initial_primary_materialization_decisions(
                state,
                self.root_body(),
            ),
            (),
        )

    def test_duplicate_primary_suppresses_materialization(self) -> None:
        first = child(
            "research-1",
            "research-department",
            "running",
            body=(
                "workflow_root_task_id=root\n"
                "workflow_role=primary\n"
                "workflow_mode=analysis"
            ),
        )
        second = child(
            "research-2",
            "research-department",
            "ready",
            body=(
                "workflow_root_task_id=root\n"
                "workflow_role=primary\n"
                "workflow_mode=analysis"
            ),
        )
        state = SupervisorState(
            parent_task_id="root",
            children=(first, second),
            workflow_mode="analysis",
            selected_primary_profiles=self.selected,
            root_is_user_query=True,
        )

        self.assertIn(
            "research-department",
            state.duplicate_primary_profiles,
        )
        self.assertEqual(
            _initial_primary_materialization_decisions(
                state,
                self.root_body(),
            ),
            (),
        )


class DelegationProjectionOrderingTest(unittest.TestCase):
    selected = (
        "research-department",
        "quant-backtest-department",
        "risk-management",
    )

    @classmethod
    def root_body(cls) -> str:
        return (
            "origin=user-query\n"
            "workflow_role=root\n"
            "workflow_mode=analysis\n"
            "analysis_mode=fast_advisory\n"
            "selected_primary_profiles=" + ",".join(cls.selected) + "\n"
            "delegation_instruction.research-department=Research plan\n"
            "delegation_instruction.quant-backtest-department=Quant plan\n"
            "delegation_instruction.risk-management=Risk plan\n"
        )

    def test_delegation_card_commits_before_primary_creation(self) -> None:
        timeline: list[str] = []

        class Client:
            def __init__(self, home: str) -> None:
                self.environment = {"HERMES_HOME": home}
                self.created: list[dict[str, object]] = []

            def show(self, task_id: str):
                return {
                    "id": task_id,
                    "assignee": "ceo-agent",
                    "status": "done",
                    "body": self.root_body(),
                }

            def create_task(self, **kwargs):
                timeline.append(f"child:{kwargs['assignee']}")
                self.created.append(kwargs)
                return {"id": f"child-{len(self.created)}"}

            def root_body(self):
                return DelegationProjectionOrderingTest.root_body()

        class Delivery:
            def __init__(self, home: str) -> None:
                self.environment = {"HERMES_HOME": home}
                self.calls = 0

            def upsert_thread_card(self, **kwargs):
                self.calls += 1
                timeline.append("ceo-dispatch")
                return "sent"

        with tempfile.TemporaryDirectory() as home:
            client = Client(home)
            delivery = Delivery(home)
            service = CeoSupervisorService(client, discord_delivery=delivery)

            handled, decision = service._materialize_completed_analysis_root_fast(
                task_id="root",
                kind="completed",
            )

        self.assertTrue(handled)
        self.assertIsNotNone(decision)
        self.assertEqual(
            timeline[0],
            "ceo-dispatch",
            "delegation display must precede primary child dispatch",
        )
        self.assertCountEqual(
            timeline[1:],
            [
                "child:research-department",
                "child:quant-backtest-department",
                "child:risk-management",
            ],
        )
        self.assertEqual(delivery.calls, 1)
        self.assertIn(
            "Fast advisory execution guardrails:",
            str(client.created[0]["body"]),
        )
        self.assertIn(
            "complete user-facing answer in result",
            str(client.created[0]["body"]),
        )
        self.assertIn(
            "summary to a brief handoff",
            str(client.created[0]["body"]),
        )

    def test_delegation_failure_does_not_change_child_execution_policy(self) -> None:
        timeline: list[str] = []

        class Client:
            environment: ClassVar[dict[str, object]] = {
                "HERMES_HOME": "/tmp/ceo-ordering-failure"
            }

            def show(self, task_id: str):
                return {
                    "id": task_id,
                    "assignee": "ceo-agent",
                    "status": "done",
                    "body": DelegationProjectionOrderingTest.root_body(),
                }

            def create_task(self, **kwargs):
                timeline.append(f"child:{kwargs['assignee']}")
                return {"id": kwargs["assignee"]}

        class FailingDelivery:
            def upsert_thread_card(self, **kwargs):
                timeline.append("ceo-dispatch-attempt")
                raise RuntimeError("delivery failure")

        service = CeoSupervisorService(Client(), discord_delivery=FailingDelivery())
        handled, _ = service._materialize_completed_analysis_root_fast(
            task_id="root",
            kind="completed",
        )

        self.assertTrue(handled)
        self.assertEqual(timeline[0], "ceo-dispatch-attempt")
        self.assertEqual(len(timeline), 4)

    def test_fast_projection_lock_blocks_concurrent_root_materialization(self) -> None:
        bridge_started = threading.Event()
        release_bridge = threading.Event()
        child_created = threading.Event()
        timeline: list[str] = []

        class Client:
            environment: ClassVar[dict[str, object]] = {
                "HERMES_HOME": "/tmp/ceo-ordering-lock"
            }

            def show(self, task_id: str):
                return {
                    "id": task_id,
                    "assignee": "ceo-agent",
                    "status": "done",
                    "body": DelegationProjectionOrderingTest.root_body(),
                }

            def create_task(self, **kwargs):
                timeline.append(f"child:{kwargs['assignee']}")
                child_created.set()
                return {"id": kwargs["assignee"]}

        class BlockingDelivery:
            def upsert_thread_card(self, **kwargs):
                timeline.append("ceo-dispatch")
                bridge_started.set()
                self_release = release_bridge.wait(timeout=2)
                if not self_release:
                    raise AssertionError("test bridge was not released")
                return "sent"

        service = CeoSupervisorService(Client(), discord_delivery=BlockingDelivery())
        with ThreadPoolExecutor(max_workers=2) as pool:
            fast = pool.submit(
                service._materialize_completed_analysis_root_fast,
                task_id="root",
                kind="completed",
            )
            self.assertTrue(bridge_started.wait(timeout=2))

            # A second materializer must not dispatch a child while the fast
            # path is still publishing the CEO delegation card.
            concurrent = pool.submit(
                service._materialize_completed_analysis_root_fast,
                task_id="root",
                kind="completed",
            )
            time.sleep(0.05)
            self.assertFalse(child_created.is_set())
            release_bridge.set()
            fast.result(timeout=2)
            concurrent.result(timeout=2)

        self.assertGreaterEqual(timeline.count("ceo-dispatch"), 1)
        self.assertTrue(child_created.is_set())
        first_child_index = next(
            index for index, item in enumerate(timeline) if item.startswith("child:")
        )
        self.assertLess(timeline.index("ceo-dispatch"), first_child_index)

    def test_fast_projection_skips_after_authoritative_primary_exists(self) -> None:
        class Client:
            environment: ClassVar[dict[str, object]] = {
                "HERMES_HOME": "/tmp/ceo-ordering-existing"
            }

            def __init__(self) -> None:
                self.created: list[dict[str, object]] = []

            def show(self, task_id: str):
                if task_id == "root":
                    return {
                        "id": task_id,
                        "assignee": "ceo-agent",
                        "status": "done",
                        "body": DelegationProjectionOrderingTest.root_body(),
                    }
                return next(item for item in self.created if item["id"] == task_id)

            def root_scoped_task_ids(self, root_id: str):
                return (root_id,) + tuple(item["id"] for item in self.created)

            def create_task(self, **kwargs):
                task_id = f"child-{len(self.created) + 1}"
                payload = {
                    "id": task_id,
                    "assignee": kwargs["assignee"],
                    "status": "ready",
                    "body": kwargs["body"],
                }
                self.created.append(payload)
                return payload

        class Delivery:
            def __init__(self) -> None:
                self.calls = 0

            def upsert_thread_card(self, **kwargs):
                self.calls += 1
                return "sent"

        client = Client()
        delivery = Delivery()
        service = CeoSupervisorService(client, discord_delivery=delivery)

        first_handled, _ = service._materialize_completed_analysis_root_fast(
            task_id="root",
            kind="completed",
        )
        second_handled, second_decision = (
            service._materialize_completed_analysis_root_fast(
                task_id="root",
                kind="completed",
            )
        )

        self.assertTrue(first_handled)
        self.assertFalse(second_handled)
        self.assertIsNone(second_decision)
        self.assertEqual(len(client.created), 3)
        self.assertEqual(delivery.calls, 1)

    def test_replayed_root_event_keeps_delegation_card_exactly_once(self) -> None:
        timeline: list[str] = []

        class Client:
            environment: ClassVar[dict[str, object]] = {
                "HERMES_HOME": "/tmp/ceo-ordering-replay"
            }

            def __init__(self):
                self.created = []

            def show(self, task_id: str):
                return {
                    "id": task_id,
                    "assignee": "ceo-agent",
                    "status": "done",
                    "body": DelegationProjectionOrderingTest.root_body(),
                }

            def create_task(self, **kwargs):
                self.created.append(kwargs)
                return {"id": f"child-{len(self.created)}"}

        class Delivery:
            def upsert_thread_card(self, **kwargs):
                timeline.append("ceo-dispatch")
                return "sent"

        client = Client()
        service = CeoSupervisorService(client, discord_delivery=Delivery())
        event = {
            "event_id": "root-replay",
            "task_id": "root",
            "assignee": "ceo-agent",
            "kind": "completed",
        }

        service.handle_terminal_event(event)
        service.handle_terminal_event(event)

        self.assertEqual(timeline, ["ceo-dispatch"])


class SupervisorWorkflowRootCacheTest(unittest.TestCase):
    """Hot-path root cache removes redundant workflow reconstruction."""

    class CacheClient:
        def __init__(self):
            self.environment = {}
            self.workflow_calls = 0
            self.show_calls = []

        def workflow(self, task_id: str):
            self.workflow_calls += 1
            return (
                "root-cache",
                (
                    {
                        "id": "root-cache",
                        "assignee": "ceo-agent",
                        "status": "done",
                        "body": (
                            "workflow_root_task_id=root-cache\n"
                            "workflow_role=root\n"
                            "workflow_mode=analysis\n"
                        ),
                    },
                    {
                        "id": "child-cache",
                        "assignee": "research-department",
                        "status": "running",
                        "body": (
                            "workflow_root_task_id=root-cache\n"
                            "workflow_role=primary\n"
                            "workflow_mode=analysis\n"
                        ),
                    },
                ),
            )

        def show(self, task_id: str):
            self.show_calls.append(task_id)

            if task_id == "root-cache":
                return {
                    "id": "root-cache",
                    "assignee": "ceo-agent",
                    "status": "done",
                    "body": (
                        "workflow_root_task_id=root-cache\n"
                        "workflow_role=root\n"
                        "workflow_mode=analysis\n"
                    ),
                }

            return {
                "id": task_id,
                "assignee": "research-department",
                "status": "running",
                "body": (
                    "workflow_root_task_id=root-cache\n"
                    "workflow_role=primary\n"
                    "workflow_mode=analysis\n"
                ),
            }

    def test_remember_workflow_root_populates_known_tasks(self):
        client = self.CacheClient()
        service = CeoSupervisorService(client)

        root_id, payloads = client.workflow("child-cache")

        service._remember_workflow_root(
            "child-cache",
            root_id,
            payloads,
        )

        self.assertEqual(
            service._cached_workflow_root("child-cache"),
            "root-cache",
        )
        self.assertEqual(
            service._cached_workflow_root("root-cache"),
            "root-cache",
        )

    def test_cached_active_event_skips_workflow_reconstruction(self):
        client = self.CacheClient()
        service = CeoSupervisorService(client)

        service._remember_workflow_root(
            "child-cache",
            "root-cache",
        )

        delivered = []

        service._deliver_department_progress = lambda **kwargs: delivered.append(kwargs)

        service.handle_terminal_event(
            {
                "event_id": "started:child-cache",
                "task_id": "child-cache",
                "assignee": "research-department",
                "kind": "started",
            }
        )

        self.assertEqual(
            client.workflow_calls,
            0,
            "cache hit must avoid workflow reconstruction",
        )
        self.assertEqual(
            client.show_calls,
            ["child-cache"],
        )
        self.assertEqual(len(delivered), 1)

    def test_non_department_active_event_skips_all_kanban_reads(self):
        client = self.CacheClient()
        service = CeoSupervisorService(client)

        service.handle_terminal_event(
            {
                "event_id": "started:ceo-root",
                "task_id": "ceo-root",
                "assignee": "ceo-agent",
                "kind": "claimed",
            }
        )

        self.assertEqual(client.workflow_calls, 0)
        self.assertEqual(client.show_calls, [])

    def test_first_active_event_uses_at_most_one_task_show_and_no_workflow(self):
        client = self.CacheClient()
        service = CeoSupervisorService(client)

        delivered = []

        service._deliver_department_progress = lambda **kwargs: delivered.append(kwargs)
        service._reconcile_department_start_progress = lambda **kwargs: None

        service.handle_terminal_event(
            {
                "event_id": "started:first:child-cache",
                "task_id": "child-cache",
                "assignee": "research-department",
                "kind": "started",
            }
        )

        self.assertEqual(client.workflow_calls, 0)
        self.assertEqual(client.show_calls, ["child-cache"])
        self.assertEqual(
            service._cached_workflow_root("child-cache"),
            "root-cache",
        )
        self.assertEqual(
            service._cached_workflow_root("root-cache"),
            "root-cache",
        )
        self.assertEqual(len(delivered), 1)


class AuthoritativePayloadReuseTest(unittest.TestCase):
    """Terminal observers must reuse the locked authoritative snapshot."""

    def test_terminal_reconciliation_does_not_reread_authoritative_tasks(self):
        root_id = "root-authoritative"
        synthesis_id = "synthesis-authoritative"
        selected = "research-department,risk-management"
        root_body = (
            build_root_body("Samsung", "req-authoritative")
            + f"\nselected_primary_profiles={selected}\n"
        )

        def primary_body(label: str) -> str:
            return build_scoped_task_body(label, root_id, role="primary")

        root = {
            "id": root_id,
            "assignee": "ceo-agent",
            "status": "done",
            "body": root_body,
        }
        research = {
            "id": "research-authoritative",
            "assignee": "research-department",
            "status": "done",
            "result": "research result",
            "final_answer": "research answer",
            "body": primary_body("research"),
        }
        risk = {
            "id": "risk-authoritative",
            "assignee": "risk-management",
            "status": "done",
            "result": "risk result",
            "final_answer": "risk answer",
            "body": primary_body("risk"),
        }
        synthesis = {
            "id": synthesis_id,
            "assignee": "ceo-agent",
            "status": "done",
            "summary": "CEO synthesis",
            "body": build_scoped_task_body(
                "synthesis\nhgfinance.ceo-supervisor.v1 action=SYNTHESIZE",
                root_id,
                role="synthesis",
            ),
        }

        class Client:
            environment: ClassVar[dict[str, object]] = {
                "HERMES_HOME": "/tmp/ceo-authoritative-test"
            }

            def __init__(self):
                self.show_calls = []

            def workflow_root(self, task_id):
                return root_id

            def authoritative_workflow_snapshot(self, known_root, task_id):
                return root_id, (research, risk, synthesis), root

            def show(self, task_id):
                self.show_calls.append(task_id)
                return {"id": task_id}

            def comment_task(self, task_id, text):
                return None

        class Delivery:
            def __init__(self):
                self.cards = []

            def upsert_thread_card(self, **kwargs):
                self.cards.append(kwargs)
                return "sent"

        client = Client()
        delivery = Delivery()
        service = CeoSupervisorService(client, discord_delivery=delivery)
        service._project_terminal_task = lambda **kwargs: "sent"
        service.decider = lambda state: None

        service.handle_terminal_event(
            {
                "event_id": "authoritative-synthesis",
                "task_id": synthesis_id,
                "kind": "completed",
            }
        )

        self.assertEqual(
            client.show_calls,
            [],
            "authoritative snapshot payloads must satisfy terminal observers",
        )
        self.assertEqual(
            {card["source_task"]["id"] for card in delivery.cards},
            {research["id"], risk["id"]},
        )


class HermesCliObservabilityTest(unittest.TestCase):
    def test_list_uses_native_reader_when_available(self) -> None:
        def runner(*args, **kwargs):
            raise AssertionError("native Kanban reader should avoid the CLI")

        class DirectReader:
            available = True

            def list_tasks(self, *, include_archived=False):
                return (
                    {
                        "id": "archived-task",
                        "status": "archived" if include_archived else "done",
                    },
                )

        client = HermesKanbanClient(runner=runner)
        client._direct_show_reader = DirectReader()

        self.assertEqual(
            client.list_tasks(include_archived=True),
            ({"id": "archived-task", "status": "archived"},),
        )

    def test_success_logs_operation_lane_and_overlap_metrics(self) -> None:
        import json
        import subprocess

        def runner(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, json.dumps([]), "")

        client = HermesKanbanClient(runner=runner, timeout=2)
        with (
            self.assertLogs(
                "orchestration.adapters.ceo_supervisor", level="INFO"
            ) as logs,
            cli_lane("recovery"),
        ):
            self.assertEqual(client.list_tasks(), ())

        rendered = "\n".join(logs.output)
        self.assertIn("operation=list", rendered)
        self.assertIn("lane=recovery", rendered)
        self.assertIn("success=true", rendered)
        self.assertNotIn("[]", rendered)
        metrics = client.cli_metrics_snapshot()
        self.assertEqual(metrics["max_active_cli_calls"], 1)
        self.assertEqual(metrics["by_operation"]["recovery:list"]["count"], 1)

    def test_timeout_is_classified_without_command_or_prompt(self) -> None:
        import subprocess

        def runner(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=2)

        client = HermesKanbanClient(runner=runner, timeout=2)
        with (
            self.assertLogs(
                "orchestration.adapters.ceo_supervisor", level="INFO"
            ) as logs,
            self.assertRaises(HermesKanbanCommandError),
            cli_lane("event"),
        ):
            client.show("task-with-secret-prompt")

        rendered = "\n".join(logs.output)
        self.assertIn("operation=show", rendered)
        self.assertIn("lane=event", rendered)
        self.assertIn("stderr_category=TIMEOUT", rendered)
        self.assertIn("success=false", rendered)
        self.assertNotIn("task-with-secret-prompt", rendered)

    def test_nonzero_sqlite_lock_is_classified_and_stderr_is_redacted(self) -> None:
        import subprocess

        def runner(args, **kwargs):
            return subprocess.CompletedProcess(
                args,
                17,
                "",
                "database is locked; raw user prompt must not be logged",
            )

        client = HermesKanbanClient(runner=runner)
        with (
            self.assertLogs(
                "orchestration.adapters.ceo_supervisor", level="INFO"
            ) as logs,
            self.assertRaises(HermesKanbanCommandError),
            cli_lane("synthesis-recovery"),
        ):
            client.list_tasks()

        rendered = "\n".join(logs.output)
        self.assertIn("operation=list", rendered)
        self.assertIn("lane=synthesis-recovery", rendered)
        self.assertIn("return_code=17", rendered)
        self.assertIn("stderr_category=SQLITE_LOCK", rendered)
        self.assertNotIn("raw user prompt", rendered)

    def test_json_error_gets_structured_category(self) -> None:
        import subprocess

        def runner(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, "not-json", "")

        client = HermesKanbanClient(runner=runner)
        with (
            self.assertLogs(
                "orchestration.adapters.ceo_supervisor", level="INFO"
            ) as logs,
            self.assertRaises(HermesKanbanCommandError),
        ):
            client.show("task")

        self.assertIn("stderr_category=JSON_ERROR", "\n".join(logs.output))

    def test_lane_and_max_active_calls_are_observed_across_threads(self) -> None:
        import json
        import subprocess

        def runner(args, **kwargs):
            time.sleep(0.03)
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps({"task": {"id": "task", "parents": [], "children": []}}),
                "",
            )

        client = HermesKanbanClient(runner=runner)

        def call_show():
            with cli_lane("event"):
                return client.show("task")

        with ThreadPoolExecutor(max_workers=2) as pool:
            tuple(pool.map(lambda _item: call_show(), range(2)))

        metrics = client.cli_metrics_snapshot()
        self.assertGreaterEqual(metrics["max_active_cli_calls"], 2)
        self.assertEqual(metrics["by_operation"]["event:show"]["count"], 2)

    def test_lane_propagates_into_parallel_hydration(self) -> None:
        import json
        import subprocess

        def runner(args, **kwargs):
            task_id = args[3]
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps({"task": {"id": task_id, "parents": [], "children": []}}),
                "",
            )

        client = HermesKanbanClient(runner=runner)
        with cli_lane("recovery"):
            client._hydrate_ids(("task-a", "task-b"))

        metrics = client.cli_metrics_snapshot()
        self.assertEqual(metrics["by_operation"]["recovery:show"]["count"], 2)

    def test_workflow_reconstruction_span_has_explicit_operation(self) -> None:
        import json
        import subprocess

        def runner(args, **kwargs):
            if args[1:3] == ["kanban", "list"]:
                return subprocess.CompletedProcess(args, 0, json.dumps([]), "")
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    {
                        "task": {
                            "id": "root",
                            "body": build_root_body("request", "request-1"),
                            "parents": [],
                            "children": [],
                        }
                    }
                ),
                "",
            )

        client = HermesKanbanClient(runner=runner)
        with (
            self.assertLogs(
                "orchestration.adapters.ceo_supervisor", level="INFO"
            ) as logs,
            cli_lane("event"),
        ):
            client.workflow("root")

        rendered = "\n".join(logs.output)
        self.assertIn("operation=workflow-reconstruction", rendered)
        self.assertIn("lane=event", rendered)


if __name__ == "__main__":
    unittest.main()
