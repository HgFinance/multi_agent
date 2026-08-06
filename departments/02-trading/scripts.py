#!/usr/bin/env python3
"""트레이딩본부 LangGraph 파이프라인 - run_bull_bear_debate(packet) -> Debate Bundle.

담당: 도현 (트레이딩본부) — 회계/포트폴리오본부는 별도 스크립트를 가진다. 이 파일에
      Ledger·Position·NAV 를 끌어오지 않는다(부서 경계, CLAUDE.md 권한 분리).
형식 근거: QA본부(departments/06-ai-qa-audit/scripts.py)와 리스크본부
      (departments/03-risk/scripts.py)의 파이프라인 형식 — 결정론 노드 + 서술만 하는 LLM,
      _guard_node fail-closed 경계, 순수 함수 MD 리포트, input_hash 재현성, 자체 점검.
      **형식만 가져왔고 내용은 우리 본부 것이다** — 저쪽의 evaluation 요약은 이미 있는 필드를
      다시 세는 것이라 넣지 않았다. LangSmith 핸드오프는 2026-08-03 에 넣되 저쪽과 달리
      부서 Project 로 격리했다(_ls_project). 기본은 여전히 꺼짐이다.
내용 근거: docs/04-organization/AGENT_EMPLOYEE_PROFILES.md TRD-01/TRD-02, 200행
      "동일 Evidence에서 찬반 논거를 독립 생성 | LangGraph Parallel Nodes".

범위 (2026-08-03 1차) - 직원 2명만:
  validate_packet   결정론 - 필수 필드, PIT 신선도, evidence_quality 게이트, Claim 색인
  bull_researcher   TRD-01 (LLM, hermes/config.yaml 페르소나 원문)  ┐ 병렬
  bear_researcher   TRD-02 (LLM, hermes/config.yaml 페르소나 원문)  ┘ 서로 못 본다
  debate_merge      결정론 - 인용 검증 / 독립성 측정 / 쟁점 대조
  propose_intent    결정론 - grounded 토론에서만 OrderIntent **제안**을 만든다 (2026-08-05)
  notion_report     Reporter (결정론 - notion_reporter.upload_debate, LLM 아님) - 결과를
                    Notion Trading DB(NOTION_TRADING_DB)에 Projection 으로 올린다.
                    업로드가 실패해도 grounded/escalate 는 못 바꾼다.

**토론 -> OrderIntent 제안까지 (2026-08-05 추가).** 토론 결과가 아무 데도 안 닿으면
근거만 쌓이고 주문이 안 나온다. 그래서 `propose_intent` 를 붙였는데, 붙이면서 지킨 선 넷:

  - **grounded 토론에서만 만든다.** 인용이 날조거나 Packet 이 stale 이면 제안이 없다.
  - **수량·가격을 토론이 정하지 않는다.** 수량은 Signal 의 target_weight 에서,
    지정가는 philosophies.yaml 프리셋에서 나온다(F11 intent_builder 그대로 재사용).
    토론이 target_weight 를 만들기 시작하면 근거 생성이 전략 결정으로 번진다.
  - **Risk Gate 가 선행한다.** 제안에는 `risk_decision_id` 가 없고 `submittable: False` 다.
    OMS 는 유효한 RiskDecision 없이 제출을 거부하므로 이 제안만으로는 주문이 못 된다
    (팀 가이드 4.3 "RISK_APPROVED 는 상태가 아니라 전제조건").
  - **입력이 없으면 제안이 없다.** Signal·시세·NAV·현재 수량은 다른 본부/회계에서 온다.
    없으면 추정하지 않고 사유를 기록한 채 제안을 비운다.

**Bull 과 Bear 는 병렬이며 서로의 출력을 입력으로 받지 않는다.** 근거:
  - TRD-01 "Bear 결과는 생성 전 보지 않는다", KPI "독립성 위반 0"
  - TRD-02 "Bull 결론을 정답으로 취급하지 않는다", KPI "Bull 문장 복제 0"
  - TRD-00 "Bull/Bear 를 독립 호출하고"

  hermes/config.yaml 의 두 페르소나는 2026-08-03 에 위 조직도 원문에 맞춰 교정했다(교정 전
  bear 프롬프트는 Bull thesis 를 입력으로 받는 순차 구조라 정반대였다 - 변경 이력은
  config.yaml 의 "Agent Profile 변경 이력" 주석). 프롬프트가 독립을 선언하는 것과 배선이
  실제로 독립인 것은 다르므로, 배선 쪽은 _check_bear_never_sees_bull 이 payload 를 직접
  본다 - 프롬프트를 믿지 않는다.

이 파이프라인은 **판정을 만들지 않는다.** verdict/quantity/side/order_type 을 만드는 노드가
없다 - 토론은 근거 생성이고, OrderIntent 는 trader-pm-agent(TRD-03), 승인은 리스크본부다
(TRD-01 금지사항 "수량·주문 유형을 결정하지 않는다", CLAUDE.md 권한 분리).

우리 본부라서 있는 것 (리스크·QA 원본에 대응물이 없다):
  - Claim 색인과 인용 검증: Research Packet 의 주장을 우리가 색인해 Bull/Bear 가 그 밖을
    인용하면 날조로 잡는다. 리서치 Packet 의 evidence_id 계약(RQ-01)이 확정되기 전까지
    남의 스키마를 추측하지 않고 받은 Packet 만 색인한다.
  - PIT 신선도 게이트: 오래된 Research 로 연 토론은 stale 로 표시하고 escalate 한다.
    마스터플랜 9.3, 개발 원칙 9번(위험한 기능은 실패 시 확대가 아니라 진입 차단 방향).
  - trade_case_id 관통: 부서 간 공통 키를 그대로 실어 나른다(config.yaml trading-supervisor
    "carry every leg on the same trade_case_id").
  - 쟁점 대조: 양측이 같은 Claim 을 두고 부딪혔는지, 아무도 안 건드린 Claim 이 뭔지.
    이게 TRD-03 이 읽을 실제 산출물이다.

원칙 (CLAUDE.md):
  - 인용 검증은 결정론적 Python 이다. LLM 은 논거 서술만 만든다.
  - 인용에 없는 Claim ID 가 하나라도 있으면 grounded=False -> escalate.
    통과한 것처럼 다음 단계로 넘기지 않는다 (skills/agentic-rag 와 같은 처리).
  - LLM 초안이 스키마를 어기거나 Hermes 가 죽어도 파이프라인을 죽이지 않고 fallback +
    escalate 로 떨어진다. 어느 쪽도 "통과"가 아니다.
  - _render_report_md 는 순수 함수다 - 반환값을 그대로 옮겨 적을 뿐 LLM 이 리포트 구조나
    내용을 창작하지 않는다.
  - run_agent(Hermes) import 는 _hermes_chat 안에서 한다(Lazy Import) - Hermes 는 프로젝트
    .venv 가 아니라 별도 설치 venv 에만 있어서, 최상단에서 부르면 자체 점검조차 못 돈다.

실행:
  python departments/02-trading/scripts.py          # 자체 점검 (Hermes 없이, 네트워크 없이)
  python departments/02-trading/scripts.py --run    # 실제 Hermes 호출 (아래 절차 필요)

  --run 은 .env 를 셸에 먼저 로드하고 Hermes venv 를 활성화해야 한다:
    set -a && source .env && set +a
    source ~/claude/bin/activate
    python departments/02-trading/scripts.py --run
  reports/trading_debate_<debate_id>.md 로 결정론적 MD 리포트도 저장한다.
"""
from __future__ import annotations

import hashlib
import json
import operator
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, TypedDict

_BASE = Path(__file__).resolve().parent
_REPO_ROOT = _BASE.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from langgraph.graph import END, StateGraph
from langsmith import tracing_context

PIPELINE_VERSION = "trading-debate-pipeline-v1"

# Claim 색인 접두사. Research Packet 의 어느 배열에서 온 주장인지 남긴다.
# ponytail: 리서치본부 Packet 이 자체 evidence_id 를 표면에 내면 그걸 쓴다. 지금은
# facts 문자열 안에 "news:3" 처럼 묻혀 있어서 우리가 받은 Packet 을 직접 색인한다 -
# 확정 안 된 RQ-01 계약을 추측해 만들지 않기 위해서다(실행현황 문서 6절 "임의 JSON 금지").
_CLAIM_FIELDS = (("fact", "facts"), ("interp", "interpretation"),
                 ("catalyst", "catalysts"), ("invalid", "invalidation"))


class DebateState(TypedDict, total=False):
    research_packet: dict
    claims: dict                 # {"fact:0": "원문", ...} - validate_packet 이 만든다
    debate_id: str               # input_hash 파생 - 같은 Packet 이면 같은 값
    input_hash: str              # 재현성 계약
    trace_id: str
    trade_case_id: str | None    # 부서 간 공통 키 - 있으면 그대로 실어 나른다
    pit: dict                    # Packet 신선도 판정
    debate_opened: bool
    bull: dict | None
    bear: dict | None
    contest_r1: dict             # 1라운드 쟁점 목록 - **Claim id 만, 문장 없음**
    bull_r2: dict | None         # 2라운드 보강 (서로 다른 키라 reducer 불필요)
    bear_r2: dict | None
    citations: dict              # 결정론 인용 검증
    independence: dict           # 결정론 독립성 측정
    contested: dict              # 양측이 같은 Claim 을 다뤘는지
    synthesis: dict              # 3라운드 결정론 종합 - verdict 를 만들지 않는다
    employee_workers: dict       # 직원 7명 자문 - binding: false
    grounded: bool
    escalate: bool
    intent_inputs: dict | None   # 호출자가 준 Signal/시세/NAV/현재수량 - 없으면 제안 없음
    order_intent_proposal: dict  # 결정론 - 제안이지 주문이 아니다
    notion_upload: dict          # Reporter 결과 - 실패해도 위 판정은 안 바뀐다
    report_markdown: str
    # 병렬 노드 둘이 같은 키에 쓰므로 reducer 가 없으면 LangGraph 가 InvalidUpdateError 를 낸다.
    fallbacks: Annotated[list[dict], operator.add]


class DebatePipelineNodeError(RuntimeError):
    """실패한 LangGraph 노드를 예외에 붙인다 - 원인은 그대로 둔다."""

    def __init__(self, node: str, cause: Exception) -> None:
        self.failed_node = node
        self.cause = cause
        super().__init__(str(cause))


def _guard_node(node: str, handler):
    def guarded(state: DebateState) -> dict:
        try:
            return handler(state)
        except DebatePipelineNodeError:
            raise
        except Exception as exc:
            raise DebatePipelineNodeError(node, exc) from exc

    guarded.__name__ = f"{node}_guarded"
    return guarded


def _sanitize(exc: Exception) -> str:
    """진단은 남기되 자격증명이 fallback 기록으로 새지 않게 한다. .env 에 NOTION_TOKEN 등
    실토큰이 있고 이 메시지는 MD 리포트까지 흘러간다."""
    message = " ".join(str(exc).split()) or "no_exception_message"
    message = re.sub(r"(?i)\b(?:rediss?|postgres(?:ql)?|https?)://[^\s]+", "[REDACTED]", message)
    message = re.sub(r"\b(?:sk|ntn|lsv2|ghp|xox[baprs])_[A-Za-z0-9._-]+\b", "[REDACTED]", message)
    return message[:240]


def _fallback(stage: str, exc: Exception, *, attempts: int = 1) -> dict:
    # safe_action 은 orchestration/workflows/investment-case.yaml 의 trading step(sequence 2)
    # failure_action 과 같은 값이어야 한다(HOLD). 여기서 다른 말을 쓰면 Orchestrator 가 선언한
    # 안전 기본값과 코드가 어긋난다. 루트 multi-agent-workflow.yaml 은 호환 진입점일 뿐이고
    # 정본은 orchestration/workflows/ 다(2026-08-03 main 에서 분할됨).
    return {"stage": stage, "error": type(exc).__name__, "error_message": _sanitize(exc),
            "attempts": attempts, "safe_action": "HOLD", "decision_origin": "FALLBACK"}


def _input_hash(packet: dict) -> str:
    payload = json.dumps(packet, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


_CONFIG: dict | None = None


def _config() -> dict:
    """hermes/config.yaml 을 한 번만 읽는다. 튜닝 숫자와 model 은 코드가 아니라 여기 있다."""
    global _CONFIG
    if _CONFIG is None:
        import yaml

        _CONFIG = yaml.safe_load((_BASE / "hermes" / "config.yaml").read_text(encoding="utf-8"))
    return _CONFIG


def _max_age_minutes() -> int:
    return int((_config().get("config") or {}).get("packet_max_age_minutes", 120))


def _max_attempts() -> int:
    """orchestration/workflows/investment-case.yaml trading step 의 retry.max_attempts 와 같은 값(3).
    F08 완료 조건
    "Schema 실패는 재시도 후 PASS 처리한다" - 한 번 실패했다고 바로 포기하지 않는다."""
    return 3


# ── LangSmith 관측성 ──────────────────────────────────────────────────────
# 기본은 꺼져 있다 (LANGSMITH_TRACING=false). 켜면 이 그래프의 노드·LLM 호출이
# 외부(LangSmith)로 나간다 - 토론 Trace 에는 미공개 Research Packet 과 종목·논거가
# 그대로 담기므로 금융 데이터 외부 전송 정책 검토 전까지 로컬 개발에서만 켠다.
def _ls_project() -> str:
    """부서별 Project 로 격리한다. 한 Project 에 8개 부서를 섞으면 트레이딩 Trace 를
    다른 본부가 그대로 열람하게 된다 - 부서 경계가 관측성에서만 무너진다."""
    return f"{os.environ.get('LANGSMITH_PROJECT') or 'hedgefund'}-02-trading"


def _langsmith_handoff(trace_id: str) -> dict[str, Any]:
    """리포트·소비자에게 넘기는 것은 Trace 원문이 아니라 이 좌표뿐이다."""
    flag = os.environ.get("LANGCHAIN_TRACING_V2", os.environ.get("LANGSMITH_TRACING", ""))
    enabled = flag.casefold() in {"1", "true", "yes", "on"}
    return {
        "trace_id": str(trace_id),
        "langsmith": {
            "enabled": enabled,
            "project": _ls_project() if enabled else None,
            "run_id": os.environ.get("LANGSMITH_RUN_ID"),
            "handoff_status": "configured" if enabled else "not_configured",
        },
    }


def _model_version() -> str:
    """F08 완료 조건 "Model, Prompt, 입력 Snapshot과 결과를 기록한다" 중 Model."""
    return str((_config().get("model") or {}).get("default", "unknown"))


def _prompt_version(persona_name: str) -> str:
    """같은 조건 중 Prompt. 페르소나 본문 해시라서 프롬프트를 한 글자만 고쳐도 값이 바뀐다 -
    Agent Profile Version 변경(agent_evolution_cycle)이 기록에서 드러난다."""
    return f"{persona_name}@{hashlib.sha256(_persona(persona_name).encode()).hexdigest()[:12]}"


def _check_freshness(packet: dict, *, now: datetime | None = None) -> dict:
    """Point-in-Time 신선도 (마스터플랜 9.3, 개발 원칙 9번).

    ponytail: as_of 가 Research Packet 의 확정 필드가 되면(RQ-01) UNKNOWN 을 hard block 으로
    올린다. 지금 리서치 Packet 스키마는 as_of 를 표면에 내지 않아서(내부 universe 블록에만
    있다) UNKNOWN 을 막으면 모든 토론이 차단된다 - 표시하고 escalate 만 한다.
    """
    raw = packet.get("as_of") or (packet.get("universe") or {}).get("as_of")
    if not raw:
        return {"status": "UNKNOWN", "as_of": None, "age_minutes": None,
                "reason": "Packet 에 as_of 가 없다 - 신선도를 확인할 수 없다"}
    try:
        as_of = datetime.fromisoformat(str(raw))
    except ValueError:
        return {"status": "UNKNOWN", "as_of": str(raw), "age_minutes": None,
                "reason": "as_of 를 ISO8601 로 읽을 수 없다"}
    if as_of.tzinfo is None:
        # naive 를 UTC 로 가정하면 KST 와 9시간이 어긋난다. 추측하지 않는다.
        return {"status": "UNKNOWN", "as_of": str(raw), "age_minutes": None,
                "reason": "as_of 에 timezone 이 없다 - 시각을 추측하지 않는다"}
    limit = _max_age_minutes()
    age = (now or datetime.now(timezone.utc)) - as_of
    minutes = round(age.total_seconds() / 60, 1)
    if minutes < 0:
        # 미래 시각 Packet 은 PIT 위반이다 (개발 원칙 5번 - 미래 데이터 유입 금지).
        return {"status": "FUTURE", "as_of": as_of.isoformat(), "age_minutes": minutes,
                "reason": f"as_of 가 현재보다 미래다 - PIT 위반, 한도 {limit}분"}
    status = "STALE" if minutes > limit else "FRESH"
    return {"status": status, "as_of": as_of.isoformat(), "age_minutes": minutes,
            "reason": f"신선도 한도 {limit}분 대비 {minutes}분 경과"}


# ── 노드 1: Packet 검증, PIT, Claim 색인 (결정론) ──────────────────────────
def validate_packet(state: DebateState) -> dict:
    packet = state.get("research_packet") or {}
    ihash = _input_hash(packet)
    base = {"input_hash": ihash, "debate_id": f"dbt-{ihash[:16]}",
            "trace_id": str(packet.get("trace_id") or f"{PIPELINE_VERSION}:{ihash[:16]}"),
            "trade_case_id": packet.get("trade_case_id")}

    missing = [k for k in ("symbol", "thesis", "evidence_quality") if not packet.get(k)]
    if missing:
        raise ValueError(f"Research Packet 필수 필드 누락: {missing}")

    claims: dict[str, str] = {}
    for prefix, field in _CLAIM_FIELDS:
        for i, line in enumerate(packet.get(field) or []):
            claims[f"{prefix}:{i}"] = str(line)
    base["claims"] = claims
    base["pit"] = pit = _check_freshness(packet)

    quality = str(packet["evidence_quality"]).lower().strip()
    # 근거가 부족하거나 미래 시각인 Packet 으로 토론을 열면 양측 다 지어낸다. 열지 않는다.
    blocked = None
    if quality == "insufficient_evidence":
        blocked = ("InsufficientEvidence", "evidence_quality 가 insufficient_evidence 다")
    elif not claims:
        blocked = ("InsufficientEvidence", "인용 가능한 Claim 이 하나도 없다")
    elif pit["status"] == "FUTURE":
        blocked = ("PointInTimeViolation", pit["reason"])
    if blocked:
        return {**base, "debate_opened": False, "grounded": False, "escalate": True,
                "fallbacks": [{"stage": "validate_packet", "error": blocked[0],
                               "error_message": f"토론을 열지 않았다 - {blocked[1]}",
                               "attempts": 0, "safe_action": "HOLD",
                               "decision_origin": "DETERMINISTIC_GATE"}]}
    return {**base, "debate_opened": True}


# ── 노드 2·3: Bull / Bear (LLM - 병렬, 서로의 출력을 받지 않는다) ──────────
def _persona(name: str) -> str:
    cfg = (_BASE / "hermes" / "config.yaml").read_text(encoding="utf-8")
    m = re.search(rf'{re.escape(name)}: "(.*?)"\n', cfg, re.DOTALL)
    if not m:
        raise ValueError(f"{name} 페르소나를 config.yaml 에서 찾을 수 없다")
    return m.group(1)


def _hermes_chat(persona: str, task: str) -> str:
    from run_agent import (
        AIAgent,  # Lazy Import - Hermes 없는 환경에서도 모듈 import 는 항상 되어야 한다
    )

    # enabled_toolsets=[] 는 도구를 0개로 만든다(기본값 None 이면 33개, 실측 2026-08-03).
    # **이게 없으면 분석가가 파일을 쓴다.** 첫 --run 에서 bull-researcher 가 답변을 그냥
    # 반환하지 않고 departments/04-research/bull_report_005930.json 을 만들었다 - 존재하지도
    # 않는 남의 본부 경로다(실제로는 01-research / 04-quant-backtest). 두 가지가 동시에 깨진다:
    #   1. 부서 경계 - 트레이딩 직원이 다른 본부 디렉터리에 쓴다 (CLAUDE.md 권한 분리)
    #   2. 산출물 경로 - 결과는 이 파이프라인의 반환값과 Notion 이지, 에이전트가 임의로
    #      정한 파일이 아니다. 감사 추적 밖에서 파일이 생긴다.
    # 이 두 페르소나는 JSON 을 반환하기만 하면 되므로 도구가 하나도 필요 없다.
    agent = AIAgent(model=_model_version(), quiet_mode=True,
                    ephemeral_system_prompt=persona, enabled_toolsets=[])
    return agent.chat(task)


def _substrate(state: DebateState) -> str:
    """Bull 과 Bear 가 **똑같이** 받는 근거면. 여기 이외의 입력은 어느 쪽에도 주지 않는다."""
    packet = state["research_packet"]
    return json.dumps({
        "symbol": packet.get("symbol"),
        "thesis": packet.get("thesis"),
        "evidence_quality": packet.get("evidence_quality"),
        "claims": state["claims"],
    }, ensure_ascii=False, indent=1)


def _parse_json_block(out: str, required: tuple[str, ...], who: str) -> dict:
    s, e = out.find("{"), out.rfind("}")
    if s < 0 or e <= s:
        raise ValueError(f"{who} 응답에 JSON 이 없다 - 초안 거부")
    note = json.loads(out[s:e + 1])
    for k in required:
        if k not in note:
            raise ValueError(f"{who} 결과에 {k} 가 없다 - 초안 거부")
    if not isinstance(note.get("claim_refs"), list):
        raise TypeError(f"{who} 의 claim_refs 가 배열이 아니다 - 초안 거부")
    return note


_BULL_KEYS = ("bull_case", "upside_scenario", "catalyst_timeline", "bull_invalidation", "claim_refs")
_BEAR_KEYS = ("bear_case", "failure_mode", "downside_scenario", "missing_evidence", "claim_refs")


def _bull_task(state: DebateState) -> str:
    return f"""Using ONLY the claims below, build the strongest evidence-backed bullish case.
Every assertion must cite the claim ids you used (e.g. "fact:0"). Do not invent claim ids.
You will NOT see the Bear Researcher's output — do not speculate about it.
Do not decide quantity, side or order type; that is not your role.
Schema (JSON only):
{{"bull_case": "2-4 sentences in Korean",
 "upside_scenario": "1-2 sentences in Korean",
 "catalyst_timeline": ["upcoming checkpoints, Korean"],
 "bull_invalidation": ["conditions that would kill the bull case, Korean"],
 "claim_refs": ["claim ids actually used"]}}

Claims:
{_substrate(state)}"""


def _bear_task(state: DebateState) -> str:
    return f"""Using ONLY the claims below, build the independent bear case: failure modes,
downside scenarios with an explicit trigger, and evidence the Packet does not establish.
Every assertion must cite the claim ids you used (e.g. "fact:0"). Do not invent claim ids.
Record insufficient evidence itself as a separate objection.
Do not decide quantity, side or order type; that is not your role.
Schema (JSON only):
{{"bear_case": "2-4 sentences in Korean",
 "failure_mode": ["how the thesis breaks, Korean"],
 "downside_scenario": "1-2 sentences in Korean, include the trigger",
 "missing_evidence": ["what the packet does not establish, Korean"],
 "claim_refs": ["claim ids actually used"]}}

Claims:
{_substrate(state)}"""


def _researcher(state: DebateState, *, key: str, persona: str, build_task,
                required: tuple[str, ...], who: str, chat) -> dict:
    if not state.get("debate_opened"):
        return {}
    call = chat or _hermes_chat
    persona_text, task = _persona(persona), build_task(state)
    repair, last = "", None
    for attempt in range(1, _max_attempts() + 1):
        try:
            note = _parse_json_block(call(persona_text, task + repair), required, who)
            note["attempts"] = attempt
            return {key: note}
        except Exception as exc:  # noqa: BLE001 - intentional fallback boundary
            last = exc
            # 일반 문구만으로는 같은 실수를 반복한다 - 무엇이 거부됐는지 알려준다(리서치본부 실측).
            repair = (f"\n\nYour previous reply was rejected ({_sanitize(exc)}). "
                      f"Return ONLY the JSON object described above, with every required key "
                      f"({', '.join(required)}) present and claim_refs as a JSON array.")
    # 재시도를 다 쓰고도 실패하면 파이프라인을 죽이지 않고 fallback 으로 남긴다 - debate_merge 가
    # grounded=False / escalate=True 로 떨어뜨린다 (F08 "재시도 후 PASS", workflow on_failure HOLD).
    return {key: None,
            "fallbacks": [_fallback(f"{key}_researcher", last, attempts=_max_attempts())]}


def bull_researcher(state: DebateState, *, chat=None) -> dict:
    return _researcher(state, key="bull", persona="bull-researcher", build_task=_bull_task,
                       required=_BULL_KEYS, who="Bull", chat=chat)


def bear_researcher(state: DebateState, *, chat=None) -> dict:
    return _researcher(state, key="bear", persona="bear-researcher", build_task=_bear_task,
                       required=_BEAR_KEYS, who="Bear", chat=chat)


# ── 노드 4: 2라운드 보강 (LLM - 병렬, **Claim id 목록만** 받는다) ──────────
_REBUTTAL_KEYS = ("addressed", "still_unaddressed", "added_case", "claim_refs")


def _rebuttal_task(state: DebateState, *, side: str) -> str:
    """2라운드 프롬프트. **상대 문장이 한 글자도 안 들어간다.**

    넘기는 것은 Claim id 목록뿐이다. 상대 원문을 주면 먼저 말한 쪽이 앵커가 되어
    확증편향이 생기고, 그것이 두 직원을 나눈 이유를 무효로 만든다(ADR-0005).
    그래서 요구도 "상대를 반박하라"가 아니라 "네가 아직 안 다룬 Claim 을 다뤄라"다.
    """
    contest = state.get("contest_r1") or {}
    opponent = "bear" if side == "bull" else "bull"
    mine = contest.get(f"{side}_only_refs") or []
    theirs = contest.get(f"{opponent}_only_refs") or []
    untouched = contest.get("untouched_refs") or []
    claims = state.get("claims", {})
    stance = "bullish" if side == "bull" else "bearish"
    return f"""Round 2 of an independent debate. You already produced your Round 1 case.

You are NOT shown the other side's text and never will be — only which claim ids each
side cited. Do not guess, reconstruct or quote the opposing argument.

Claims the opposing side cited but you did not: {theirs or "none"}
Claims only you cited: {mine or "none"}
Claims neither side has addressed: {untouched or "none"}

Task: for the claims you have not yet addressed, state what they mean **from your own
{stance} position**. If a claim genuinely does not change your case, say so and put it in
still_unaddressed with the reason. Do not decide quantity, side or order type.

Schema (JSON only):
{{"addressed": ["claim ids you now address"],
 "still_unaddressed": ["claim ids you deliberately leave, Korean reason after a colon"],
 "added_case": "2-3 sentences in Korean - what your position adds after these claims",
 "claim_refs": ["every claim id used in this reply"]}}

Claim index:
{json.dumps(claims, ensure_ascii=False, indent=1)}"""


def _bull_rebuttal_task(state: DebateState) -> str:
    return _rebuttal_task(state, side="bull")


def _bear_rebuttal_task(state: DebateState) -> str:
    return _rebuttal_task(state, side="bear")


def _rebuttal(state: DebateState, *, side: str, chat) -> dict:
    """2라운드 실행. **R1 산출이 없으면 아예 돌지 않는다.**

    `_researcher` 의 게이트는 `debate_opened` 하나뿐이라 R2 조건을 모른다. 실패한
    1라운드 위에 LLM 을 두 번 더 태울 이유가 없으므로 여기서 먼저 막는다.
    """
    if not (state.get("bull") and state.get("bear")):
        return {}
    persona = f"{side}-researcher"
    build = _bull_rebuttal_task if side == "bull" else _bear_rebuttal_task
    return _researcher(state, key=f"{side}_r2", persona=persona, build_task=build,
                       required=_REBUTTAL_KEYS, who=f"{side.capitalize()} R2", chat=chat)


def bull_rebuttal(state: DebateState, *, chat=None) -> dict:
    return _rebuttal(state, side="bull", chat=chat)


def bear_rebuttal(state: DebateState, *, chat=None) -> dict:
    return _rebuttal(state, side="bear", chat=chat)


# ── 노드 4: 대조 (결정론 - 판정 아님) ──────────────────────────────────────
# 서술이 아닌 필드. **이름으로 명시한다** - 2라운드의 addressed/still_unaddressed 는
# Claim id 목록이라 문장으로 세면 독립성 수치가 오염된다. 길이 필터(>12자)가 우연히
# 걸러주긴 하지만(fact:0=6자, catalyst:12=11자) 우연에 기대지 않는다.
_REF_FIELDS = frozenset({"claim_refs", "addressed", "still_unaddressed"})


def _sentences(obj) -> set[str]:
    """서술 필드를 문장 단위로 정규화. 독립성(Bull 문장 복제 0) 측정용.

    메타 필드(attempts 같은 것)는 이름이 아니라 **타입으로** 거른다 - 이름으로 거르면
    새 메타 필드가 늘 때마다 여기서 터진다(실측: attempts 추가 때 터졌다).
    id 목록 필드만 _REF_FIELDS 로 이름 지정해 뺀다."""
    parts: list[str] = []
    for key, value in (obj or {}).items():
        if key in _REF_FIELDS:
            continue
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (list, tuple)):
            parts += [str(v) for v in value]
    text = " ".join(parts)
    return {s for s in (" ".join(p.split()) for p in re.split(r"[.!?。\n]", text)) if len(s) > 12}


def _contest(claims: dict, bull_refs, bear_refs) -> dict:
    """쟁점 집합 대수. `contest_round1` 과 `debate_merge` 가 **공유한다**.

    복붙하면 한쪽만 고쳐질 자리다. 색인 밖 인용은 여기서 조용히 빠진다 -
    날조 판정은 citations 가 따로 한다.
    """
    b = set(bull_refs) & set(claims)
    r = set(bear_refs) & set(claims)
    return {"contested_refs": sorted(b & r), "bull_only_refs": sorted(b - r),
            "bear_only_refs": sorted(r - b),
            "untouched_refs": sorted(set(claims) - b - r)}


def _refs_of(*notes) -> list[str]:
    """여러 라운드 note 의 claim_refs 합집합(순서 보존)."""
    seen: list[str] = []
    for note in notes:
        for ref in (note or {}).get("claim_refs", []):
            if ref not in seen:
                seen.append(ref)
    return seen


# ── 노드 3: 1라운드 쟁점 대조 (결정론 - 문장을 만들지 않는다) ──────────────
def contest_round1(state: DebateState) -> dict:
    """1라운드 인용을 쟁점 목록으로 바꾼다. **Claim id 만 만들고 문장은 안 만든다.**

    2라운드가 받을 유일한 입력이다. 여기서 상대 문장을 한 글자라도 실으면 앵커링이
    생기고 두 직원을 나눈 의미가 사라진다(ADR-0005).
    """
    claims = state.get("claims", {})
    bull_refs = list((state.get("bull") or {}).get("claim_refs", []))
    bear_refs = list((state.get("bear") or {}).get("claim_refs", []))
    return {"contest_r1": {**_contest(claims, bull_refs, bear_refs),
                           "round": 1, "decided_by": "deterministic"}}


def debate_merge(state: DebateState) -> dict:
    """두 라운드 합산 대조. 인용·독립성·쟁점을 라운드별로 나눠 본다."""
    claims = state.get("claims", {})
    bull, bear = state.get("bull"), state.get("bear")
    bull_r2, bear_r2 = state.get("bull_r2"), state.get("bear_r2")

    bull_refs = _refs_of(bull, bull_r2)
    bear_refs = _refs_of(bear, bear_r2)

    # 인용 검증 - 색인에 없는 Claim ID 는 날조다. LLM 이 아니라 여기서 잡는다.
    unknown = sorted({r for r in bull_refs + bear_refs if r not in claims})
    citations = {"bull_refs": bull_refs, "bear_refs": bear_refs, "unknown_refs": unknown,
                 "bull_uncited": bool(bull) and not bull_refs,
                 "bear_uncited": bool(bear) and not bear_refs,
                 "by_round": {
                     "r1": {"bull": _refs_of(bull), "bear": _refs_of(bear)},
                     "r2": {"bull": _refs_of(bull_r2), "bear": _refs_of(bear_r2)}}}

    # 독립성 - 두 라운드 다 병렬이라 원래 0 이어야 한다. 0 이 아니면 배선이 샌 것이다.
    r1_shared = _sentences(bull) & _sentences(bear)
    r2_shared = _sentences(bull_r2) & _sentences(bear_r2)
    # 라운드를 넘어선 복제도 본다 - 2라운드가 상대의 1라운드 문장을 베끼면 그것도 위반이다.
    cross = (_sentences(bull_r2) & _sentences(bear)) | (_sentences(bear_r2) & _sentences(bull))
    shared = r1_shared | r2_shared | cross
    independence = {"duplicated_sentences": sorted(shared), "violations": len(shared),
                    "by_round": {"r1": len(r1_shared), "r2": len(r2_shared),
                                 "cross_round": len(cross)}}

    contested = {**_contest(claims, bull_refs, bear_refs), "round": 2}

    stale = (state.get("pit") or {}).get("status") in {"STALE", "FUTURE"}
    grounded = bool(
        state.get("debate_opened") and bull and bear and not unknown
        and not citations["bull_uncited"] and not citations["bear_uncited"]
        and not independence["violations"] and not stale
    )
    return {"citations": citations, "independence": independence, "contested": contested,
            "grounded": grounded, "escalate": not grounded or bool(state.get("fallbacks"))}


# ── 노드 5: 3라운드 종합 (결정론 - verdict 를 만들지 않는다) ───────────────
def synthesize(state: DebateState) -> dict:
    """토론의 결론을 결정론으로 종합한다.

    **verdict·수량·방향을 만들지 않는다.** 어느 쟁점이 해소되고 무엇이 미해결인지,
    근거가 얼마나 덮였는지, 인용과 독립성이 지켜졌는지를 셈할 뿐이다. 방향과 수량은
    여전히 Signal 의 target_weight 와 리스크본부가 정한다(ADR-0005 "지키는 경계").
    """
    claims = state.get("claims", {})
    r1 = state.get("contest_r1") or {}
    final = state.get("contested") or {}
    citations = state.get("citations") or {}
    independence = state.get("independence") or {}

    covered = set(claims) - set(final.get("untouched_refs") or [])
    # 1라운드에 한쪽만 인용했던 Claim 중 2라운드에서 양측이 다루게 된 것 = 해소됨.
    r1_one_sided = set(r1.get("bull_only_refs") or []) | set(r1.get("bear_only_refs") or [])
    resolved = sorted(r1_one_sided & set(final.get("contested_refs") or []))
    unresolved = sorted(r1_one_sided - set(resolved))

    by_round = citations.get("by_round") or {}
    discipline = {"unknown_refs": list(citations.get("unknown_refs") or []),
                  "bull_uncited": bool(citations.get("bull_uncited")),
                  "bear_uncited": bool(citations.get("bear_uncited")),
                  "clean": not citations.get("unknown_refs")
                  and not citations.get("bull_uncited")
                  and not citations.get("bear_uncited")}

    return {"synthesis": {
        "round": 3,
        "evidence_coverage": round(len(covered) / len(claims), 4) if claims else 0.0,
        "covered_claims": sorted(covered),
        "unaddressed_claims": sorted(final.get("untouched_refs") or []),
        "resolved_issues": resolved,
        "unresolved_issues": unresolved,
        "still_contested": sorted(final.get("contested_refs") or []),
        "citation_discipline": discipline,
        "independence": dict(independence.get("by_round") or {}),
        "rounds_completed": 2 if (state.get("bull_r2") or state.get("bear_r2")) else 1,
        "grounded": bool(state.get("grounded")),
        "refs_by_round": by_round,
        # 이 종합은 판정이 아니다 - 소비자가 착각하지 않게 계약으로 박는다.
        "decided_by": "deterministic",
        "authoritative": False,
        "produces_verdict": False,
    }}


# ── 노드 5: OrderIntent 제안 (결정론 - 주문이 아니다) ──────────────────────
def _no_proposal(reason: str, detail: str) -> dict:
    return {"order_intent_proposal": {
        "available": False, "reason": reason, "detail": detail,
        "risk_gate_required": True, "submittable": False}}


def propose_intent(state: DebateState) -> dict:
    """grounded 토론 + 외부 입력이 다 있을 때만 OrderIntent 제안을 만든다.

    Packet 접수 게이트(packet_gate)와 F11(intent_builder)을 그대로 재사용한다 - 여기서
    수량·가격 계산을 다시 쓰면 같은 규칙이 두 곳에 생기고 한쪽만 바뀐다.
    """
    if not state.get("grounded"):
        return _no_proposal("not_grounded",
                            "인용·독립성·신선도 검증을 통과하지 못한 토론으로는 주문을 제안하지 않는다")
    inputs = state.get("intent_inputs") or {}
    missing = [k for k in ("packet", "signal", "snapshot", "nav", "current_quantity")
               if inputs.get(k) is None]
    if missing:
        return _no_proposal(
            "inputs_missing",
            f"제안에 필요한 입력이 없다: {missing}. Signal 은 strategy-registry-api, 시세는 "
            "market-api, NAV·보유수량은 회계본부에서 온다 - 추정하지 않는다")

    # Lazy Import - contracts 계층은 pydantic 등 의존이 있고, 제안을 안 만드는 실행에서는
    # 불러올 이유가 없다(_hermes_chat 과 같은 이유).
    for sub in ("contracts", "oms"):
        path = str(_BASE / sub)
        if path not in sys.path:
            sys.path.insert(0, path)
    from intent_builder import IntentBuildError, build_order_intent, load_presets
    from packet_gate import PacketGateError, check_packet_admissible

    # 토론이 읽은 Packet 과 게이트가 검사할 Packet 이 같은 것인지 확인한다. 다르면 A 의 근거
    # 위에 B 의 주문이 올라탄다 - packet_gate 가 Case 동일성을 보는 것과 같은 이유다.
    debated_id = (state.get("research_packet") or {}).get("packet_id")
    gate_id = getattr(inputs["packet"], "packet_id", None)
    if debated_id and gate_id and str(debated_id) != str(gate_id):
        return _no_proposal(
            "packet_mismatch",
            f"토론한 Packet({debated_id})과 제안 입력 Packet({gate_id})이 다르다")

    signal, snapshot = inputs["signal"], inputs["snapshot"]
    try:
        packet_ref = check_packet_admissible(inputs["packet"], signal, snapshot)
        preset = load_presets()[signal.philosophy]
        intent = build_order_intent(
            signal, snapshot=snapshot, nav=inputs["nav"],
            current_quantity=inputs["current_quantity"], preset=preset,
            trade_case_id=inputs.get("trade_case_id") or signal.strategy_id,
            now=inputs.get("now") or datetime.now(timezone.utc))
    except (PacketGateError, IntentBuildError, KeyError) as exc:
        # 게이트에 막힌 것은 파이프라인 장애가 아니다 - 제안이 없을 뿐이고 사유가 남는다.
        return _no_proposal(type(exc).__name__, _sanitize(exc))

    if intent is None:
        return _no_proposal("no_delta", "목표 비중에 이미 도달해 만들 주문이 없다")

    return {"order_intent_proposal": {
        "available": True,
        "order_intent": intent.model_dump(mode="json"),
        "packet_ref": {"packet_id": packet_ref.packet_id, "case_id": packet_ref.case_id,
                       "status": packet_ref.status,
                       "as_known_at": packet_ref.as_known_at.isoformat()},
        # **이 셋이 이 제안의 요지다.** 제안은 주문 권한이 아니다.
        "risk_gate_required": True,
        "risk_decision_id": None,
        "submittable": False,
        "next_step": ("risk-api 판정 -> contracts/risk_gate.to_risk_decision -> "
                      "oms.apply_risk_decision -> oms.create_broker_order"),
    }}


# ── 노드 7: 직원 7명 자문 (Worker Registry - binding: false) ───────────────
def employee_workers(state: DebateState, *, run=None) -> dict:
    """부서 직원 레지스트리를 돌려 자문 맥락을 얻는다.

    방향은 **scripts.py -> employee_workers.py 단방향**이다. 반대로 직원 모듈이 이
    파일을 import 하면 langgraph·langsmith·notion_reporter 가 직원 레지스트리에
    끌려 들어오고 순환이 생긴다(리스크본부 scripts.py 와 같은 방향).

    `propose_intent` 뒤에 있어야 `order_intent_proposal` 로 execution_request 가 켜져
    venue-cost-worker 가 돈다. **bull/bear 원문은 payload 에 넣지 않는다** - 직원
    계층에서도 상대 논지 복제를 막는다(ADR-0005).
    """
    runner = run
    if runner is None:
        from employee_workers import run_employee_workers  # Lazy Import (_BASE 는 sys.path 에 있다)

        runner = run_employee_workers

    payload = {
        "research_packet": state.get("research_packet") or {},
        "market_snapshot": (state.get("intent_inputs") or {}).get("snapshot"),
        "trade_case_id": state.get("trade_case_id"),
        "debate": {
            "claims": state.get("claims", {}),
            "citations": state.get("citations", {}),
            "contested": state.get("contested", {}),
            "independence": state.get("independence", {}),
            "synthesis": state.get("synthesis", {}),
            "grounded": bool(state.get("grounded")),
            "order_intent_proposal": state.get("order_intent_proposal", {}),
            # bull / bear 원문은 의도적으로 없다.
        },
    }
    try:
        return {"employee_workers": runner(payload)}
    except Exception as exc:  # noqa: BLE001 - intentional fallback boundary
        # 직원 자문 실패가 토론 판정을 못 바꾼다. grounded 는 이미 앞에서 확정됐다.
        return {"employee_workers": {"binding": False, "degraded": True,
                                     "error": type(exc).__name__,
                                     "detail": _sanitize(exc)},
                "fallbacks": [_fallback("employee_workers", exc)]}


# ── 그래프 조립 ────────────────────────────────────────────────────────────
def _route_after_validate(state: DebateState):
    # 토론을 열면 Bull/Bear 를 같은 superstep 에 병렬로 띄운다(1라운드).
    if state.get("debate_opened"):
        return ["bull_researcher", "bear_researcher"]
    return "debate_merge"


def _route_after_contest(state: DebateState):
    """2라운드 진입. **1라운드가 실패했으면 건너뛴다.**

    한쪽이라도 초안을 못 냈으면 쟁점 목록이 반쪽이라 2라운드가 의미가 없고, 실패한
    토론에 LLM 을 두 번 더 태울 이유도 없다. 바로 debate_merge 로 가서 grounded=False
    로 떨어진다.
    """
    if state.get("bull") and state.get("bear"):
        return ["bull_rebuttal", "bear_rebuttal"]
    return "debate_merge"


# ── 노드 5: Notion 업로드 (Reporter - 결정론, LLM 아님) ────────────────────
def notion_report(state: DebateState, *, uploader=None) -> dict:
    """최종 결과를 Notion Trading DB 에 Projection 으로 올린다.

    **업로드 실패는 토론 결과를 못 바꾼다.** grounded/escalate 는 이 노드 앞에서 이미
    확정됐고 여기서는 읽기만 한다 - notion_upload 필드에 성공/실패만 기록한다.
    """
    from notion_reporter import upload_debate

    out = _assemble_out(state, state.get("research_packet") or {})
    report_md = _render_report_md(out)
    upload = uploader or upload_debate
    try:
        result = upload(out, report_md=report_md)
    except Exception as exc:   # reporter 는 원래 예외를 안 던지지만, 던져도 여기서 멈춘다  # noqa: BLE001 - intentional fallback boundary
        result = {"ok": False, "reason": f"Reporter 예외: {type(exc).__name__}"}
    return {"notion_upload": result, "report_markdown": report_md}


def build_pipeline(chat=None, uploader=None, workers=None):
    """chat / uploader / workers 를 주입받아 자체 점검이 전역 함수를 바꿔치기하지 않아도
    되게 한다 (리스크본부는 global 스왑을 쓴다 - 같은 결과인데 되돌리기 코드가 길어져서
    여기선 주입). 자체 점검이 Hermes·Notion·Ollama 유무에 의존하지 않게 하는 효과도 있다.

    3라운드 배선:
      R1  bull_researcher ∥ bear_researcher   같은 Claim 색인, 서로 못 봄
      R2  bull_rebuttal   ∥ bear_rebuttal     Claim id 목록만 받는다
      R3  synthesize                          결정론 종합 (verdict 없음)
    """
    g = StateGraph(DebateState)
    g.add_node("validate_packet", _guard_node("validate_packet", validate_packet))
    g.add_node("bull_researcher",
               _guard_node("bull_researcher", lambda s: bull_researcher(s, chat=chat)))
    g.add_node("bear_researcher",
               _guard_node("bear_researcher", lambda s: bear_researcher(s, chat=chat)))
    g.add_node("contest_round1", _guard_node("contest_round1", contest_round1))
    g.add_node("bull_rebuttal",
               _guard_node("bull_rebuttal", lambda s: bull_rebuttal(s, chat=chat)))
    g.add_node("bear_rebuttal",
               _guard_node("bear_rebuttal", lambda s: bear_rebuttal(s, chat=chat)))
    g.add_node("debate_merge", _guard_node("debate_merge", debate_merge))
    g.add_node("synthesize", _guard_node("synthesize", synthesize))
    g.add_node("propose_intent", _guard_node("propose_intent", propose_intent))
    g.add_node("employee_workers",
               _guard_node("employee_workers", lambda s: employee_workers(s, run=workers)))
    g.add_node("notion_report",
               _guard_node("notion_report", lambda s: notion_report(s, uploader=uploader)))
    g.set_entry_point("validate_packet")
    g.add_conditional_edges("validate_packet", _route_after_validate)
    g.add_edge("bull_researcher", "contest_round1")
    g.add_edge("bear_researcher", "contest_round1")
    g.add_conditional_edges("contest_round1", _route_after_contest)
    g.add_edge("bull_rebuttal", "debate_merge")
    g.add_edge("bear_rebuttal", "debate_merge")
    g.add_edge("debate_merge", "synthesize")
    g.add_edge("synthesize", "propose_intent")
    # 직원 자문은 제안 **뒤**다 - order_intent_proposal 이 있어야 execution_request 가
    # 켜져 venue-cost-worker 가 돈다.
    g.add_edge("propose_intent", "employee_workers")
    g.add_edge("employee_workers", "notion_report")
    g.add_edge("notion_report", END)
    return g.compile()


def _assemble_out(state: DebateState, packet: dict) -> dict:
    """DebateState -> 외부 결과 dict. 필드 목록을 한 곳에서만 유지한다 - run 과 리포트가
    따로 베끼면 한쪽만 필드를 늘렸을 때 드리프트가 생긴다."""
    ihash = state.get("input_hash") or _input_hash(packet)
    return {"pipeline_version": PIPELINE_VERSION,
            "debate_id": state.get("debate_id") or f"dbt-{ihash[:16]}",
            "input_hash": ihash,
            "trace_id": state.get("trace_id") or f"{PIPELINE_VERSION}:{ihash[:16]}",
            "trade_case_id": state.get("trade_case_id"),
            "symbol": packet.get("symbol"),
            "pit": state.get("pit", {"status": "UNKNOWN", "reason": "검증 전 실패"}),
            "debate_opened": state.get("debate_opened", False),
            "claims": state.get("claims", {}),
            "bull": state.get("bull"), "bear": state.get("bear"),
            # 2라운드. 1라운드가 실패하면 없다(건너뛴다).
            "contest_r1": state.get("contest_r1", {}),
            "bull_r2": state.get("bull_r2"), "bear_r2": state.get("bear_r2"),
            "citations": state.get("citations", {}),
            "independence": state.get("independence", {}),
            "contested": state.get("contested", {}),
            "synthesis": state.get("synthesis", {}),
            "employee_workers": state.get("employee_workers", {}),
            "grounded": state.get("grounded", False),
            "escalate": state.get("escalate", True),
            "fallbacks": state.get("fallbacks", []),
            "observability": _langsmith_handoff(
                state.get("trace_id") or f"{PIPELINE_VERSION}:{ihash[:16]}"),
            # F08 완료 조건 "Model, Prompt, 입력 Snapshot과 결과를 기록한다".
            # 입력 Snapshot 은 input_hash, 결과는 이 dict 자체가 기록이다.
            "agent_versions": {"model": _model_version(),
                               "bull_prompt": _prompt_version("bull-researcher"),
                               "bear_prompt": _prompt_version("bear-researcher")},
            "order_intent_proposal": state.get("order_intent_proposal") or {
                "available": False, "reason": "not_reached",
                "detail": "제안 노드 전에 파이프라인이 끝났다",
                "risk_gate_required": True, "submittable": False},
            # 이 파이프라인은 판정을 만들지 않는다 - 소비자가 착각하지 않게 계약으로 박는다.
            # OrderIntent 는 **제안**까지만 만든다. 제출 권한은 Risk 판정 뒤에 생긴다.
            "authoritative": False,
            "produces_order_intent": bool(
                (state.get("order_intent_proposal") or {}).get("available")),
            "submittable": False}


def run_bull_bear_debate(research_packet: dict, *, chat=None, uploader=None,
                         intent_inputs: dict | None = None, workers=None) -> dict:
    """본부 단독 실행 - 리스크/QA/리서치의 run_<dept>_department 와 같은 외부 인터페이스.

    `intent_inputs` 를 주면 grounded 토론에 한해 OrderIntent **제안**까지 만든다.
    키: packet(ResearchPacketV2), signal(StrategySignal), snapshot(MarketSnapshot),
    nav(Decimal), current_quantity(Decimal), trade_case_id?, now?.
    안 주면 토론까지만 하고 제안은 `available: False` 로 사유를 남긴다.

    `workers` 는 직원 레지스트리 러너 주입구다(자체 점검용). 안 주면 실제
    `employee_workers.run_employee_workers` 를 부른다.
    """
    try:
        # tracing_context 는 enabled 를 건드리지 않는다 - LANGSMITH_TRACING 이 꺼져 있으면
        # 그대로 꺼진 채고, 켜져 있을 때만 트레이딩본부 Project 로 보낸다.
        with tracing_context(project_name=_ls_project()):
            state = build_pipeline(chat=chat, uploader=uploader, workers=workers).invoke(
                {"research_packet": research_packet, "fallbacks": [],
                 "intent_inputs": intent_inputs})
    except Exception as exc:  # noqa: BLE001 - intentional fallback boundary
        state = {"research_packet": research_packet, "claims": {}, "debate_opened": False,
                 "bull": None, "bear": None, "grounded": False, "escalate": True,
                 "fallbacks": [_fallback("pipeline", exc)]}
    out = _assemble_out(state, research_packet)
    out["report_markdown"] = state.get("report_markdown") or _render_report_md(out)
    # 그래프가 통째로 실패해 notion_report 까지 못 갔으면 업로드 시도 자체가 없었다는 뜻이다.
    out["notion_upload"] = state.get("notion_upload") or {
        "ok": False, "reason": "파이프라인이 Reporter 전에 실패해 업로드하지 않았다"}
    return out


# ── 결정론적 MD 리포트 (순수 함수 - LLM 이 형식·내용을 창작하지 않는다) ────
def _md_cell(value: Any) -> str:
    """표 셀 한 칸. 줄바꿈은 공백으로 접는다 - <br> 로 바꾸면 Notion 블록에서 문자 그대로 보인다
    (departments/notion_markdown.py 는 HTML 을 해석하지 않는다)."""
    if value is None:
        return "—"
    return " ".join(str(value).replace("|", "\\|").split())


def _md_lines(title: str, value) -> list[str]:
    """제목 + 본문을 진짜 마크다운 줄로 낸다.

    <br> 로 이어붙이지 않는다 - departments/notion_markdown.py 가 이 MD 를 Notion 블록으로
    렌더링하는데, <br> 는 HTML 이라 블록 안에서 문자 그대로 보인다(2026-08-03 실측).
    목록은 목록 줄로, 문단은 문단으로 내야 Notion 에서도 목록·문단으로 읽힌다.
    """
    lines = [f"### {title}", ""]
    if isinstance(value, (list, tuple)):
        lines += [f"- {_md_cell(v)}" for v in value] or ["—"]
    else:
        lines.append(_md_cell(value) if value is not None else "—")
    lines.append("")
    return lines


def _md_refs(values) -> str:
    return _md_cell(", ".join(values or []) or "없음")


def _render_report_md(out: dict) -> str:
    """out(run_bull_bear_debate 반환값)을 그대로 옮겨 적는다."""
    bull, bear = out.get("bull") or {}, out.get("bear") or {}
    pit, versions = out.get("pit") or {}, out.get("agent_versions") or {}
    lines = [
        "# 트레이딩본부 — Bull/Bear 토론 대조표 (결정론적 생성, LLM 자유 서술 아님)",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        f"| **debate_id** | `{_md_cell(out.get('debate_id'))}` |",
        f"| **종목** | {_md_cell(out.get('symbol'))} |",
        f"| **토론 개시** | {'예' if out.get('debate_opened') else '**아니오 (차단)**'} |",
        f"| **grounded** | {'예' if out.get('grounded') else '**아니오**'} |",
        f"| **escalate** | {'**예**' if out.get('escalate') else '아니오'} |",
        f"| **Packet 신선도** | {_md_cell(pit.get('status'))} — {_md_cell(pit.get('reason'))} |",
        f"| trade_case_id | `{_md_cell(out.get('trade_case_id'))}` |",
        f"| pipeline_version | `{_md_cell(out.get('pipeline_version'))}` |",
        f"| input_hash | `{_md_cell(out.get('input_hash'))}` |",
        f"| trace_id | `{_md_cell(out.get('trace_id'))}` |",
        f"| model | `{_md_cell(versions.get('model'))}` |",
        f"| prompt (Bull) | `{_md_cell(versions.get('bull_prompt'))}` |",
        f"| prompt (Bear) | `{_md_cell(versions.get('bear_prompt'))}` |",
        f"| LangSmith | {_md_cell(json.dumps((out.get('observability') or {}).get('langsmith'), ensure_ascii=False, sort_keys=True))} |",
        "",
        "> 이 문서는 **판정이 아니다.** 매수/매도 방향, 수량, 주문 유형을 담지 않는다.",
        "> OrderIntent 는 trader-pm-agent(TRD-03), 승인은 리스크본부 Risk Engine 이 만든다.",
        "",
        "## Claim 색인 (Research Packet 에서 결정론적으로 생성)",
        "",
        "| id | 원문 |",
        "|---|---|",
    ]
    lines += [f"| `{cid}` | {_md_cell(text)} |" for cid, text in (out.get("claims") or {}).items()]

    lines += ["", "## Bull Researcher (TRD-01)", ""]
    if bull:
        lines += _md_lines("Bull Case", bull.get("bull_case"))
        lines += _md_lines("Upside Scenario", bull.get("upside_scenario"))
        lines += _md_lines("Catalyst Timeline", bull.get("catalyst_timeline"))
        lines += _md_lines("Bull Invalidation", bull.get("bull_invalidation"))
        lines += [f"**인용 Claim:** {_md_refs(bull.get('claim_refs'))}"]
    else:
        lines.append("초안 없음 — 아래 Fallback 참고.")

    lines += ["", "## Bear Researcher (TRD-02)", ""]
    if bear:
        lines += _md_lines("Bear Case", bear.get("bear_case"))
        lines += _md_lines("Failure Mode", bear.get("failure_mode"))
        lines += _md_lines("Downside Scenario", bear.get("downside_scenario"))
        lines += _md_lines("Missing Evidence", bear.get("missing_evidence"))
        lines += [f"**인용 Claim:** {_md_refs(bear.get('claim_refs'))}"]
    else:
        lines.append("초안 없음 — 아래 Fallback 참고.")

    # 2라운드 — 상대 원문이 아니라 Claim id 목록만 보고 보강한 결과다.
    bull_r2, bear_r2 = out.get("bull_r2") or {}, out.get("bear_r2") or {}
    if bull_r2 or bear_r2:
        lines += ["", "## 2라운드 보강 (쟁점 id 만 받음 — 상대 원문 미제공)", ""]
        for who, note in (("Bull", bull_r2), ("Bear", bear_r2)):
            lines += [f"### {who} R2", ""]
            if note:
                lines += [f"- **보강:** {_md_cell(note.get('added_case'))}",
                          f"- **다룬 Claim:** {_md_refs(note.get('addressed'))}",
                          f"- **의도적으로 남긴 Claim:** {_md_refs(note.get('still_unaddressed'))}"]
            else:
                lines.append("초안 없음 — 아래 Fallback 참고.")
            lines.append("")

    citations, independence = out.get("citations") or {}, out.get("independence") or {}
    contested = out.get("contested") or {}
    by_round = independence.get("by_round") or {}
    lines += [
        "", "## 결정론 검증 (두 라운드 합산)", "",
        "| 검사 | 결과 |",
        "|---|---|",
        f"| 색인에 없는 인용 (날조) | {_md_refs(citations.get('unknown_refs'))} |",
        f"| Bull 무인용 | {'**예**' if citations.get('bull_uncited') else '아니오'} |",
        f"| Bear 무인용 | {'**예**' if citations.get('bear_uncited') else '아니오'} |",
        f"| 독립성 위반 (문장 복제) | {_md_cell(independence.get('violations', 0))} |",
        f"| ↳ 라운드별 (R1 / R2 / 교차) | {_md_cell(by_round.get('r1', 0))} / "
        f"{_md_cell(by_round.get('r2', 0))} / {_md_cell(by_round.get('cross_round', 0))} |",
        "",
        "> 교차 라운드 복제는 2라운드가 상대의 1라운드 문장을 베낀 경우다. 0 이어야 한다.",
        "",
        "## 쟁점 대조 — TRD-03(PM)이 읽을 산출물",
        "",
        f"- **양측이 다툰 Claim:** {_md_refs(contested.get('contested_refs'))}",
        f"- **Bull 만 인용:** {_md_refs(contested.get('bull_only_refs'))}",
        f"- **Bear 만 인용:** {_md_refs(contested.get('bear_only_refs'))}",
        f"- **아무도 다루지 않은 Claim:** {_md_refs(contested.get('untouched_refs'))}",
    ]

    synthesis = out.get("synthesis") or {}
    if synthesis:
        lines += [
            "", "## 3라운드 종합 (결정론 — 판정이 아니다)", "",
            "| 항목 | 값 |", "|---|---|",
            f"| 근거 커버리지 | {_md_cell(synthesis.get('evidence_coverage'))} |",
            f"| 완료 라운드 | {_md_cell(synthesis.get('rounds_completed'))} |",
            f"| 2라운드에 해소된 쟁점 | {_md_refs(synthesis.get('resolved_issues'))} |",
            f"| 끝까지 미해결 | {_md_refs(synthesis.get('unresolved_issues'))} |",
            f"| 여전히 다투는 Claim | {_md_refs(synthesis.get('still_contested'))} |",
            f"| 끝까지 미인용 Claim | {_md_refs(synthesis.get('unaddressed_claims'))} |",
            f"| 인용 규율 | {'통과' if (synthesis.get('citation_discipline') or {}).get('clean') else '**실패**'} |",
            f"| verdict 생성 | {'**예**' if synthesis.get('produces_verdict') else '아니오'} |",
            "",
            "> 이 종합은 **방향·수량을 정하지 않는다.** 그것은 Signal 의 target_weight 와",
            "> 리스크본부가 정한다 (ADR-0005 \"지키는 경계\").",
        ]

    staff = out.get("employee_workers") or {}
    if staff:
        lines += [
            "", "## 직원 자문 (비바인딩)", "",
            f"- 실행 {_md_refs(staff.get('executed'))}",
            f"- 미실행 {_md_refs(staff.get('not_executed'))}",
            f"- 실패 {_md_refs(staff.get('failed'))}",
            f"- binding: {_md_cell(staff.get('binding'))}",
        ]
        for name, prov in (staff.get("trigger_provenance") or {}).items():
            lines.append(f"  - `{_md_cell(name)}` = {_md_cell(prov.get('value'))} — "
                         f"{_md_cell(prov.get('reason'))}")

    proposal = out.get("order_intent_proposal") or {}
    lines += ["", "## OrderIntent 제안 (주문이 아니다)", "",
              "| 항목 | 값 |", "|---|---|",
              f"| 제안 생성 | {'예' if proposal.get('available') else '**아니오**'} |"]
    if proposal.get("available"):
        intent = proposal.get("order_intent") or {}
        lines += [
            f"| 방향 / 수량 | {_md_cell(intent.get('side'))} / {_md_cell(intent.get('quantity'))} |",
            f"| 지정가 | {_md_cell(intent.get('limit_price'))} |",
            f"| idempotency_key | `{_md_cell(intent.get('idempotency_key'))}` |",
            f"| 근거 Packet | `{_md_cell((proposal.get('packet_ref') or {}).get('packet_id'))}` |",
        ]
    else:
        lines.append(f"| 사유 | {_md_cell(proposal.get('reason'))} — "
                     f"{_md_cell(proposal.get('detail'))} |")
    lines += [
        f"| risk_decision_id | {_md_cell(proposal.get('risk_decision_id'))} |",
        f"| 제출 가능 | {'예' if proposal.get('submittable') else '**아니오 — Risk 판정 선행**'} |",
        "",
        "> 이 제안에는 `risk_decision_id` 가 없다. OMS 는 유효한 Risk 판정 없이 제출을",
        "> 거부하므로 이 문서만으로는 주문이 나가지 않는다(팀 가이드 4.3).",
    ]

    if out.get("fallbacks"):
        lines += ["", "## Fallback", "", "| 단계 | 오류 | 내용 | 안전 조치 |", "|---|---|---|---|"]
        lines += [f"| {_md_cell(f.get('stage'))} | {_md_cell(f.get('error'))} | "
                  f"{_md_cell(f.get('error_message'))} | {_md_cell(f.get('safe_action'))} |"
                  for f in out["fallbacks"]]
    return "\n".join(lines) + "\n"


# ── 자체 점검 (Hermes 없음, 네트워크 없음) ────────────────────────────────
_PACKET = {"symbol": "005930", "thesis": "메모리 업사이클 진입",
           "facts": ["DRAM 고정가 3개월 연속 상승", "4분기 영업이익 컨센서스 상향"],
           "interpretation": ["가격 전가력이 회복되는 국면"],
           "catalysts": ["1월 실적 발표"], "invalidation": ["DRAM 고정가 하락 반전"],
           "evidence_quality": "sufficient"}

_BULL_OK = {"bull_case": "고정가 상승이 이익 레버리지로 이어진다", "upside_scenario": "컨센서스 상회",
            "catalyst_timeline": ["1월 실적"], "bull_invalidation": ["고정가 반전"],
            "claim_refs": ["fact:0", "fact:1"]}
_BEAR_OK = {"bear_case": "가격 상승분이 이미 주가에 반영됐다", "failure_mode": ["수요 둔화"],
            "downside_scenario": "고정가 반전 시 되돌림", "missing_evidence": ["재고 수준 미확인"],
            "claim_refs": ["fact:0", "invalid:0"]}


def _no_upload(out, *, report_md=""):
    """자체 점검 전용 Reporter 스텁. .env 에 실제 NOTION_TOKEN 이 있으므로 스텁을 넣지 않으면
    자체 점검이 진짜 Notion 에 페이지를 만든다 - 그래서 _run() 이 항상 이걸 끼운다."""
    return {"ok": False, "reason": "self-check stub - 네트워크 없음"}


_BULL_R2_OK = {"addressed": ["invalid:0"], "still_unaddressed": [],
               "added_case": "무효화 조건까지 감안해도 상승 논지가 유지된다",
               "claim_refs": ["invalid:0"]}
_BEAR_R2_OK = {"addressed": ["fact:1"], "still_unaddressed": ["catalyst:0: 시점 불명"],
               "added_case": "컨센서스 상향은 이미 가격에 반영됐을 수 있다",
               "claim_refs": ["fact:1"]}


def _no_workers(_payload):
    """자체 점검 전용 직원 러너 스텁. 실제 러너는 Ollama 를 부르므로 네트워크를 탄다."""
    return {"binding": False, "executed": [], "failed": [], "not_executed": [],
            "degraded": False, "workers": [], "trigger_provenance": {}}


def _run(packet, **kw):
    """자체 점검용 실행기 - uploader 와 workers 를 반드시 스텁으로 채운다(네트워크 금지
    규칙을 검사마다 반복해 적는 대신 여기 한 곳에서 강제한다)."""
    kw.setdefault("uploader", _no_upload)
    kw.setdefault("workers", _no_workers)
    return run_bull_bear_debate(packet, **kw)


def _stub(bull=_BULL_OK, bear=_BEAR_OK, capture=None,
          bull_r2=_BULL_R2_OK, bear_r2=_BEAR_R2_OK):
    def chat(persona, task):
        # 역할 코드로 가른다 - Bull 페르소나도 "the Bear Researcher"를 언급하므로(병렬 독립
        # 선언) 이름 부분일치로 가르면 Bull 을 Bear 로 오인한다.
        who = "bear" if "(TRD-02)" in persona else "bull"
        # 라운드는 페르소나가 같으므로 task 문구로 가른다.
        rnd = "r2" if "Round 2" in task else "r1"
        if capture is not None:
            capture[f"{who}_{rnd}" if rnd == "r2" else who] = task
        payload = {("bull", "r1"): bull, ("bear", "r1"): bear,
                   ("bull", "r2"): bull_r2, ("bear", "r2"): bear_r2}[(who, rnd)]
        if isinstance(payload, Exception):
            raise payload
        return json.dumps(payload, ensure_ascii=False)
    return chat


def _check_graph_shape():
    assert build_pipeline() is not None
    print("  그래프 컴파일              OK")


def _check_persona_lookup():
    # config.yaml 에서 두 페르소나가 실제로 읽히는지 - 교정한 문구가 정규식에 안 잡히면 여기서 걸린다.
    for name, marker in (("bull-researcher", "(TRD-01)"), ("bear-researcher", "(TRD-02)")):
        text = _persona(name)
        assert marker in text, f"{name} 페르소나에 {marker} 가 없다"
        assert "never decide quantity" in text, f"{name} 에 수량 결정 금지 문장이 없다"
    assert "never see the Bear output" in _persona("bull-researcher")
    assert "never receive the Bull output" in _persona("bear-researcher")
    try:
        _persona("nonexistent-agent")
        raise AssertionError("없는 페르소나가 조회됐다")
    except ValueError:
        pass
    print("  페르소나 조회 + 실패 가드  OK")


def _check_bear_never_sees_bull():
    """이 파이프라인의 존재 이유. **두 라운드 모두** 상대 원문이 안 섞여야 한다.

    프롬프트가 독립을 선언하는 것과 배선이 실제로 독립인 것은 다르다 - 여기서는
    payload 를 직접 본다(ADR-0005).
    """
    seen: dict = {}
    out = _run(_PACKET, chat=_stub(capture=seen))
    assert set(seen) == {"bull", "bear", "bull_r2", "bear_r2"}, sorted(seen)

    # 1라운드 - 같은 Claim 색인만 받고 서로를 모른다
    for distinctive in (_BULL_OK["bull_case"], _BULL_OK["upside_scenario"]):
        assert distinctive not in seen["bear"], "Bear R1 task 에 Bull 출력이 샜다"
    assert _BEAR_OK["bear_case"] not in seen["bull"], "Bull R1 task 에 Bear 출력이 샜다"
    assert seen["bull"] == _bull_task({"research_packet": _PACKET, "claims": out["claims"]})

    # 2라운드 - **여기가 새 위험 지점이다.** 쟁점 id 만 받고 상대 문장은 못 받는다.
    # 양측이 정당하게 공유하는 Claim 색인 원문과 겹치지 않는 **서술 전용 필드**로 본다
    # (catalyst_timeline "1월 실적"은 Claim "1월 실적 발표"의 부분 문자열이라 오탐이 된다).
    for text in (_BULL_OK["bull_case"], _BULL_OK["upside_scenario"], *_BULL_OK["bull_invalidation"]):
        assert text not in seen["bear_r2"], "Bear R2 task 에 Bull 1라운드 원문이 샜다"
    for text in (_BEAR_OK["bear_case"], _BEAR_OK["downside_scenario"], *_BEAR_OK["failure_mode"]):
        assert text not in seen["bull_r2"], "Bull R2 task 에 Bear 1라운드 원문이 샜다"
    # 상대가 인용한 Claim **id** 는 들어간다 - 그게 쟁점 목록의 내용이다
    assert "invalid:0" in seen["bull_r2"], "Bear 만 인용한 Claim id 가 안 넘어갔다"
    assert "fact:1" in seen["bear_r2"], "Bull 만 인용한 Claim id 가 안 넘어갔다"
    # 2라운드 프롬프트가 독립을 명시하는지 - 배선(위)과 문구(아래)를 둘 다 본다
    assert "Round 2" in seen["bull_r2"]
    assert "never will be" in seen["bull_r2"], "R2 프롬프트에 독립 선언이 없다"

    # 라운드별 독립성 측정 - 교차 라운드 복제까지 0 이어야 한다
    assert out["independence"]["violations"] == 0
    assert out["independence"]["by_round"] == {"r1": 0, "r2": 0, "cross_round": 0}
    print("  Bear 독립성 (2라운드 배선) OK")


def _check_citation_guard():
    # 색인에 없는 Claim ID 를 인용하면 grounded 가 무너지고 escalate 된다.
    out = _run(_PACKET, chat=_stub(bear={**_BEAR_OK, "claim_refs": ["fact:0", "fact:99"]}))
    assert out["citations"]["unknown_refs"] == ["fact:99"], out["citations"]
    assert out["grounded"] is False and out["escalate"] is True
    # 2라운드 인용도 같은 색인으로 검증된다 - R2 에서 날조해도 잡힌다
    r2_fake = _run(_PACKET, chat=_stub(bear_r2={**_BEAR_R2_OK, "claim_refs": ["fact:77"]}))
    assert r2_fake["citations"]["unknown_refs"] == ["fact:77"], r2_fake["citations"]
    assert r2_fake["grounded"] is False

    # 인용이 아예 없어도 통과시키지 않는다. **두 라운드 다 무인용이어야 무인용이다** -
    # R1 에서 안 하고 R2 에서 했으면 인용을 한 것이다.
    out2 = _run(_PACKET, chat=_stub(bull={**_BULL_OK, "claim_refs": []},
                                    bull_r2={**_BULL_R2_OK, "claim_refs": []}))
    assert out2["citations"]["bull_uncited"] is True and out2["grounded"] is False
    # R1 만 비었으면 무인용이 아니다
    partial = _run(_PACKET, chat=_stub(bull={**_BULL_OK, "claim_refs": []}))
    assert partial["citations"]["bull_uncited"] is False, partial["citations"]
    assert partial["citations"]["by_round"]["r1"]["bull"] == []
    assert partial["citations"]["by_round"]["r2"]["bull"] == ["invalid:0"]
    print("  인용 검증 (결정론, 2라운드) OK")


def _check_schema_guard():
    # 불완전한 초안도 Hermes 장애도 파이프라인을 죽이지 않고 fallback + escalate 로 떨어진다.
    for broken in ({k: v for k, v in _BULL_OK.items() if k != "upside_scenario"},
                   {**_BULL_OK, "claim_refs": "fact:0"},
                   RuntimeError("hermes 응답 없음")):
        out = _run(_PACKET, chat=_stub(bull=broken))
        assert out["bull"] is None, "불완전 Bull 초안이 통과했다"
        assert [f["stage"] for f in out["fallbacks"]] == ["bull_researcher"], out["fallbacks"]
        assert out["fallbacks"][0]["attempts"] == _max_attempts()
        assert out["grounded"] is False and out["escalate"] is True
        assert out["bear"] is not None, "Bull 실패가 Bear 까지 죽였다"
    print("  스키마·장애 가드           OK")


def _check_insufficient_evidence_gate():
    # 근거 부족 Packet 은 토론을 열지 않는다 - LLM 을 아예 부르지 않는지까지 본다.
    called: list = []

    def never(persona, task):
        called.append(persona)
        return "{}"

    out = _run({**_PACKET, "evidence_quality": "insufficient_evidence"}, chat=never)
    assert called == [], "근거 부족인데 LLM 을 불렀다"
    assert out["debate_opened"] is False and out["escalate"] is True
    # safe_action 은 investment-case.yaml trading step 의 failure_action 과 같은 값이어야 한다
    assert out["fallbacks"][0]["safe_action"] == "HOLD", out["fallbacks"][0]
    print("  근거부족 차단 게이트       OK")


def _check_retry_and_versions():
    # investment-case.yaml trading step retry.max_attempts: 3 / F08 "Schema 실패는 재시도 후 PASS".
    calls: list = []

    def flaky(persona, task):
        calls.append(task)
        bear = "(TRD-02)" in persona
        if "Round 2" in task:      # 2라운드는 흔들지 않는다 - 재시도 검사는 1라운드가 대상이다
            return json.dumps(_BEAR_R2_OK if bear else _BULL_R2_OK, ensure_ascii=False)
        if bear:
            return json.dumps(_BEAR_OK, ensure_ascii=False)
        # Bull 은 첫 두 번 스키마를 어기고 세 번째에 성공한다
        bull_calls = sum(1 for t in calls if "bullish case" in t)
        if bull_calls < 3:
            return json.dumps({k: v for k, v in _BULL_OK.items() if k != "claim_refs"})
        return json.dumps(_BULL_OK, ensure_ascii=False)

    out = _run(_PACKET, chat=flaky)
    assert out["bull"] is not None, "재시도가 없어서 복구하지 못했다"
    assert out["bull"]["attempts"] == 3, out["bull"]["attempts"]
    assert out["bear"]["attempts"] == 1
    assert out["grounded"] is True and not out["fallbacks"]
    # 거부 사유가 다음 시도 프롬프트에 실려야 같은 실수를 반복하지 않는다.
    # 2라운드가 뒤에 붙어서 마지막 호출은 R2 다 - 1라운드 호출들 중에서 찾는다.
    r1_calls = [t for t in calls if "Round 2" not in t]
    assert any("was rejected" in t for t in r1_calls), "거부 사유가 재시도 프롬프트에 없다"

    # 3회를 다 쓰고도 실패하면 attempts=3 으로 기록되고 HOLD 로 떨어진다
    dead = _run(_PACKET, chat=_stub(bull=RuntimeError("hermes down")))
    assert dead["fallbacks"][0]["attempts"] == 3, dead["fallbacks"][0]
    assert dead["fallbacks"][0]["safe_action"] == "HOLD"

    # F08 완료 조건 "Model, Prompt, 입력 Snapshot과 결과를 기록한다"
    v = out["agent_versions"]
    assert v["model"] == _model_version() and v["model"] != "unknown", v
    assert v["bull_prompt"].startswith("bull-researcher@") and v["bear_prompt"] != v["bull_prompt"]
    print("  재시도 + Model/Prompt 기록 OK")


def _check_pit_gate():
    limit = _max_age_minutes()

    # 1) _check_freshness 단위 검사 - now 를 주입해 시계에 의존하지 않는다.
    fixed = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    assert _check_freshness(
        {"as_of": (fixed - timedelta(minutes=1)).isoformat()}, now=fixed)["status"] == "FRESH"
    assert _check_freshness(
        {"as_of": (fixed - timedelta(minutes=limit + 10)).isoformat()},
        now=fixed)["status"] == "STALE"
    assert _check_freshness(
        {"as_of": (fixed + timedelta(hours=1)).isoformat()}, now=fixed)["status"] == "FUTURE"
    # naive 시각은 UTC 로 가정하지 않는다 - KST 와 9시간 어긋난다
    assert _check_freshness({"as_of": "2026-08-03T11:00:00"}, now=fixed)["status"] == "UNKNOWN"
    assert _check_freshness({"as_of": "어제"}, now=fixed)["status"] == "UNKNOWN"
    assert _check_freshness({}, now=fixed)["status"] == "UNKNOWN"
    # 리서치본부가 as_of 를 universe 블록에만 싣는 경우도 읽는다
    assert _check_freshness(
        {"universe": {"as_of": (fixed - timedelta(minutes=1)).isoformat()}},
        now=fixed)["status"] == "FRESH"

    # 2) 관통 검사 - validate_packet 은 실제 시계를 쓰므로 Packet 도 실제 시계 기준으로 만든다.
    real_now = datetime.now(timezone.utc)
    called: list = []
    out = _run({**_PACKET, "as_of": (real_now + timedelta(hours=1)).isoformat()},
                               chat=lambda p, t: called.append(p) or "{}")
    assert called == [], "미래 시각 Packet 인데 LLM 을 불렀다"   # 개발 원칙 5번
    assert out["pit"]["status"] == "FUTURE" and out["debate_opened"] is False
    assert out["fallbacks"][0]["error"] == "PointInTimeViolation"

    # STALE 은 토론은 열되 grounded 를 못 준다 - 오래된 근거로 PM 에게 넘기지 않는다.
    out2 = _run(
        {**_PACKET, "as_of": (real_now - timedelta(minutes=limit + 10)).isoformat()}, chat=_stub())
    assert out2["debate_opened"] is True and out2["pit"]["status"] == "STALE", out2["pit"]
    assert out2["grounded"] is False and out2["escalate"] is True

    # FRESH 는 정상 통과 - 게이트가 항상 막기만 하는 게 아니라는 확인
    out3 = _run({**_PACKET, "as_of": real_now.isoformat()}, chat=_stub())
    assert out3["pit"]["status"] == "FRESH" and out3["grounded"] is True
    print("  PIT 신선도 게이트          OK")


def _check_malformed_packet():
    # 필수 필드가 없는 Packet 은 예외로 죽지 않고 fail-closed 결과로 나온다.
    out = _run({"symbol": "005930"}, chat=_stub())
    assert out["grounded"] is False and out["escalate"] is True
    assert out["fallbacks"][0]["stage"] == "pipeline"
    assert out["report_markdown"], "실패해도 리포트는 나와야 한다"
    print("  잘못된 Packet fail-closed  OK")


def _check_parallel_fallback_merge():
    # 병렬 노드 둘이 같은 fallbacks 키에 쓴다 - reducer 가 없으면 여기서 InvalidUpdateError 다.
    # 주입한 chat 으로 양쪽을 실패시킨다(Hermes 설치 여부에 의존하지 않는다).
    boom = _stub(bull=RuntimeError("bull down"), bear=RuntimeError("bear down"))
    out = _run(_PACKET, chat=boom)
    assert {f["stage"] for f in out["fallbacks"]} == {"bull_researcher", "bear_researcher"}
    assert out["grounded"] is False and out["escalate"] is True
    print("  병렬 fallback 병합         OK")


def _check_contested_math():
    """라운드별 쟁점 대조. 1라운드 쟁점이 2라운드에서 어떻게 해소되는지까지 본다."""
    out = _run(_PACKET, chat=_stub())

    # 1라운드 - 상대만 인용한 Claim 이 쟁점으로 드러난다
    r1 = out["contest_r1"]
    assert r1["contested_refs"] == ["fact:0"], r1        # 양측이 같이 다툰 Claim
    assert r1["bull_only_refs"] == ["fact:1"]
    assert r1["bear_only_refs"] == ["invalid:0"]
    assert "catalyst:0" in r1["untouched_refs"]          # 아무도 안 건드린 Claim
    assert r1["decided_by"] == "deterministic"

    # 2라운드 합산 - 각자 상대만 인용했던 Claim 을 다뤄 쟁점이 좁혀진다
    c = out["contested"]
    assert c["contested_refs"] == ["fact:0", "fact:1", "invalid:0"], c
    assert c["bull_only_refs"] == [] and c["bear_only_refs"] == []
    assert "catalyst:0" in c["untouched_refs"], "끝까지 안 다룬 Claim 이 사라졌다"

    assert out["grounded"] is True and out["escalate"] is False
    assert out["produces_order_intent"] is False      # 이 본부 단계에서 주문은 안 만든다
    print("  쟁점 대조 (라운드별)       OK")


def _check_reproducibility_and_keys():
    # 같은 Packet 이면 같은 input_hash·debate_id. trade_case_id 는 그대로 실려 나간다.
    a = _run(_PACKET, chat=_stub())
    b = _run(dict(reversed(list(_PACKET.items()))), chat=_stub())
    assert a["input_hash"] == b["input_hash"], "키 순서가 해시를 바꿨다"
    assert a["debate_id"] == b["debate_id"]
    c = _run({**_PACKET, "thesis": "다른 논지"}, chat=_stub())
    assert c["input_hash"] != a["input_hash"], "Packet 이 바뀌었는데 해시가 같다"
    keyed = _run({**_PACKET, "trade_case_id": "tc-1", "trace_id": "tr-1"},
                                 chat=_stub())
    assert keyed["trade_case_id"] == "tc-1" and keyed["trace_id"] == "tr-1"
    print("  재현성 + 부서간 키 관통    OK")


def _check_report_is_deterministic():
    out = _run(_PACKET, chat=_stub())
    md = out["report_markdown"]
    assert _render_report_md(out) == md, "리포트가 호출마다 달라진다"
    for must in (out["debate_id"], out["input_hash"], "fact:0", "판정이 아니다",
                 _BULL_OK["bull_case"], _BEAR_OK["bear_case"]):
        assert must in md, f"리포트에 {must!r} 가 없다"

    # departments/notion_markdown.py 가 이 MD 를 Notion 블록으로 바꾼다. 생성과 렌더링이
    # 어긋나면(예: <br> 를 쓰면) Notion 에서 문자 그대로 보인다 - 여기서 잡는다.
    from departments.notion_markdown import markdown_to_notion_blocks

    assert "<br>" not in md, "MD 에 <br> 가 있다 - Notion 블록에서 문자로 보인다"
    blocks = markdown_to_notion_blocks(md)
    kinds = {b["type"] for b in blocks}
    for need in ("heading_1", "heading_2", "heading_3", "table", "bulleted_list_item", "quote"):
        assert need in kinds, f"렌더링 결과에 {need} 가 없다: {sorted(kinds)}"

    def _plain(b):
        d = b.get(b["type"], {})
        return "".join(x.get("text", {}).get("content", "") for x in (d.get("rich_text") or []))

    rendered = " ".join(_plain(b) for b in blocks)
    assert "<br>" not in rendered and "**" not in rendered, "마크업이 본문 텍스트로 샜다"
    assert _BEAR_OK["failure_mode"][0] in rendered, "Bear 목록이 렌더링에서 유실됐다"
    print("  결정론 MD 리포트 + 렌더링  OK")


def _check_agent_has_no_tools():
    """분석가에게 도구가 붙지 않는지. Hermes 없이도 돌도록 가짜 run_agent 를 끼운다.

    실측 사고(2026-08-03): 도구를 안 막았더니 bull-researcher 가 답변을 반환하는 대신
    departments/04-research/bull_report_005930.json 을 만들었다 - 없는 본부 경로다.
    """
    import types

    captured: dict = {}

    class FakeAgent:
        def __init__(self, **kw):
            captured.update(kw)

        def chat(self, task):
            return "{}"

    fake = types.ModuleType("run_agent")
    fake.AIAgent = FakeAgent
    saved = sys.modules.get("run_agent")
    sys.modules["run_agent"] = fake
    try:
        _hermes_chat("persona", "task")
    finally:
        sys.modules.pop("run_agent", None)
        if saved is not None:
            sys.modules["run_agent"] = saved

    assert captured.get("enabled_toolsets") == [], \
        f"분석가에게 도구가 열려 있다: enabled_toolsets={captured.get('enabled_toolsets')!r}"
    assert captured.get("ephemeral_system_prompt") == "persona"
    assert captured.get("model") == _model_version(), "config.yaml 의 model 을 안 쓴다"
    print("  분석가 도구 0개 (경계)     OK")


def _check_notion_report_node():
    # Reporter 가 그래프에 실제로 배선됐고, 완성된 결과 + MD 리포트를 받는지.
    seen: dict = {}

    def uploader(out, *, report_md=""):
        seen["out"], seen["report_md"] = out, report_md
        return {"ok": True, "url": "https://notion.so/fake"}

    out = _run(_PACKET, chat=_stub(), uploader=uploader)
    assert out["notion_upload"] == {"ok": True, "url": "https://notion.so/fake"}
    assert seen["out"]["debate_id"] == out["debate_id"]
    assert seen["out"]["grounded"] is True          # 판정이 확정된 뒤에 불린다
    assert "fact:0" in seen["report_md"], "Reporter 가 MD 리포트를 못 받았다"

    # 업로드가 실패해도 토론 판정은 안 바뀐다 (Notion 은 Projection 일 뿐)
    def dead(out, *, report_md=""):
        raise RuntimeError("notion down")

    broken = _run(_PACKET, chat=_stub(), uploader=dead)
    assert broken["notion_upload"]["ok"] is False
    assert broken["grounded"] is True and broken["escalate"] is False, "업로드 실패가 판정을 바꿨다"

    # 토론이 차단돼도 그 사실이 Notion 으로 올라간다 - 조용히 사라지지 않는다
    blocked = _run({**_PACKET, "evidence_quality": "insufficient_evidence"},
                   chat=_stub(), uploader=uploader)
    assert blocked["notion_upload"]["ok"] is True
    assert seen["out"]["debate_opened"] is False
    print("  Notion Reporter 노드       OK")


def _check_langsmith_observability():
    """기본은 꺼짐이고, Project 는 트레이딩본부로 격리된다.
    실제 그래프를 켠 채로 돌리지 않는다 - 이 점검은 네트워크를 타면 안 된다."""
    saved = {k: os.environ.get(k) for k in
             ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_PROJECT")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        off = _langsmith_handoff("t1")["langsmith"]
        assert off["enabled"] is False and off["handoff_status"] == "not_configured", off
        assert off["project"] is None, off

        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGSMITH_PROJECT"] = "First"
        on = _langsmith_handoff("t1")["langsmith"]
        assert on == {"enabled": True, "project": "First-02-trading",
                      "run_id": None, "handoff_status": "configured"}, on
        # 회계본부 Project 를 그대로 쓰지 않는다 (부서 경계).
        assert _ls_project().endswith("-02-trading"), _ls_project()
        os.environ.pop("LANGSMITH_PROJECT")
        assert _ls_project() == "hedgefund-02-trading", _ls_project()
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
    out = _run(_PACKET, chat=_stub())
    assert out["observability"]["trace_id"] == out["trace_id"], out["observability"]
    assert "| LangSmith |" in out["report_markdown"]
    print("  LangSmith 관측성           OK")


def _intent_fixture(*, weight="0.02", held="0"):
    """제안 검사용 입력 묶음. Packet 은 동규님 testkit canonical Fixture 를 쓴다 -
    우리가 지어내면 재일님 계약이 바뀌어도 이 검사가 안 깨진다."""
    from decimal import Decimal
    from uuid import uuid4

    sys.path.insert(0, str(_BASE / "contracts"))
    from contracts import MarketSnapshot, StrategySignal
    from departments.risk_qa_testkit.research_packet import make_canonical_test_packet

    now = datetime.now(timezone.utc)
    instrument_id = uuid4()
    canonical = make_canonical_test_packet(as_known_at=now - timedelta(hours=1))
    packet = canonical.research_packet.model_copy(
        update={"instrument_id": str(instrument_id), "status": "PUBLISHED"})
    signal = StrategySignal(
        strategy_id=uuid4(), strategy_version="v1", fund_id=uuid4(), book_id=uuid4(),
        instrument_id=instrument_id, philosophy="momentum", target_weight=Decimal(weight),
        stage="paper", as_of=now, valid_until=now + timedelta(hours=6), trace_id="t_prop")
    snapshot = MarketSnapshot(market_snapshot_id="snap_prop", as_of=now,
                              bid=Decimal("69900"), ask=Decimal("70100"))
    debated = {**_PACKET, "as_of": now.isoformat(), "packet_id": packet.packet_id}
    return debated, {"packet": packet, "signal": signal, "snapshot": snapshot,
                     "nav": Decimal("1000000000"), "current_quantity": Decimal(held),
                     "now": now}


def _check_intent_proposal():
    """토론 -> OrderIntent 제안. **grounded 일 때만, 그리고 Risk 없이는 주문이 안 된다.**"""
    from decimal import Decimal

    debated, inputs = _intent_fixture()

    # 1. 입력이 없으면 제안이 없다 - 추정해서 만들지 않는다
    bare = _run(debated, chat=_stub())
    assert bare["grounded"] is True and bare["produces_order_intent"] is False
    assert bare["order_intent_proposal"]["reason"] == "inputs_missing", bare["order_intent_proposal"]

    # 2. grounded 가 아니면 입력이 다 있어도 제안이 없다 (인용 날조 토론)
    ungrounded = _run(debated, chat=_stub(bear={**_BEAR_OK, "claim_refs": ["fact:99"]}),
                      intent_inputs=inputs)
    assert ungrounded["grounded"] is False
    assert ungrounded["order_intent_proposal"]["reason"] == "not_grounded"
    assert ungrounded["produces_order_intent"] is False

    # 3. grounded + 입력 -> 제안이 생긴다. 단 제출 권한은 없다
    out = _run(debated, chat=_stub(), intent_inputs=inputs)
    proposal = out["order_intent_proposal"]
    assert proposal["available"] is True, proposal
    assert out["produces_order_intent"] is True and out["submittable"] is False
    assert proposal["risk_gate_required"] is True and proposal["risk_decision_id"] is None
    intent_dict = proposal["order_intent"]
    assert intent_dict["side"] == "BUY" and Decimal(intent_dict["quantity"]) == Decimal("285")
    assert proposal["packet_ref"]["packet_id"] == inputs["packet"].packet_id
    assert out["authoritative"] is False

    # 4. **제안만으로는 OMS 가 주문을 만들지 않는다** - Risk Gate 가 선행한다는 증명
    sys.path.insert(0, str(_BASE / "oms"))
    from contracts import OrderIntent
    from oms import OMS, OMSError

    intent = OrderIntent.model_validate(intent_dict)
    oms = OMS(adapter="paper")
    rec = oms.register_intent(intent)
    try:
        oms.create_broker_order(rec, intent)
        raise AssertionError("Risk 판정 없는 제안이 주문이 됐다")
    except OMSError:
        pass

    # 5. Packet 게이트에 막히면 제안만 없고 토론 판정은 그대로다
    blocked_packet = inputs["packet"].model_copy(update={"status": "PARTIAL"})
    blocked = _run(debated, chat=_stub(), intent_inputs={**inputs, "packet": blocked_packet})
    assert blocked["grounded"] is True, "게이트 차단이 토론 판정을 바꿨다"
    assert blocked["order_intent_proposal"]["available"] is False
    assert blocked["order_intent_proposal"]["reason"] == "PacketGateError"

    # 6. 다른 Packet 을 물고 오면 거부한다 - A 의 근거 위에 B 의 주문이 못 올라탄다
    other = inputs["packet"].model_copy(update={"packet_id": "pkt-other"})
    mixed = _run(debated, chat=_stub(), intent_inputs={**inputs, "packet": other})
    assert mixed["order_intent_proposal"]["reason"] == "packet_mismatch", \
        mixed["order_intent_proposal"]

    # 7. 목표에 이미 도달했으면 0주 주문을 제안하지 않는다
    _, held = _intent_fixture(held="285")
    reached = _run(debated, chat=_stub(), intent_inputs={**held, "packet": inputs["packet"],
                                                         "signal": inputs["signal"]})
    assert reached["order_intent_proposal"]["reason"] == "no_delta", \
        reached["order_intent_proposal"]

    # 8. 리포트에 제안과 "Risk 판정 선행"이 그대로 적힌다
    md = out["report_markdown"]
    assert "## OrderIntent 제안 (주문이 아니다)" in md
    assert "Risk 판정 선행" in md and intent_dict["idempotency_key"] in md
    print("  OrderIntent 제안 + Risk 선행 OK")


def _check_three_round_debate():
    """3라운드 구조. **종합은 결정론이고 verdict 를 만들지 않는다.**"""
    out = _run(_PACKET, chat=_stub())
    s = out["synthesis"]

    # 1. 3라운드 종합이 셈한 것들
    assert s["round"] == 3 and s["rounds_completed"] == 2
    # Claim 5개 중 2라운드까지 다뤄진 것 3개 (catalyst:0, interp:0 은 끝까지 미인용)
    assert s["evidence_coverage"] == 0.6, s["evidence_coverage"]
    assert set(s["unaddressed_claims"]) == {"catalyst:0", "interp:0"}, s["unaddressed_claims"]
    # 1라운드에 한쪽만 인용했던 Claim 을 2라운드에서 양측이 다뤘다 -> 해소
    assert s["resolved_issues"] == ["fact:1", "invalid:0"], s["resolved_issues"]
    assert s["unresolved_issues"] == []
    assert s["citation_discipline"]["clean"] is True
    assert s["independence"] == {"r1": 0, "r2": 0, "cross_round": 0}

    # 2. **판정을 만들지 않는다** - 이게 3라운드의 경계다
    assert s["produces_verdict"] is False and s["authoritative"] is False
    assert s["decided_by"] == "deterministic"
    forbidden = {"verdict", "recommendation", "side", "quantity", "action", "direction"}
    assert not (forbidden & set(s)), f"종합이 판정 필드를 만들었다: {forbidden & set(s)}"

    # 3. **1라운드가 실패하면 2라운드를 건너뛴다** - 실패한 토론에 LLM 을 더 안 태운다
    calls: list = []

    def counting(persona, task):
        calls.append(task)
        if "(TRD-01)" in persona:
            raise RuntimeError("bull down")
        return json.dumps(_BEAR_OK if "Round 2" not in task else _BEAR_R2_OK,
                          ensure_ascii=False)

    dead = _run(_PACKET, chat=counting)
    assert dead["bull"] is None and dead["grounded"] is False
    assert not any("Round 2" in t for t in calls), "1라운드 실패인데 2라운드를 태웠다"
    assert dead["bull_r2"] is None and dead["bear_r2"] is None
    assert dead["synthesis"]["rounds_completed"] == 1
    assert dead["escalate"] is True

    # 4. 2라운드만 실패해도 1라운드 결과는 남는다
    half = _run(_PACKET, chat=_stub(bull_r2=RuntimeError("r2 down")))
    assert half["bull"] is not None and half["bull_r2"] is None
    assert half["contest_r1"]["contested_refs"] == ["fact:0"], "1라운드 쟁점이 사라졌다"
    assert any(f["stage"] == "bull_r2_researcher" for f in half["fallbacks"]), half["fallbacks"]

    # 5. 리포트에 라운드별 섹션과 종합표가 나온다
    md = out["report_markdown"]
    assert "## 2라운드 보강 (쟁점 id 만 받음 — 상대 원문 미제공)" in md
    assert "## 3라운드 종합 (결정론 — 판정이 아니다)" in md
    assert "교차 라운드 복제" in md
    print("  3라운드 토론 + 종합        OK")


def _check_employee_workers_node():
    """직원 자문 노드. **상대 원문을 직원에게도 안 준다. 실패해도 토론 판정은 그대로.**"""
    seen: dict = {}

    def spy(payload):
        seen["payload"] = payload
        return {"binding": False, "executed": ["bull-thesis-worker"], "failed": [],
                "not_executed": [], "degraded": False, "workers": [],
                "trigger_provenance": {"approved_risk": {"value": False, "reason": "테스트"}}}

    out = _run(_PACKET, chat=_stub(), workers=spy)
    payload = seen["payload"]

    # 1. 토론 산출이 근거로 넘어간다
    assert set(payload["debate"]) >= {"claims", "citations", "contested", "independence",
                                      "synthesis", "grounded", "order_intent_proposal"}
    assert payload["debate"]["claims"] == out["claims"]
    assert payload["debate"]["contested"]["contested_refs"] == ["fact:0", "fact:1", "invalid:0"]

    # 2. **bull/bear 원문은 안 넘어간다** - 직원 계층에서도 논지 복제를 막는다
    assert "bull" not in payload["debate"] and "bear" not in payload["debate"]
    dumped = json.dumps(payload, ensure_ascii=False, default=str)
    assert _BULL_OK["bull_case"] not in dumped, "직원 payload 에 Bull 원문이 샜다"
    assert _BEAR_OK["bear_case"] not in dumped, "직원 payload 에 Bear 원문이 샜다"

    # 3. 결과가 실려 나오고 리포트에 적힌다
    assert out["employee_workers"]["binding"] is False
    assert "## 직원 자문 (비바인딩)" in out["report_markdown"]
    assert "approved_risk" in out["report_markdown"]

    # 4. **직원 실패가 토론 판정을 못 바꾼다**
    def boom(_payload):
        raise RuntimeError("ollama down")

    broken = _run(_PACKET, chat=_stub(), workers=boom)
    assert broken["grounded"] is True, "직원 실패가 grounded 를 바꿨다"
    assert broken["employee_workers"]["degraded"] is True
    assert broken["employee_workers"]["binding"] is False
    assert broken["report_markdown"], "직원이 죽었는데 리포트가 없다"
    print("  직원 자문 노드 (경계)      OK")


def _check_secret_redaction():
    leaked = _fallback("bull_researcher", RuntimeError("connect https://x.notion.com/t ntn_abc123DEF"))
    assert "ntn_abc123DEF" not in leaked["error_message"], leaked
    assert "https://" not in leaked["error_message"], leaked
    print("  자격증명 마스킹            OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        print(f"{PIPELINE_VERSION} 실행 (고정 Fixture Packet - 실 Hermes 호출)")
        result = run_bull_bear_debate({**_PACKET, "as_of": datetime.now(timezone.utc).isoformat()})
        print(json.dumps({k: v for k, v in result.items() if k != "report_markdown"},
                         ensure_ascii=False, indent=1))
        report_dir = _BASE / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = report_dir / f"trading_debate_{result['debate_id']}_{stamp}.md"
        path.write_text(result["report_markdown"], encoding="utf-8")
        print(f"결정론적 MD 리포트 저장: {path}")
        raise SystemExit(0 if result["grounded"] else 1)

    print(f"{PIPELINE_VERSION} 자체 점검 (Hermes 없음, 네트워크 없음)")
    _check_graph_shape()
    _check_persona_lookup()
    _check_bear_never_sees_bull()
    _check_citation_guard()
    _check_schema_guard()
    _check_insufficient_evidence_gate()
    _check_retry_and_versions()
    _check_pit_gate()
    _check_malformed_packet()
    _check_parallel_fallback_merge()
    _check_contested_math()
    _check_reproducibility_and_keys()
    _check_report_is_deterministic()
    _check_agent_has_no_tools()
    _check_notion_report_node()
    _check_intent_proposal()
    _check_three_round_debate()
    _check_employee_workers_node()
    _check_secret_redaction()
    _check_langsmith_observability()
    print("트레이딩본부 토론 파이프라인 20개 영역 통과. 실행은 --run (Hermes + Notion 필요)")
