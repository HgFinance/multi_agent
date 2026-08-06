#!/usr/bin/env python3
"""QA본부 LangGraph 파이프라인 - run_qa_department(artifact, evidence_store, decision_time) -> QA Assessment.

담당: 동규 (리스크/QA)
근거: 이 부서의 원안(LangGraph 실무진 + Hermes 부서장 정제)을 재일님이 리서치본부에,
      동규님이 리스크본부에 적용하며 실구현 직원 패턴으로 개선했다
      (departments/01-research/scripts.py, departments/03-risk/scripts.py, 2026-07-31).
      원안 자신은 그 개선을 반영하지 않은 채 남아있어 같은 패턴으로 재작성한다(2026-08-01).

원안과의 차이 - **워커가 자유 판단 프롬프트가 아니라 이미 구현된 결정론 엔진 + 그 결과를
grounded 서술만 하는 LLM이다** (research/risk 패턴을 QA 자신에게 역적용):
  check_evidence          evidence-qa-agent 결정론 절반 (evidence_qa_engine.EvidenceQaEngine.check_artifact -
                           8단계 검사, PASS/WARN/FAIL/CONDITIONAL, input_hash 재현성)
  draft_claim_narrative   evidence-qa-agent LLM 절반 (내부 Ollama 워커 - 엔진 결과를 grounded
                           서술만 한다, 새 판정 금지)
  supervise               qa-audit-supervisor 페르소나 (hermes/config.yaml 원문, Hermes AIAgent 호출 -
                           종합·Escalation만, 판정 자체는 못 바꾼다)
  hallucination_review    hallucination-critic (skills/agentic-rag 기존 baseline 재사용, evidence-qa-agent
                           코퍼스 그대로 씀) - UNSUPPORTED/CONTRADICTED로 이미 플래그된 claim만 대상,
                           그 결과를 뒤집지 않고 유형 분류·인용 근거만 덧붙인다(2026-08-02 추가)
  notion_report           Reporter Node (결정론 - notion_reporter.upload_case, LLM 아님) - 최종 감사
                           결과를 Notion QA DB(NOTION_QA_DB)에 Projection으로 올린다. 실패해도
                           QAState의 바인딩 판정은 못 바꾼다(2026-08-02 추가, 03-risk와 동일 패턴)

model-risk-agent/internal-audit-agent는 뺐다 - config.yaml의 not_started 항목대로 대응 결정론
서비스가 아직 없다 (research가 나머지 페르소나를 같은 이유로 뺀 것과 동일).

agent-ops-monitor/incident-postmortem-agent/tool-permission-security-reviewer는 실제 결정론
백엔드(audit/ops_health_monitor.py, audit/incident_timeline.py, audit/tool_permission_check.py)가
있지만 run_qa_department()의 QAState(artifact/evidence_store)와 공유 신호가 없어(개별 Artifact
검토가 아니라 부서 전체 Ops/Trace를 본다) 같은 그래프에 조건부로 억지로 끼워 넣지 않는다 -
"트리거할 실제 신호가 없는 노드를 조건부로 연결하면 날조한 기준이 된다" 원칙. 대신 각자의 실제
신호로만 조건부 라우팅하는 별도 소형 파이프라인 2개를 둔다:
  run_ops_incident_review     agent-ops-monitor(결정론 OpsHealthMonitor.evaluate) -> Incident가
                               실제로 뜬 경우에만(fabricated 아님, OpsAssessment.incident 그 자체)
                               incident-postmortem-agent가 FACT/INFERENCE를 Timeline에 남긴다.
  run_tool_permission_review  tool-permission-security-reviewer(결정론 check_tool_permission) ->
                               DENIED일 때만 위반 서술을 LLM이 덧붙인다.

원칙 (전 노드 공통, CLAUDE.md):
  - 바인딩 decision은 EvidenceQaEngine.check_artifact 결과에서만 온다. 워커 LLM도 qa-audit-supervisor도
    그 결과를 절대 못 바꾸고 서술(narrative)만 만든다 - config.yaml의 evidence-qa-agent 페르소나 자체가
    "you interpret ... you do not judge citations from memory"라고 명시한다.
  - 워커 LLM 주소는 팀 공용 GPU에 올린 Ollama 서버다. 환경변수로 바꾸지 않는다 - 동규님이 의도적으로
    고정한 팀 공유 인프라 주소다.
  - 부서장(Hermes AIAgent)은 config.yaml의 model.default를 그대로 쓴다 - 부서별 env var로 바꾸지 않는다
    (CLAUDE.md "model은 8개 파일 모두 동일, 바꾸려면 8개를 함께 바꾼다").
  - run_agent(Hermes Runtime) import는 모듈 최상단이 아니라 _hermes_chat 함수 안에서 한다(Lazy Import) -
    이 패키지는 프로젝트 .venv가 아니라 별도 Hermes 설치 venv(예: ~/claude)에만 있으므로, 최상단에서
    부르면 --run 없이 자체 점검만 하려는 경우에도 ModuleNotFoundError로 모듈 전체가 죽는다.

실행:
  source ~/claude/bin/activate    # run_agent(Hermes Runtime)가 이 venv에만 설치돼 있다 - 프로젝트 .venv엔 없음
  python scripts.py               # 자체 점검 (Ollama·Hermes 없음, run_agent 안 불러도 됨 - Lazy Import)
  python scripts.py --run         # 데모 Artifact로 실제 실행 (워커 Ollama, Hermes 필요) -
                                   # reports/qa_audit_report_<qa_decision_id>.md 도 같이 저장한다.
                                   # _render_report_md는 run_qa_department() 반환값을 그대로 옮기기만
                                   # 하는 순수 함수다 - LLM이 리포트 형식·내용을 자유 창작하지 않는다.
"""

from __future__ import annotations

# Current runtime: Evidence QA is always evaluated first; Model Risk and Internal
# Audit are deterministic conditional stages with independent LangGraph worker
# narration. Their non-PASS result only escalates and never changes Evidence QA
# into an approval or grants operational authority.
import hashlib
import importlib.util
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict
from uuid import UUID, uuid4

import yaml
from langgraph.graph import END, StateGraph
from langsmith.wrappers import wrap_openai
from openai import OpenAI

_BASE = Path(__file__).resolve().parent
_REPO_ROOT = _BASE.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))
from qa_employee_workers import run_employee_workers as run_qa_employee_workers

_AGENTIC_RAG_DIR = _BASE.parent.parent / "skills" / "agentic-rag"
for _p in (_BASE / "evidence", _BASE / "audit", _AGENTIC_RAG_DIR):
    sys.path.insert(0, str(_p))

from reporting import (
    evaluation_metrics,
    json_cell,
    langsmith_handoff,
    md_cell,
)

PIPELINE_VERSION = "qa-department-pipeline-v1"
from apps.observability.risk_qa import get_runtime_telemetry, observe_pipeline

QA_TELEMETRY = get_runtime_telemetry("qa")
from src.resilience import register_circuit_breaker_observer

register_circuit_breaker_observer(QA_TELEMETRY.record_circuit_breaker)


_JOURNAL_MODULE = None


def _journal_module():
    """Load this department's journal without colliding with Risk's package."""

    global _JOURNAL_MODULE
    if _JOURNAL_MODULE is None:
        journal_path = _BASE / "harness" / "journal.py"
        spec = importlib.util.spec_from_file_location(
            "qa_execution_journal", journal_path
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"QA journal을 로드할 수 없습니다: {journal_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _JOURNAL_MODULE = module
    return _JOURNAL_MODULE


def _ollama_base_url() -> str:
    raw = (os.getenv("OLLAMA_BASE_URL") or "http://127.0.0.1:11434/v1").rstrip("/")
    return raw if raw.endswith("/v1") else f"{raw}/v1"


# 직원 LLM은 부서장 Hermes와 분리된 로컬/팀 Ollama 런타임이다.
# 주소와 모델은 환경변수로 주입해 개발·CI·운영 환경을 섞지 않는다.
internal_llm = wrap_openai(
    OpenAI(
        base_url=_ollama_base_url(),
        api_key=os.getenv("OLLAMA_API_KEY", "ollama"),
        timeout=float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "8")),
    )
)


def _call_internal_llm(prompt: str) -> str:
    res = internal_llm.chat.completions.create(
        model=os.getenv("OLLAMA_CHAT_MODEL") or "qwen3:1.7b",
        messages=[{"role": "user", "content": prompt}],
    )
    return res.choices[0].message.content


_HERMES_PROFILE = "qa-department"


def _hermes_config() -> dict:
    config_path = _BASE / "hermes" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise TypeError(f"Hermes config is not a mapping: {config_path}")
    return config


def _hermes_model_config() -> dict[str, str | None]:
    raw_model = _hermes_config().get("model")
    if isinstance(raw_model, str):
        model = raw_model.strip()
        provider = base_url = api_mode = None
    elif isinstance(raw_model, dict):
        model = str(raw_model.get("default") or raw_model.get("model") or "").strip()
        provider = str(raw_model.get("provider") or "").strip() or None
        base_url = str(raw_model.get("base_url") or "").strip() or None
        api_mode = str(raw_model.get("api_mode") or "").strip() or None
    else:
        model = ""
        provider = base_url = api_mode = None
    if not model:
        raise ValueError("Hermes config model.default is required")
    return {
        "model": model,
        "provider": provider,
        "base_url": base_url,
        "api_mode": api_mode,
    }


def _profile_dir() -> Path:
    from hermes_cli.profiles import get_profile_dir

    return Path(get_profile_dir(_HERMES_PROFILE))


def _hermes_runtime_metadata() -> dict:
    """Expose profile wiring without reading secrets or memory contents."""
    model = _hermes_model_config()
    metadata = {
        "profile": _HERMES_PROFILE,
        "provider": model["provider"],
        "model": model["model"],
        "base_url": model["base_url"],
        "config_source": str(_BASE / "hermes" / "config.yaml"),
    }
    try:
        profile_dir = _profile_dir()
        skills_dir = profile_dir / "skills"
        memories_dir = profile_dir / "memories"
        runtime_config = yaml.safe_load(
            (profile_dir / "config.yaml").read_text(encoding="utf-8")
        )
        runtime_model = (
            runtime_config.get("model", {}) if isinstance(runtime_config, dict) else {}
        )
        runtime_provider = (
            runtime_model.get("provider") if isinstance(runtime_model, dict) else None
        )
        runtime_default = (
            runtime_model.get("default")
            if isinstance(runtime_model, dict)
            else runtime_model
        )
        metadata.update(
            {
                "runtime_profile_dir": str(profile_dir),
                "runtime_config_provider": runtime_provider,
                "runtime_config_model": runtime_default,
                "runtime_config_matches_source": runtime_provider == model["provider"]
                and runtime_default == model["model"],
                "soul_md_present": (profile_dir / "SOUL.md").is_file(),
                "skills_dir_present": skills_dir.is_dir(),
                "skill_file_count": sum(1 for _ in skills_dir.rglob("SKILL.md"))
                if skills_dir.is_dir()
                else 0,
                "memories_dir_present": memories_dir.is_dir(),
                "memory_file_count": sum(
                    1 for path in memories_dir.iterdir() if path.is_file()
                )
                if memories_dir.is_dir()
                else 0,
                "auth_present": (profile_dir / "auth.json").is_file(),
                "env_present": (profile_dir / ".env").is_file(),
            }
        )
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        metadata["profile_error"] = type(exc).__name__
    return metadata


@contextmanager
def _hermes_profile_scope():
    """Make AIAgent load this department's runtime profile, not the global default."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(_profile_dir()))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def _persona(name: str) -> str:
    personalities = _hermes_config().get("agent", {}).get("personalities", {})
    if not isinstance(personalities, dict) or not isinstance(
        personalities.get(name), str
    ):
        raise TypeError(f"{name} persona is missing from hermes/config.yaml")
    return personalities[name]


class QAState(TypedDict, total=False):
    model_risk_input: dict
    internal_audit_events: list
    model_risk: dict
    internal_audit: dict
    internal_audit_department: str
    audit_escalate: bool
    artifact: dict  # Artifact JSON (Claim 포함) - 검토 대상
    evidence_store: (
        dict  # {evidence_id: EvidenceChunk 필드} - QA는 근거를 직접 수집하지 않는다
    )
    decision_time: str  # PIT 기준 시각 (ISO8601)
    assessment: dict  # evidence-qa-agent 결정론 결과 (QaAssessment)
    claim_narrative: str  # evidence-qa-agent LLM 결과 (grounded 서술)
    hallucination_reviews: list  # hallucination-critic 결과 (UNSUPPORTED/CONTRADICTED claim만, 비었으면 전부 SUPPORTED/PARTIAL)
    verdict: str  # 최종 값 - 항상 assessment 에서만 옴
    narrative: str
    supervisor_llm_called: bool
    supervisor_call_status: str
    hermes_runtime: dict
    escalate: bool
    notion_upload: (
        dict  # Reporter Node 결과 ({"ok": bool, "url"|"reason"|"error": ...})
    )
    fallbacks: list[dict]
    employee_workers: dict
    observability: dict
    report_markdown: str
    evaluation: dict


def _fallback(stage: str, exc: Exception) -> dict:
    return {"stage": stage, "error": type(exc).__name__, "action": "ESCALATE"}


def _fallback_narrative(stage: str, exc: Exception) -> str:
    return (
        f"{stage} Agent를 사용할 수 없어 결정론적 QA 판정만 유지했습니다 "
        f"({type(exc).__name__}). 원본 부서와 독립 검토자에게 수동 확인을 요청합니다."
    )


def _fallback_assessment(state: QAState, exc: Exception) -> dict:
    payload = json.dumps(
        state.get("artifact", {}), ensure_ascii=False, sort_keys=True, default=str
    )
    input_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return {
        "assessment": {
            "qa_decision_id": str(uuid4()),
            "decision": "FAIL",
            "reason_codes": ["pipeline_fallback"],
            "calculation_version": "qa-pipeline-fallback-v1",
            "input_hash": input_hash,
            "claim_checks": [],
            "findings": [
                {
                    "finding_id": str(uuid4()),
                    "finding_type": "pipeline_failure",
                    "severity": "CRITICAL",
                    "description": _fallback_narrative("Evidence QA", exc),
                }
            ],
        },
        "fallbacks": [_fallback("evidence_check", exc)],
    }


# ── 노드 1: Evidence 검사 (결정론 직원 - EvidenceQaEngine) ─────────────────
def check_evidence(state: QAState) -> dict:
    from evidence_qa_engine import (
        Artifact,
        EvidenceChunk,
        EvidenceQaEngine,
        EvidenceStore,
        QaContext,
    )

    chunks = {
        UUID(eid): EvidenceChunk(evidence_id=UUID(eid), **fields)
        for eid, fields in state["evidence_store"].items()
    }
    ctx = QaContext(
        evidence_store=EvidenceStore(chunks=chunks),
        decision_time=datetime.fromisoformat(state["decision_time"]),
    )
    result = EvidenceQaEngine().check_artifact(Artifact(**state["artifact"]), ctx)
    return {
        "assessment": {
            "qa_decision_id": str(result.qa_decision_id),
            "decision": result.decision.value,
            "reason_codes": [r.value for r in result.reason_codes],
            "calculation_version": result.calculation_version,
            "input_hash": result.input_hash,
            "claim_checks": [
                {
                    "claim_index": c.claim_index,
                    "claim": c.claim,
                    "result": c.result.value,
                    "reason": c.reason,
                }
                for c in result.claim_checks
            ],
            "findings": [
                {
                    "finding_id": str(f.finding_id),
                    "finding_type": f.finding_type,
                    "severity": f.severity.value,
                    "description": f.description,
                }
                for f in result.findings
            ],
        }
    }


# ── 노드 1.25: Model Risk / Internal Audit (결정론적 엔진 + 직원 Worker) ────────
def model_and_internal_audit(state: QAState) -> dict:
    """Run optional P1 reviews when their governed input signals are present.

    The engines remain deterministic.  The LangGraph employee Worker receives
    only their serialised results later in ``draft_claim_narrative``; it cannot
    change the binding Evidence QA decision.
    """
    output: dict[str, object] = {}

    model_input = state.get("model_risk_input")
    if model_input is not None:
        try:
            from model_risk import ModelRiskEngine, ModelRiskInput

            evidence = ModelRiskInput(
                model_id=UUID(str(model_input["model_id"])),
                model_version=str(model_input["model_version"]),
                prompt_version=str(model_input["prompt_version"]),
                dataset_version=str(model_input["dataset_version"]),
                evaluation_count=int(model_input["evaluation_count"]),
                accuracy=float(model_input["accuracy"]),
                calibration_error=float(model_input["calibration_error"]),
                drift_score=float(model_input["drift_score"]),
                protected_failure_rate=float(model_input["protected_failure_rate"]),
            )
            assessment = ModelRiskEngine().evaluate(evidence)
            output["model_risk"] = {
                "decision": assessment.decision.value,
                "reason_codes": list(assessment.reason_codes),
                "calculation_version": assessment.calculation_version,
                "input_hash": assessment.input_hash,
            }
        except Exception as exc:  # noqa: BLE001 - invalid audit input escalates
            output["model_risk"] = {
                "decision": "ESCALATE",
                "reason_codes": ["model_risk_input_invalid"],
                "calculation_version": "qa-model-risk-v1",
                "input_hash": hashlib.sha256(
                    json.dumps(model_input, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest(),
                "error": type(exc).__name__,
            }

    audit_events = state.get("internal_audit_events")
    if audit_events is not None:
        try:
            from internal_audit import InternalAuditEngine

            assessment = InternalAuditEngine().evaluate(
                events=audit_events,
                expected_department=str(state.get("internal_audit_department") or "qa"),
            )
            output["internal_audit"] = {
                "decision": assessment.decision.value,
                "findings": list(assessment.findings),
                "calculation_version": assessment.calculation_version,
                "input_hash": assessment.input_hash,
            }
        except Exception as exc:  # noqa: BLE001 - invalid audit input escalates
            output["internal_audit"] = {
                "decision": "ESCALATE",
                "findings": ["internal_audit_input_invalid"],
                "calculation_version": "qa-internal-audit-v1",
                "input_hash": hashlib.sha256(
                    json.dumps(audit_events, sort_keys=True, default=str).encode(
                        "utf-8"
                    )
                ).hexdigest(),
                "error": type(exc).__name__,
            }

    decisions = [
        str(result.get("decision"))
        for result in (output.get("model_risk"), output.get("internal_audit"))
        if isinstance(result, dict)
    ]
    output["audit_escalate"] = any(decision != "PASS" for decision in decisions)
    return output


# ── 노드 1.5: Hallucination Critic (skills/agentic-rag, evidence-qa-agent 코퍼스 재사용) ──
def hallucination_review(state: QAState) -> dict:
    from src.graph import run_compliance_check

    a = state["assessment"]
    flagged = [
        c for c in a["claim_checks"] if c["result"] in ("UNSUPPORTED", "CONTRADICTED")
    ]
    if not flagged:
        # config.yaml 페르소나 프롬프트 원칙 - 이미 나온 근거를 우선하고, 불필요한 외부 호출은 피한다
        return {"hallucination_reviews": []}

    corpus_dir = _AGENTIC_RAG_DIR / "corpus" / "evidence"
    as_of_date = state["decision_time"][:10]
    return {
        "hallucination_reviews": [
            {
                "claim_index": c["claim_index"],
                **run_compliance_check(
                    c["claim"],
                    as_of_date,
                    corpus_dir=corpus_dir,
                    persona="hallucination-critic",
                ),
            }
            for c in flagged
        ]
    }


# ── 노드 2: Claim 서술 (LLM 직원 - 내부 Ollama, grounded 서술만) ───────────
def draft_claim_narrative(state: QAState) -> dict:
    a = state["assessment"]
    report = run_qa_employee_workers(
        {
            "trace_id": (state.get("artifact") or {}).get("trace_id"),
            "assessment": a,
            "hallucination_reviews": state.get("hallucination_reviews", []),
            "model_risk": state.get("model_risk"),
            "internal_audit": state.get("internal_audit"),
            "ops_assessment": state.get("ops_assessment"),
            "permission_check": state.get("permission_check"),
            "incident": state.get("incident"),
            "incident_events": state.get("incident_events", []),
        },
        llm=lambda system, prompt: _call_internal_llm(f"{system}\n{prompt}"),
    )
    # evidence-qa-worker는 2026-08-06에 qa-runner로 흡수됐고 qa-runner는
    # LLM 서술이 없다(summary 필드 자체가 없음) - claim_narrative는 항상
    # 결정론적 claim_checks 요약을 쓴다. fallbacks만 다른 Worker(예:
    # hallucination-critic-worker) 실패 여부로 기록한다.
    fallbacks = []
    if report.get("failed"):
        fallbacks = [
            {
                "stage": "claim_narrative",
                "node": "qa-employee-workers",
                "error": ";".join(
                    str(item.get("error"))
                    for item in report.get("workers", [])
                    if item.get("status") != "COMPLETED"
                ),
                "action": "ESCALATE",
                "safe_action": "ESCALATE",
                "decision_origin": "FALLBACK",
            }
        ]
    deterministic_summary = "; ".join(
        f"Claim {item.get('claim_index')}: {item.get('result')} ({item.get('reason', '')})"
        for item in a.get("claim_checks", [])
    )
    return {
        "claim_narrative": "결정론적 Evidence QA 결과를 전달합니다: " + deterministic_summary,
        "employee_workers": report,
        "fallbacks": fallbacks,
    }


# ── 노드 3: 종합 (qa-audit-supervisor 페르소나 - Hermes AIAgent) ───────────
def _hermes_chat(persona: str, task: str) -> str:
    from run_agent import (
        AIAgent,  # Lazy Import - Hermes 없는 환경에서도 모듈 자체는 항상 import 가능해야 한다
    )

    model = _hermes_model_config()
    with _hermes_profile_scope():
        agent = AIAgent(
            provider=model["provider"],
            model=model["model"],
            base_url=model["base_url"],
            api_mode=model["api_mode"],
            quiet_mode=True,
            load_soul_identity=True,
            skip_memory=False,
            ephemeral_system_prompt=persona,
        )
        return agent.chat(task)


def supervise(state: QAState, *, chat=None) -> dict:
    a = state["assessment"]
    bundle = {
        "decision": a["decision"],
        "reason_codes": a["reason_codes"],
        "claim_checks": a["claim_checks"],
        "findings": a["findings"],
        "claim_narrative": state["claim_narrative"],
        "hallucination_reviews": state.get("hallucination_reviews", []),
        "model_risk": state.get("model_risk"),
        "internal_audit": state.get("internal_audit"),
        "audit_escalate": state.get("audit_escalate", False),
        "employee_workers": state.get("employee_workers", {}),
    }
    task = f"""Using ONLY the evidence below, write a case-level QA audit narrative in Korean for
CEO/department review. The binding decision is "{a["decision"]}" from the deterministic Evidence QA
Engine - you cannot change it, only interpret and escalate it.
Schema (JSON only):
{{"narrative": "2-4 sentences, cite claim_checks/findings",
 "escalate": true or false, "cited_checks": ["claim indices or finding types referenced"]}}

Evidence:
{json.dumps(bundle, ensure_ascii=False, indent=1)}"""

    call = chat or _hermes_chat
    out = call(_persona("qa-audit-supervisor"), task)
    s, e = out.find("{"), out.rfind("}")
    note = json.loads(out[s : e + 1])
    for k in ("narrative", "escalate", "cited_checks"):
        if k not in note:
            raise ValueError(f"Supervisor 종합 결과에 {k} 가 없다 - 초안 거부")
    # 바인딩 decision 은 LLM 출력이 아니라 assessment 에서 그대로 가져온다
    call_status = "injected" if chat is not None else "succeeded"
    return {
        "verdict": a["decision"],
        "narrative": note["narrative"],
        "escalate": bool(note["escalate"] or state.get("audit_escalate", False)),
        "supervisor_llm_called": True,
        "supervisor_call_status": call_status,
        "hermes_runtime": {**_hermes_runtime_metadata(), "call_status": call_status},
    }


def _assemble_out(state: QAState) -> dict:
    """QAState -> run_qa_department()/notion_report 공용 결과 dict. 필드 목록을 한 곳에서만
    유지한다(03-risk/scripts.py와 동일 패턴) - 그래프 노드와 외부 인터페이스가 따로 베끼면
    한쪽만 필드를 늘렸을 때 드리프트가 생긴다."""
    a = state["assessment"]
    return {
        "qa_decision_id": a["qa_decision_id"],
        "verdict": state["verdict"],
        "reason_codes": a["reason_codes"],
        "claim_checks": a["claim_checks"],
        "findings": a["findings"],
        "calculation_version": a["calculation_version"],
        "input_hash": a["input_hash"],
        "claim_narrative": state["claim_narrative"],
        "hallucination_reviews": state.get("hallucination_reviews", []),
        "model_risk": state.get("model_risk"),
        "internal_audit": state.get("internal_audit"),
        "audit_escalate": state.get("audit_escalate", False),
        "narrative": state["narrative"],
        "escalate": state["escalate"],
        "execution_evidence": state.get("execution_evidence"),
    }


def _record_execution_evidence(
    payload: dict,
    result: dict,
    *,
    run_id: str,
    trace_id: str,
    as_of: str | None,
    asset: str | None,
    log_path: str | Path | None,
) -> dict:
    """Record the Hermes/LangGraph boundary without changing the QA verdict."""

    try:
        journal_module = _journal_module()
        sink = journal_module.jsonl_sink(log_path) if log_path else None
        journal = journal_module.RunJournal(
            department="qa-department",
            hermes_profile="qa-department",
            sink=sink,
        )
        input_event = journal.input_snapshot(
            run_id=run_id,
            trace_id=trace_id,
            employee_profile="qa-audit-supervisor",
            payload=payload,
            schema_id="qa.department.input.v1",
            as_of=as_of,
            asset=asset,
            model_version=PIPELINE_VERSION,
            prompt_version="qa-hermes-profile-v1",
            parameter_version="evidence-qa-engine-v1",
        )
        execution = result.get("agent_execution") or {}
        executed = list(execution.get("executed") or [])
        failed = list(execution.get("failed") or [])
        not_executed = list(execution.get("not_executed") or [])
        fallbacks = list(result.get("fallbacks") or [])
        degraded = bool(fallbacks or result.get("escalate"))
        schema_valid = bool(result.get("qa_decision_id")) and not bool(fallbacks)
        domain_valid = (
            result.get("verdict") in {"PASS", "WARN", "FAIL"} and not degraded
        )
        agents_for_log = list(dict.fromkeys(executed + failed)) or [
            "qa-pipeline-fallback"
        ]
        for employee in agents_for_log:
            journal.agent_output(
                run_id=run_id,
                trace_id=trace_id,
                employee_profile=employee,
                output={
                    "employee_profile": employee,
                    "supervisor_call_status": result.get("supervisor_call_status")
                    if employee == "qa-audit-supervisor"
                    else None,
                    "hermes_runtime": result.get("hermes_runtime")
                    if employee == "qa-audit-supervisor"
                    else None,
                    "decision": result.get("verdict"),
                    "evidence_refs": tuple(
                        str(check.get("claim_index"))
                        for check in (result.get("claim_checks") or [])
                        if check.get("claim_index") is not None
                    ),
                },
                inputs_hash=input_event.inputs_hash,
                schema_id="qa.agent-output.v1",
                schema_valid=schema_valid,
                domain_valid=domain_valid,
                failed_rule=(result.get("reason_codes") or [None])[0],
                fallback_reason=(fallbacks[0].get("stage") if fallbacks else None),
                retry_count=max(
                    (item.get("attempt", 0) for item in fallbacks), default=0
                ),
                as_of=as_of,
                asset=asset,
                model_version=PIPELINE_VERSION,
                prompt_version="qa-hermes-profile-v1",
                parameter_version="evidence-qa-engine-v1",
            )
        failed_rule = (result.get("reason_codes") or [None])[0]
        if fallbacks and not failed_rule:
            failed_rule = fallbacks[0].get("stage") or "pipeline_fallback"
        journal.validation(
            run_id=run_id,
            trace_id=trace_id,
            employee_profile="qa-audit-supervisor",
            inputs_hash=input_event.inputs_hash,
            schema_id="qa.department.output.v1",
            schema_valid=schema_valid,
            domain_valid=domain_valid,
            failed_rule=failed_rule,
            fallback_reason=(fallbacks[0].get("reason") if fallbacks else None),
            retry_count=max((item.get("attempt", 0) for item in fallbacks), default=0),
            raw={"executed": executed, "not_executed": not_executed},
            as_of=as_of,
            asset=asset,
        )
        safe_action = "ESCALATE" if degraded else result.get("verdict", "FAIL")
        journal.decision(
            run_id=run_id,
            trace_id=trace_id,
            employee_profile="qa-audit-supervisor",
            inputs_hash=input_event.inputs_hash,
            output={
                "decision": result.get("verdict"),
                "safe_action": safe_action,
                "binding": False,
            },
            schema_id="qa.department.decision.v1",
            schema_valid=schema_valid,
            domain_valid=domain_valid,
            failed_rule=failed_rule,
            fallback_reason=(fallbacks[0].get("reason") if fallbacks else None),
            retry_count=max((item.get("attempt", 0) for item in fallbacks), default=0),
            as_of=as_of,
            asset=asset,
        )
        review = journal.review(run_id)
        return {
            "run_id": run_id,
            "trace_id": trace_id,
            "input_hash": input_event.inputs_hash,
            "pipeline_status": "DEGRADED" if degraded else "COMPLETED",
            "candidate_verdict": result.get("verdict"),
            "safe_action": safe_action,
            "binding": False,
            "hermes_profile": "qa-department",
            "employees_executed": executed,
            "employees_failed": failed,
            "employees_not_executed": not_executed,
            "retry_policy": {"max_retries": 2, "max_attempts": 3},
            "external_dependencies": {
                "policy_corpus": "PLACEHOLDER_OR_UNVERIFIED",
                "canonical_db": "CONFIGURED"
                if (
                    os.environ.get("RISK_QA_DATABASE_URL")
                    or os.environ.get("DATABASE_URL")
                )
                else "NOT_CONFIGURED",
                "agent_runs_tool_calls": "CONFIGURED"
                if (
                    os.environ.get("RISK_QA_DATABASE_URL")
                    or os.environ.get("DATABASE_URL")
                )
                else "NOT_CONFIGURED",
            },
            "event_types": [
                event.event_type.value for event in journal.events_for_run(run_id)
            ],
            "replay_ready": review["replay_ready"],
            "journal_event_count": len(journal.events_for_run(run_id)),
            "journal_path": str(log_path) if log_path else None,
            "review": review,
        }
    except Exception as exc:  # noqa: BLE001 - logging must never turn a safe decision into approval
        return {
            "run_id": run_id,
            "trace_id": trace_id,
            "pipeline_status": "DEGRADED",
            "candidate_verdict": result.get("verdict", "FAIL"),
            "safe_action": "ESCALATE",
            "binding": False,
            "journal_error": type(exc).__name__,
            "journal_path": str(log_path) if log_path else None,
        }


# ── 노드 4: Notion 업로드 (Reporter Node - 결정론, LLM 아님) ────────────────
def notion_report(state: QAState, *, uploader=None) -> dict:
    from notion_reporter import upload_case

    out = _assemble_out(state)
    report_md = _render_report_md(state["artifact"], state["decision_time"], out)
    upload = uploader or upload_case
    return {
        "notion_upload": upload(
            state["artifact"], state["decision_time"], out, report_md=report_md
        )
    }


# ── 그래프 조립 ────────────────────────────────────────────────────────────
def build_pipeline():
    g = StateGraph(QAState)
    g.add_node("check_evidence", check_evidence)
    g.add_node("model_and_internal_audit", model_and_internal_audit)
    g.add_node("hallucination_review", hallucination_review)
    g.add_node("draft_claim_narrative", draft_claim_narrative)
    g.add_node("supervise", supervise)
    g.add_node("notion_report", notion_report)
    g.set_entry_point("check_evidence")
    g.add_edge("check_evidence", "model_and_internal_audit")
    g.add_edge("model_and_internal_audit", "hallucination_review")
    g.add_edge("hallucination_review", "draft_claim_narrative")
    g.add_edge("draft_claim_narrative", "supervise")
    g.add_edge("supervise", "notion_report")
    g.add_edge("notion_report", END)
    return g.compile()


@observe_pipeline(QA_TELEMETRY, "pipeline")
def run_qa_department(artifact: dict, evidence_store: dict, decision_time: str) -> dict:
    """본부 단독 실행 - Risk/Research 의 run_<dept>_department 와 같은 외부 인터페이스."""
    out = build_pipeline().invoke(
        {
            "artifact": artifact,
            "evidence_store": evidence_store,
            "decision_time": decision_time,
        }
    )
    result = _assemble_out(out)
    result["notion_upload"] = out.get("notion_upload")
    return result


# ── 자체 점검 (Ollama·Hermes 없음) ──────────────────────────────────────────
def _check_graph_shape():
    p = build_pipeline()
    assert p is not None
    print("  그래프 컴파일              OK")


def _check_fail_still_narrates():
    # FAIL 판정이어도 워커·부서장 둘 다 계속 불려 서술이 남는지, decision 값 자체는
    # 그대로 유지되는지 - Ollama·Hermes·Agentic RAG 콜은 전부 스텁으로 대체한다. notion_report 도
    # 스텁한다 - 안 그러면 ai-office/.dev.vars 에 실제 토큰이 있을 때 자체 점검이 진짜 Notion에
    # 네트워크 호출을 낸다(03-risk/scripts.py와 동일 이유).
    global \
        check_evidence, \
        hallucination_review, \
        _call_internal_llm, \
        _hermes_chat, \
        notion_report
    orig = (
        check_evidence,
        hallucination_review,
        _call_internal_llm,
        _hermes_chat,
        notion_report,
    )
    check_evidence = lambda s: {
        "assessment": {
            "qa_decision_id": "d1",
            "decision": "FAIL",
            "reason_codes": ["fact_without_evidence"],
            "claim_checks": [
                {
                    "claim_index": 0,
                    "claim": "x",
                    "result": "UNSUPPORTED",
                    "reason": "근거 없음",
                }
            ],
            "findings": [
                {
                    "finding_type": "unsupported_claim",
                    "severity": "HIGH",
                    "description": "d",
                }
            ],
        }
    }
    hallucination_review = lambda s: {
        "hallucination_reviews": [
            {"claim_index": 0, "answer": {"verdict": "HALLUCINATION"}, "grounded": True}
        ]
    }
    _call_internal_llm = lambda prompt: "요약: 근거 없는 주장 1건"
    _hermes_chat = lambda persona, task: (
        '{"narrative": "차단됨", "escalate": true, "cited_checks": ["0"]}'
    )
    notion_report = lambda s: {
        "notion_upload": {"ok": False, "reason": "self-check stub"}
    }
    try:
        out = build_pipeline().invoke(
            {"artifact": {}, "evidence_store": {}, "decision_time": "x"}
        )
        assert out["assessment"]["decision"] == "FAIL"
        assert out["verdict"] == "FAIL"
        assert out["escalate"] is True
        assert out["hallucination_reviews"][0]["answer"]["verdict"] == "HALLUCINATION"
    finally:
        (
            check_evidence,
            hallucination_review,
            _call_internal_llm,
            _hermes_chat,
            notion_report,
        ) = orig
    print("  FAIL 판정에도 서술 계속됨   OK")


def _check_hallucination_review_conditional():
    # SUPPORTED/PARTIAL 뿐이면 Agentic RAG 콜 자체를 안 하는지(비용 절감), UNSUPPORTED/CONTRADICTED가
    # 있으면 그 claim만 골라 hallucination-critic 페르소나로 넘기는지 확인한다 - run_compliance_check을
    # src.graph 모듈에서 직접 스텁해 네트워크 없이 점검한다(hallucination_review는 함수 안에서 Lazy Import).
    import src.graph as agentic_graph

    clean = {
        "assessment": {
            "claim_checks": [
                {"claim_index": 0, "claim": "x", "result": "SUPPORTED", "reason": "ok"}
            ]
        }
    }
    flagged = {
        "assessment": {
            "claim_checks": [
                {
                    "claim_index": 0,
                    "claim": "ok",
                    "result": "SUPPORTED",
                    "reason": "ok",
                },
                {
                    "claim_index": 1,
                    "claim": "bad",
                    "result": "UNSUPPORTED",
                    "reason": "근거 없음",
                },
            ]
        },
        "decision_time": "2026-08-02T00:00:00+00:00",
    }
    calls = []

    def stub(query, as_of, corpus_dir=None, persona="compliance-policy-agent"):
        calls.append((query, persona, corpus_dir.name if corpus_dir else None))
        return {"answer": {"verdict": "HALLUCINATION"}, "grounded": True}

    orig = agentic_graph.run_compliance_check
    agentic_graph.run_compliance_check = stub
    try:
        assert hallucination_review(clean) == {"hallucination_reviews": []}
        out = hallucination_review(flagged)
        assert calls == [("bad", "hallucination-critic", "evidence")]
        assert out["hallucination_reviews"][0]["claim_index"] == 1
    finally:
        agentic_graph.run_compliance_check = orig
    print("  Hallucination Critic 조건부 호출  OK")


def _check_supervisor_guard():
    a = {"decision": "PASS", "reason_codes": [], "claim_checks": [], "findings": []}
    bad_chat = lambda persona, task: '{"narrative": "n"}'  # escalate/cited_checks 누락
    try:
        supervise({"assessment": a, "claim_narrative": "n"}, chat=bad_chat)
        raise AssertionError("불완전 종합 결과가 통과했다")
    except ValueError:
        pass
    print("  Supervisor 스키마 가드      OK")


# ── MD 리포트 렌더 (결정론 - LLM 미개입, run_qa_department 반환값을 그대로 옮기기만 한다) ──
def _render_report_md(artifact: dict, decision_time: str, out: dict) -> str:
    checks = out.get("claim_checks", [])
    findings = out.get("findings", [])

    lines = [
        "# AI QA/감사본부 — 감사 보고서 (결정론적 생성, LLM 자유 서술 아님)",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        f"| **qa_decision_id** | `{out['qa_decision_id']}` |",
        f"| **판정 (verdict)** | **{out['verdict']}** |",
        f"| **판정 엔진** | departments/06-ai-qa-audit/evidence/evidence_qa_engine.py (`{out['calculation_version']}`) |",
        f"| **input_hash** | `{out['input_hash']}` (같은 Artifact·Context면 재현 가능) |",
        f"| **decision_time (PIT)** | {md_cell(decision_time)} |",
        f"| **escalate** | {md_cell(out['escalate'])} |",
        f"| **생성** | {PIPELINE_VERSION}, {datetime.now(timezone.utc).isoformat()} |",
        "",
        "---",
        "",
        "## Claim별 검사 결과",
        "",
        "| # | Claim | 검사 결과 | 사유 |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {md_cell(c.get('claim_index'))} | {md_cell(c.get('claim'))} | "
        f"{md_cell(c.get('result'))} | {md_cell(c.get('reason'))} |"
        for c in checks
    ]
    if not checks:
        lines.append("| — | (claim_checks 없음) | — | — |")

    lines += ["", "## Hallucination Critic (hallucination-critic, Agentic RAG)", ""]
    reviews = out.get("hallucination_reviews") or []
    if reviews:
        for r in reviews:
            answer = r.get("answer") or {}
            lines += [
                f"### Claim #{r['claim_index']}",
                "",
                "| 필드 | 값 |",
                "|---|---|",
                f"| verdict | {md_cell(answer.get('verdict'))} |",
                f"| grounded | {md_cell(r.get('grounded'))} |",
                f"| rationale | {md_cell(answer.get('rationale'))} |",
                "",
            ]
    else:
        lines.append("UNSUPPORTED/CONTRADICTED claim 없음 - 조건부 노드 미호출")

    lines += [
        "",
        "## Reason Codes",
        "",
        ", ".join(f"`{r}`" for r in out["reason_codes"])
        if out["reason_codes"]
        else "없음",
        "",
        "## Findings",
        "",
    ]
    if findings:
        for f in findings:
            lines += [
                f"### `{md_cell(f.get('finding_id'))}`",
                "",
                "| 필드 | 값 |",
                "|---|---|",
                f"| 유형 | `{md_cell(f.get('finding_type'))}` |",
                f"| 심각도 | {md_cell(f.get('severity'))} |",
                f"| 설명 | {md_cell(f.get('description'))} |",
                "",
            ]
    else:
        lines.append("없음")

    lines += [
        "",
        "## Claim 서술 (evidence-qa-agent, 내부 Ollama - 판정 재해석 없이 결과만 풀어씀)",
        "",
        md_cell(out["claim_narrative"]),
        "",
        "## 종합 서술 (qa-audit-supervisor, Hermes)",
        "",
        md_cell(out["narrative"]),
    ]

    evaluation = out.get("evaluation") or {}
    if evaluation:
        lines += ["", "## 평가 지표", "", "| 지표 | 값 |", "|---|---|"]
        lines += [
            f"| {md_cell(key)} | {json_cell(value)} |"
            for key, value in evaluation.items()
        ]

    observability = out.get("observability") or {}
    if observability:
        lines += [
            "",
            "## LangSmith / HR 관측성 전달",
            "",
            "| 필드 | 값 |",
            "|---|---|",
            f"| trace_id | `{md_cell(observability.get('trace_id'))}` |",
            f"| LangSmith | {json_cell(observability.get('langsmith'))} |",
        ]

    agent_execution = out.get("agent_execution") or {}
    if agent_execution:
        lines += ["", "## Agent 실행 매니페스트", "", "| 구분 | Agent |", "|---|---|"]
        lines += [
            f"| 실행 | {md_cell(agent)} |"
            for agent in agent_execution.get("executed", [])
        ]
        lines += [
            f"| 실패 | {md_cell(agent)} |"
            for agent in agent_execution.get("failed", [])
        ]
        lines += [
            f"| 미실행/조건부 | {md_cell(agent)} |"
            for agent in agent_execution.get("not_executed", [])
        ]
        worker_execution = out.get("employee_workers") or {}
        if worker_execution:
            lines += [
                "",
                "### LangGraph Employee Workers",
                "",
                "| Worker | 상태 | 도구 |",
                "|---|---|---|",
            ]
            lines += [
                f"| `{md_cell(item.get('worker_id'))}` | {md_cell(item.get('status'))} | "
                f"{md_cell(', '.join(item.get('tools') or []))} |"
                for item in worker_execution.get("workers", [])
            ]
            lines += [
                f"- executor: `{md_cell((worker_execution.get('runtime') or {}).get('executor'))}`",
                f"- model: `{md_cell((worker_execution.get('runtime') or {}).get('model'))}`",
            ]
        runtime = out.get("hermes_runtime") or {}
        if runtime:
            lines += [
                "",
                "### Hermes Runtime",
                "",
                f"- profile: `{md_cell(runtime.get('profile'))}`",
                f"- provider/model: `{md_cell(runtime.get('provider'))}` / `{md_cell(runtime.get('model'))}`",
                f"- runtime config matches source: `{md_cell(runtime.get('runtime_config_matches_source'))}`",
                f"- supervisor call: `{md_cell(out.get('supervisor_call_status'))}`",
                f"- skills: `{md_cell(runtime.get('skill_file_count'))}`; memory files: `{md_cell(runtime.get('memory_file_count'))}`",
            ]

    fallbacks = out.get("fallbacks") or []
    if fallbacks:
        lines += [
            "",
            "## Fallback / Escalation",
            "",
            "| 단계 | 오류 | 조치 |",
            "|---|---|---|",
        ]
        lines += [
            f"| {md_cell(item.get('stage'))} | {md_cell(item.get('error'))} | "
            f"{md_cell(item.get('action'))} |"
            for item in fallbacks
        ]

    notion = out.get("notion_upload")
    if notion is not None:
        lines += ["", "## Notion 업로드 (Reporter Node)", ""]
        lines.append(
            f"업로드 성공: {notion['url']}"
            if notion.get("ok")
            else f"업로드 생략/실패: {notion.get('reason') or notion.get('error')}"
        )

    lines += [
        "",
        "---",
        "> 이 문서는 evidence_qa_engine.py의 결정론적 판정과 스키마 검증된 LLM 서술을 Python이 그대로",
        "> 옮긴 것이다 - LLM이 이 파일의 형식이나 내용을 자유롭게 창작하지 않았다.",
    ]
    return "\n".join(lines)


def _check_notion_report_node():
    captured = {}

    def stub_uploader(artifact, decision_time, out, *, report_md=""):
        captured["out"], captured["report_md"] = out, report_md
        return {"ok": True, "url": "https://notion.so/fake"}

    state = {
        "artifact": {"trace_id": "t1"},
        "decision_time": "2026-08-01",
        "narrative": "n",
        "escalate": False,
        "claim_narrative": "cn",
        "verdict": "PASS",
        "assessment": {
            "qa_decision_id": "d1",
            "decision": "PASS",
            "reason_codes": [],
            "claim_checks": [],
            "findings": [],
            "calculation_version": "v",
            "input_hash": "h",
        },
    }
    result = notion_report(state, uploader=stub_uploader)
    assert result["notion_upload"] == {"ok": True, "url": "https://notion.so/fake"}
    assert result["report_markdown"]
    assert captured["out"]["qa_decision_id"] == "d1"
    assert (
        "qa_decision_id" in captured["report_md"]
    )  # _render_report_md 가 실제로 불렸는지
    print("  Notion Reporter 노드        OK")


_RAW_CHECK_EVIDENCE = check_evidence
_RAW_HALLUCINATION_REVIEW = hallucination_review
_RAW_DRAFT_CLAIM_NARRATIVE = draft_claim_narrative
_RAW_SUPERVISE = supervise


def check_evidence(state: QAState) -> dict:
    try:
        return _RAW_CHECK_EVIDENCE(state)
    except Exception as exc:  # noqa: BLE001 - fail-closed boundary
        return _fallback_assessment(state, exc)


def hallucination_review(state: QAState) -> dict:
    try:
        return _RAW_HALLUCINATION_REVIEW(state)
    except Exception as exc:  # noqa: BLE001 - fail-closed boundary
        flagged = [
            c
            for c in state.get("assessment", {}).get("claim_checks", [])
            if c.get("result") in ("UNSUPPORTED", "CONTRADICTED")
        ]
        return {
            "hallucination_reviews": [
                {
                    "claim_index": c.get("claim_index"),
                    "answer": {
                        "verdict": "HALLUCINATION",
                        "cited_documents": [],
                        "rationale": _fallback_narrative("Hallucination Critic", exc),
                        "confidence": 0.0,
                        "escalate": True,
                    },
                    "grounded": False,
                    "fallback": True,
                    "fallback_reason": type(exc).__name__,
                }
                for c in flagged
            ],
            "fallbacks": [_fallback("hallucination_review", exc)],
        }


def draft_claim_narrative(state: QAState) -> dict:
    try:
        return _RAW_DRAFT_CLAIM_NARRATIVE(state)
    except Exception as exc:  # noqa: BLE001 - fail-closed boundary
        checks = state.get("assessment", {}).get("claim_checks", [])
        summary = (
            "; ".join(
                f"Claim {c.get('claim_index')}: {c.get('result')} — {c.get('reason')}"
                for c in checks
            )
            or "검사된 Claim 없음"
        )
        return {
            "claim_narrative": f"결정론적 Evidence QA 결과를 전달합니다. {summary}",
            "fallbacks": [_fallback("claim_narrative", exc)],
        }


def supervise(state: QAState, *, chat=None) -> dict:
    try:
        return _RAW_SUPERVISE(state, chat=chat)
    except Exception as exc:
        if chat is not None:
            raise
        decision = state.get("assessment", {}).get("decision", "FAIL")
        return {
            "verdict": decision,
            "narrative": _fallback_narrative("QA Supervisor", exc),
            "escalate": True,
            "supervisor_llm_called": True,
            "supervisor_call_status": "failed",
            "hermes_runtime": {
                **_hermes_runtime_metadata(),
                "call_status": "failed",
                "error_type": type(exc).__name__,
            },
            "fallbacks": [_fallback("supervisor", exc)],
        }


_RAW_ASSEMBLE_OUT = _assemble_out


def _assemble_out(state: QAState) -> dict:
    out = _RAW_ASSEMBLE_OUT(state)
    trace_id = (state.get("artifact") or {}).get("trace_id")
    trace_id = trace_id or f"{PIPELINE_VERSION}:{out['qa_decision_id']}"
    out["fallbacks"] = state.get("fallbacks", [])
    out["observability"] = langsmith_handoff(str(trace_id))
    worker_execution = state.get("employee_workers") or {}
    executed_agents = list(worker_execution.get("executed") or [])
    failed_worker_agents = list(worker_execution.get("failed") or [])
    # evidence-qa-worker/model-and-internal-audit-worker/ops-and-permission-worker는
    # 2026-08-06에 qa-runner 하나로 합쳐졌다 - 셋 중 하나라도 실행 근거가 있으면
    # qa-runner 한 번만 기록한다.
    if not worker_execution and (
        out.get("claim_checks") is not None or out.get("claim_narrative")
    ):
        executed_agents.append("qa-runner")
    if out.get("hallucination_reviews") and not worker_execution:
        executed_agents.append("hallucination-critic-worker")
    supervisor_status = state.get("supervisor_call_status", "not_called")
    if out.get("narrative") and supervisor_status in {"succeeded", "injected"}:
        executed_agents.append("qa-audit-supervisor")
    qa_agents = [
        "qa-audit-supervisor",
        "qa-runner",
        "hallucination-critic-worker",
        "incident-postmortem-worker",
    ]
    failed_agents = list(failed_worker_agents)
    if supervisor_status == "failed":
        failed_agents.append("qa-audit-supervisor")
    attempted_agents = list(dict.fromkeys(executed_agents + failed_agents))
    out["supervisor_call_status"] = supervisor_status
    out["hermes_runtime"] = state.get("hermes_runtime")
    out["agent_execution"] = {
        "executed": list(dict.fromkeys(executed_agents)),
        "failed": failed_agents,
        "attempted": attempted_agents,
        "not_executed": [agent for agent in qa_agents if agent not in attempted_agents],
    }
    out["execution_evidence"] = state.get("execution_evidence")
    out["employee_workers"] = worker_execution
    return out


_RAW_NOTION_REPORT = notion_report


def notion_report(state: QAState, *, uploader=None) -> dict:
    out = _assemble_out(state)
    report_md = _render_report_md(state["artifact"], state["decision_time"], out)
    evaluation = evaluation_metrics(out, report_md)
    report_md = _render_report_md(
        state["artifact"], state["decision_time"], {**out, "evaluation": evaluation}
    )
    evaluation = evaluation_metrics(out, report_md)
    from notion_reporter import upload_case

    try:
        upload = uploader or upload_case
        notion_upload = upload(
            state["artifact"], state["decision_time"], out, report_md=report_md
        )
    except Exception as exc:  # noqa: BLE001 - fail-closed boundary
        notion_upload = {"ok": False, "reason": f"Reporter 예외: {type(exc).__name__}"}
        evaluation["fallback_count"] += 1
    evaluation = evaluation_metrics({**out, "notion_upload": notion_upload}, report_md)
    return {
        "notion_upload": notion_upload,
        "report_markdown": report_md,
        "evaluation": evaluation,
    }


_RAW_RUN_QA_DEPARTMENT = run_qa_department


def run_qa_department(
    artifact: dict,
    evidence_store: dict,
    decision_time: str,
    *,
    run_id: str | None = None,
    log_path: str | Path | None = None,
    model_risk_input: dict | None = None,
    internal_audit_events: list[dict] | None = None,
    internal_audit_department: str = "qa",
) -> dict:
    execution_run_id = run_id or str(uuid4())
    trace_id = str((artifact or {}).get("trace_id") or f"qa:{execution_run_id}")
    payload = {
        "artifact": artifact,
        "evidence_store": evidence_store,
        "decision_time": decision_time,
    }
    asset = (
        str(
            (artifact or {}).get("asset") or (artifact or {}).get("instrument_id") or ""
        )
        or None
    )
    try:
        out = build_pipeline().invoke(
            {
                "artifact": artifact,
                "evidence_store": evidence_store,
                "decision_time": decision_time,
                "model_risk_input": model_risk_input,
                "internal_audit_events": internal_audit_events,
                "internal_audit_department": internal_audit_department,
            }
        )
    except Exception as exc:  # noqa: BLE001 - fail-closed boundary
        out = {
            "artifact": artifact,
            "evidence_store": evidence_store,
            "decision_time": decision_time,
            **_fallback_assessment({"artifact": artifact}, exc),
            "claim_narrative": _fallback_narrative("QA pipeline", exc),
            "hallucination_reviews": [],
            "verdict": "FAIL",
            "narrative": _fallback_narrative("QA Supervisor", exc),
            "escalate": True,
        }
    result = _assemble_out(out)
    out["execution_evidence"] = _record_execution_evidence(
        payload,
        result,
        run_id=execution_run_id,
        trace_id=trace_id,
        as_of=decision_time,
        asset=asset,
        log_path=log_path,
    )
    out.update(notion_report(out))
    result = _assemble_out(out)
    result["execution_evidence"] = out["execution_evidence"]
    result["notion_upload"] = out.get("notion_upload")
    result["report_markdown"] = out.get("report_markdown", "")
    result["evaluation"] = out.get("evaluation", evaluation_metrics(result))
    return result


# ── Ops Health -> Incident Postmortem (agent-ops-monitor, incident-postmortem-agent) ──
# QAState의 artifact/evidence_store와 무관한 별도 소형 파이프라인 - 근거는 위 모듈 docstring.
class OpsIncidentState(TypedDict, total=False):
    metrics: (
        dict  # AgentHealthMetrics 필드 (scope/window/*_count/p95_latency_ms/cost_usd)
    )
    thresholds: dict  # OpsThresholds 필드
    trace_id: str | None
    ops_assessment: dict  # agent-ops-monitor 결정론 결과 (OpsAssessment)
    incident_events: list  # IncidentTimeline에 실제로 남긴 FACT/INFERENCE 행
    postmortem_narrative: str
    fallbacks: list[dict]


def _event_dict(event) -> dict:
    return {
        "incident_event_id": str(event.incident_event_id),
        "source": event.source,
        "entry_type": event.entry_type.value,
        "summary": event.summary,
        "evidence": event.evidence,
    }


def _fallback_ops_assessment(state: OpsIncidentState, exc: Exception) -> dict:
    # 모니터 자체가 죽으면 "정상"으로 침묵하지 않는다 - 판정 불가를 SEV2 Incident로 격상한다
    # (원칙 9: 위험한 기능은 실패 시 확대가 아니라 차단 방향으로 - risk_engine.py와 동일 정신).
    now = datetime.now(timezone.utc)
    return {
        "ops_assessment": {
            "scope": (state.get("metrics") or {}).get("scope", "unknown"),
            "status": "critical",
            "breaches": ["monitor_pipeline_failure"],
            "incident": {
                "incident_id": str(uuid4()),
                "incident_code": f"OPS-MONITOR-FAILURE-{now.strftime('%Y%m%dT%H%M%S')}",
                "severity": "SEV2",
                "title": f"agent-ops-monitor 파이프라인 실패 ({type(exc).__name__})",
                "impact": {"error": type(exc).__name__},
                "started_at": now.isoformat(),
                "detected_at": now.isoformat(),
            },
        },
        "fallbacks": [_fallback("evaluate_ops_health", exc)],
    }


def evaluate_ops_health(state: OpsIncidentState) -> dict:
    from decimal import Decimal

    from ops_health_monitor import AgentHealthMetrics, OpsHealthMonitor, OpsThresholds

    m, t = state["metrics"], state["thresholds"]
    metrics = AgentHealthMetrics(
        scope=m["scope"],
        window_start=datetime.fromisoformat(m["window_start"]),
        window_end=datetime.fromisoformat(m["window_end"]),
        request_count=int(m["request_count"]),
        error_count=int(m["error_count"]),
        p95_latency_ms=Decimal(str(m["p95_latency_ms"])),
        cost_usd=Decimal(str(m["cost_usd"])),
    )
    thresholds = OpsThresholds(
        max_error_rate=Decimal(str(t["max_error_rate"])),
        critical_error_rate=Decimal(str(t["critical_error_rate"])),
        max_p95_latency_ms=Decimal(str(t["max_p95_latency_ms"])),
        critical_p95_latency_ms=Decimal(str(t["critical_p95_latency_ms"])),
        max_cost_usd_per_window=Decimal(str(t["max_cost_usd_per_window"])),
    )
    trace_id = state.get("trace_id")
    assessment = OpsHealthMonitor().evaluate(
        metrics, thresholds, UUID(trace_id) if trace_id else None
    )
    incident = assessment.incident
    return {
        "ops_assessment": {
            "scope": assessment.scope,
            "status": assessment.status.value,
            "breaches": [b.value for b in assessment.breaches],
            "incident": None
            if incident is None
            else {
                "incident_id": str(incident.incident_id),
                "incident_code": incident.incident_code,
                "severity": incident.severity.value,
                "title": incident.title,
                "impact": incident.impact,
                "started_at": incident.started_at.isoformat(),
                "detected_at": incident.detected_at.isoformat(),
            },
        }
    }


def _ops_incident_detected(state: OpsIncidentState) -> bool:
    # 실제 신호(OpsAssessment.incident)로만 라우팅한다 - 날조한 조건이 아니다.
    return (state.get("ops_assessment") or {}).get("incident") is not None


def record_incident_fact(state: OpsIncidentState) -> dict:
    from incident_timeline import IncidentEntryType, IncidentTimeline

    incident = state["ops_assessment"]["incident"]
    event = IncidentTimeline().add_event(
        UUID(incident["incident_id"]),
        "agent-ops-monitor",
        IncidentEntryType.FACT,
        incident["title"],
        datetime.fromisoformat(incident["detected_at"]),
        "agent-ops-monitor",
        evidence=incident["impact"],
    )
    return {"incident_events": [_event_dict(event)]}


def draft_incident_postmortem(state: OpsIncidentState) -> dict:
    from incident_timeline import IncidentEntryType, IncidentTimeline

    incident = state["ops_assessment"]["incident"]
    prompt = f"""{_persona("incident-postmortem-agent")}

아래는 agent-ops-monitor가 결정론적으로 감지한 Incident FACT다. 이미 확인된 사실 밖의 내용을
지어내지 말고, 원인 추정(INFERENCE)이라는 것을 분명히 밝히며 한국어로 1-2문장만 작성하라:
{json.dumps(incident, ensure_ascii=False, indent=1)}"""
    narrative = _call_internal_llm(prompt)
    event = IncidentTimeline().add_event(
        UUID(incident["incident_id"]),
        "incident-postmortem-agent",
        IncidentEntryType.INFERENCE,
        narrative,
        datetime.fromisoformat(incident["detected_at"]),
        "incident-postmortem-agent",
    )
    return {
        "postmortem_narrative": narrative,
        "incident_events": state.get("incident_events", []) + [_event_dict(event)],
    }


def build_ops_incident_pipeline():
    g = StateGraph(OpsIncidentState)
    g.add_node("evaluate_ops_health", evaluate_ops_health)
    g.add_node("record_incident_fact", record_incident_fact)
    g.add_node("draft_incident_postmortem", draft_incident_postmortem)
    g.set_entry_point("evaluate_ops_health")
    g.add_conditional_edges(
        "evaluate_ops_health",
        _ops_incident_detected,
        {True: "record_incident_fact", False: END},
    )
    g.add_edge("record_incident_fact", "draft_incident_postmortem")
    g.add_edge("draft_incident_postmortem", END)
    return g.compile()


def run_ops_incident_review(
    metrics: dict, thresholds: dict, trace_id: str | None = None
) -> dict:
    out = build_ops_incident_pipeline().invoke(
        {"metrics": metrics, "thresholds": thresholds, "trace_id": trace_id}
    )
    return {
        "ops_assessment": out["ops_assessment"],
        "incident_events": out.get("incident_events", []),
        "postmortem_narrative": out.get("postmortem_narrative"),
        "fallbacks": out.get("fallbacks", []),
    }


_RAW_EVALUATE_OPS_HEALTH = evaluate_ops_health
_RAW_RECORD_INCIDENT_FACT = record_incident_fact
_RAW_DRAFT_INCIDENT_POSTMORTEM = draft_incident_postmortem


def evaluate_ops_health(state: OpsIncidentState) -> dict:
    try:
        return _RAW_EVALUATE_OPS_HEALTH(state)
    except Exception as exc:  # noqa: BLE001 - fail-closed boundary
        return _fallback_ops_assessment(state, exc)


def record_incident_fact(state: OpsIncidentState) -> dict:
    try:
        return _RAW_RECORD_INCIDENT_FACT(state)
    except Exception as exc:  # noqa: BLE001 - fail-closed boundary
        return {
            "incident_events": [],
            "fallbacks": [_fallback("record_incident_fact", exc)],
        }


def draft_incident_postmortem(state: OpsIncidentState) -> dict:
    try:
        return _RAW_DRAFT_INCIDENT_POSTMORTEM(state)
    except Exception as exc:  # noqa: BLE001 - fail-closed boundary
        return {
            "postmortem_narrative": _fallback_narrative("Incident Postmortem", exc),
            "incident_events": state.get("incident_events", []),
            "fallbacks": [_fallback("draft_incident_postmortem", exc)],
        }


def _check_ops_incident_conditional():
    # 정상 Metrics -> Incident 없음, Timeline/LLM 둘 다 안 불림. Breach Metrics -> 실제
    # OpsAssessment.incident를 그대로 물려받아 FACT/INFERENCE가 남는지 확인한다.
    global _call_internal_llm
    orig = _call_internal_llm
    _call_internal_llm = lambda prompt: (
        "지연 원인은 market-api 응답 지연으로 추정됨(확정 아님)"
    )
    now = datetime.now(timezone.utc)
    healthy_metrics = {
        "scope": "research-department",
        "window_start": now.isoformat(),
        "window_end": now.isoformat(),
        "request_count": 1000,
        "error_count": 5,
        "p95_latency_ms": "800",
        "cost_usd": "2.5",
    }
    breach_metrics = {**healthy_metrics, "error_count": 150}
    thresholds = {
        "max_error_rate": "0.02",
        "critical_error_rate": "0.10",
        "max_p95_latency_ms": "2000",
        "critical_p95_latency_ms": "5000",
        "max_cost_usd_per_window": "10",
    }
    try:
        healthy = run_ops_incident_review(healthy_metrics, thresholds)
        assert healthy["ops_assessment"]["status"] == "healthy"
        assert healthy["ops_assessment"]["incident"] is None
        assert healthy["incident_events"] == []

        breached = run_ops_incident_review(breach_metrics, thresholds)
        assert breached["ops_assessment"]["incident"] is not None
        assert [e["entry_type"] for e in breached["incident_events"]] == [
            "FACT",
            "INFERENCE",
        ]
        assert breached["postmortem_narrative"]
    finally:
        _call_internal_llm = orig
    print("  Ops Health -> Incident 조건부 연결  OK")


# ── Tool Permission Security Review (tool-permission-security-reviewer) ─────
class ToolPermissionState(TypedDict, total=False):
    policy: dict  # AgentToolPolicy 필드 (agent_id/profile_version_id/allowed_tools)
    tool_name: str
    permission_check: dict  # {"result": "ALLOWED"|"DENIED", "reason": str}
    violation_narrative: str
    fallbacks: list[dict]


def check_tool_permission_node(state: ToolPermissionState) -> dict:
    from tool_permission_check import AgentToolPolicy, check_tool_permission

    p = state["policy"]
    policy = AgentToolPolicy(
        agent_id=UUID(p["agent_id"]),
        profile_version_id=UUID(p["profile_version_id"]),
        allowed_tools=frozenset(p["allowed_tools"]),
    )
    result = check_tool_permission(policy, state["tool_name"])
    return {
        "permission_check": {"result": result.result.value, "reason": result.reason}
    }


def _permission_denied(state: ToolPermissionState) -> bool:
    return (state.get("permission_check") or {}).get("result") == "DENIED"


def narrate_permission_violation(state: ToolPermissionState) -> dict:
    prompt = f"""{_persona("tool-permission-security-reviewer")}

아래는 결정론적 Tool Allowlist 판정 결과다(DENIED). 새 판정을 내리지 말고, 위반 사실과 왜
Escalation이 필요한지만 한국어 1-2문장으로 서술하라:
{json.dumps({"tool_name": state["tool_name"], **state["permission_check"]}, ensure_ascii=False, indent=1)}"""
    return {"violation_narrative": _call_internal_llm(prompt)}


def build_tool_permission_pipeline():
    g = StateGraph(ToolPermissionState)
    g.add_node("check_tool_permission_node", check_tool_permission_node)
    g.add_node("narrate_permission_violation", narrate_permission_violation)
    g.set_entry_point("check_tool_permission_node")
    g.add_conditional_edges(
        "check_tool_permission_node",
        _permission_denied,
        {True: "narrate_permission_violation", False: END},
    )
    g.add_edge("narrate_permission_violation", END)
    return g.compile()


def run_tool_permission_review(policy: dict, tool_name: str) -> dict:
    out = build_tool_permission_pipeline().invoke(
        {"policy": policy, "tool_name": tool_name}
    )
    return {
        "permission_check": out["permission_check"],
        "violation_narrative": out.get("violation_narrative"),
        "fallbacks": out.get("fallbacks", []),
    }


_RAW_CHECK_TOOL_PERMISSION_NODE = check_tool_permission_node
_RAW_NARRATE_PERMISSION_VIOLATION = narrate_permission_violation


def check_tool_permission_node(state: ToolPermissionState) -> dict:
    try:
        return _RAW_CHECK_TOOL_PERMISSION_NODE(state)
    except Exception as exc:  # noqa: BLE001 - fail-closed boundary (애매하면 거부, tool_permission_check.py와 동일 원칙)
        return {
            "permission_check": {
                "result": "DENIED",
                "reason": f"판정 파이프라인 실패로 안전하게 거부 ({type(exc).__name__})",
            },
            "fallbacks": [_fallback("check_tool_permission", exc)],
        }


def narrate_permission_violation(state: ToolPermissionState) -> dict:
    try:
        return _RAW_NARRATE_PERMISSION_VIOLATION(state)
    except Exception as exc:  # noqa: BLE001 - fail-closed boundary
        return {
            "violation_narrative": _fallback_narrative(
                "Tool Permission Security Reviewer", exc
            ),
            "fallbacks": [_fallback("narrate_permission_violation", exc)],
        }


def _check_tool_permission_conditional():
    global _call_internal_llm
    orig = _call_internal_llm
    _call_internal_llm = lambda prompt: "Allowlist 밖 Tool 호출 - 즉시 Escalation 필요"
    policy = {
        "agent_id": str(uuid4()),
        "profile_version_id": str(uuid4()),
        "allowed_tools": ["market-api"],
    }
    try:
        allowed = run_tool_permission_review(policy, "market-api")
        assert allowed["permission_check"]["result"] == "ALLOWED"
        assert allowed["violation_narrative"] is None

        denied = run_tool_permission_review(policy, "oms.submit")
        assert denied["permission_check"]["result"] == "DENIED"
        assert denied["violation_narrative"]
    finally:
        _call_internal_llm = orig
    print("  Tool Permission 조건부 연결        OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        if os.environ.get("RISK_QA_PRODUCTION_ENABLED", "false").lower() != "true":
            print(
                "AI-QA production pipeline is OFF. "
                "Run scripts/run_risk_qa_test_pipeline.py --mode test, "
                "or explicitly enable the reviewed production gate."
            )
            raise SystemExit(2)
        from uuid import uuid4

        now_iso = datetime.now(timezone.utc).isoformat()
        eid = str(uuid4())
        demo_artifact = {
            "artifact_version_id": str(uuid4()),
            "artifact_type": "research_packet",
            "producer": "research-supervisor",
            "fund_id": str(uuid4()),
            "trace_id": str(uuid4()),
            "claims": [
                {
                    "claim_index": 0,
                    "text": "AAPL 종가는 70000원",
                    "kind": "fact",
                    "subject": "AAPL",
                    "numeric_value": "70000",
                    "unit": "KRW",
                    "evidence_ids": [] if "--fail" in sys.argv else [eid],
                }
            ],
        }
        demo_evidence_store = {
            eid: {
                "source": "market-api",
                "published_at": now_iso,
                "observed_at": now_iso,
                "excerpt": "종가 70000원",
                "numeric_value": "70000",
                "unit": "KRW",
            }
        }
        print(
            f"{PIPELINE_VERSION} 실행 (데모 Artifact{' - FAIL 유도' if '--fail' in sys.argv else ''})"
        )
        log_path = Path(
            os.environ.get(
                "QA_RUN_LOG_PATH",
                _BASE
                / "reports"
                / "runs"
                / f"qa_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl",
            )
        )
        if "--log-path" in sys.argv:
            log_index = sys.argv.index("--log-path") + 1
            if log_index >= len(sys.argv):
                raise SystemExit("--log-path requires a file path")
            log_path = Path(sys.argv[log_index])
        out = run_qa_department(
            demo_artifact, demo_evidence_store, now_iso, log_path=log_path
        )
        print(json.dumps(out, ensure_ascii=False, indent=1))

        report_dir = _BASE / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"qa_audit_report_{out['qa_decision_id']}.md"
        report_path.write_text(
            _render_report_md(demo_artifact, now_iso, out), encoding="utf-8"
        )
        print(f"결정론적 MD 리포트 저장: {report_path}")
        raise SystemExit(0)

    print(f"{PIPELINE_VERSION} 자체 점검 (Ollama·Hermes·Agentic RAG 없음)")
    _check_graph_shape()
    _check_fail_still_narrates()
    _check_hallucination_review_conditional()
    _check_supervisor_guard()
    _check_notion_report_node()
    _check_ops_incident_conditional()
    _check_tool_permission_conditional()
    print(
        "본부 파이프라인 7개 영역 통과. 실행은 --run (내부 Ollama·Hermes·OPENAI_API_KEY 필요)"
    )
