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
  notion_report     Reporter (결정론 - notion_reporter.upload_debate, LLM 아님) - 결과를
                    Notion Trading DB(NOTION_TRADING_DB)에 Projection 으로 올린다.
                    업로드가 실패해도 grounded/escalate 는 못 바꾼다.

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
    citations: dict              # 결정론 인용 검증
    independence: dict           # 결정론 독립성 측정
    contested: dict              # 양측이 같은 Claim 을 다뤘는지
    grounded: bool
    escalate: bool
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
        raise ValueError(f"{who} 의 claim_refs 가 배열이 아니다 - 초안 거부")
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
        except Exception as exc:
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


# ── 노드 4: 대조 (결정론 - 판정 아님) ──────────────────────────────────────
def _sentences(obj) -> set[str]:
    """서술 필드를 문장 단위로 정규화. 독립성(Bull 문장 복제 0) 측정용.

    서술이 아닌 필드(claim_refs, attempts 같은 메타)는 이름이 아니라 **타입으로** 거른다 -
    이름으로 거르면 새 메타 필드가 늘 때마다 여기서 터진다(실측: attempts 추가 때 터졌다)."""
    parts: list[str] = []
    for key, value in (obj or {}).items():
        if key == "claim_refs":
            continue
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, (list, tuple)):
            parts += [str(v) for v in value]
    text = " ".join(parts)
    return {s for s in (" ".join(p.split()) for p in re.split(r"[.!?。\n]", text)) if len(s) > 12}


def debate_merge(state: DebateState) -> dict:
    claims, bull, bear = state.get("claims", {}), state.get("bull"), state.get("bear")
    bull_refs = list((bull or {}).get("claim_refs", []))
    bear_refs = list((bear or {}).get("claim_refs", []))

    # 인용 검증 - 색인에 없는 Claim ID 는 날조다. LLM 이 아니라 여기서 잡는다.
    unknown = sorted({r for r in bull_refs + bear_refs if r not in claims})
    citations = {"bull_refs": bull_refs, "bear_refs": bear_refs, "unknown_refs": unknown,
                 "bull_uncited": bool(bull) and not bull_refs,
                 "bear_uncited": bool(bear) and not bear_refs}

    # 독립성 - 병렬이라 원래 0 이어야 한다. 0 이 아니면 배선이 샌 것이다.
    shared = _sentences(bull) & _sentences(bear)
    independence = {"duplicated_sentences": sorted(shared), "violations": len(shared)}

    b, r = set(bull_refs) & set(claims), set(bear_refs) & set(claims)
    contested = {"contested_refs": sorted(b & r), "bull_only_refs": sorted(b - r),
                 "bear_only_refs": sorted(r - b),
                 "untouched_refs": sorted(set(claims) - b - r)}

    stale = (state.get("pit") or {}).get("status") in {"STALE", "FUTURE"}
    grounded = bool(
        state.get("debate_opened") and bull and bear and not unknown
        and not citations["bull_uncited"] and not citations["bear_uncited"]
        and not independence["violations"] and not stale
    )
    return {"citations": citations, "independence": independence, "contested": contested,
            "grounded": grounded, "escalate": not grounded or bool(state.get("fallbacks"))}


# ── 그래프 조립 ────────────────────────────────────────────────────────────
def _route_after_validate(state: DebateState):
    # 토론을 열면 Bull/Bear 를 같은 superstep 에 병렬로 띄운다. 둘 다 끝나야 debate_merge 가 돈다.
    if state.get("debate_opened"):
        return ["bull_researcher", "bear_researcher"]
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
    except Exception as exc:   # reporter 는 원래 예외를 안 던지지만, 던져도 여기서 멈춘다
        result = {"ok": False, "reason": f"Reporter 예외: {type(exc).__name__}"}
    return {"notion_upload": result, "report_markdown": report_md}


def build_pipeline(chat=None, uploader=None):
    """chat / uploader 를 주입받아 자체 점검이 전역 함수를 바꿔치기하지 않아도 되게 한다
    (리스크본부는 global 스왑을 쓴다 - 같은 결과인데 되돌리기 코드가 길어져서 여기선 주입).
    자체 점검이 Hermes 설치·Notion 토큰 유무에 의존하지 않게 하는 효과도 있다."""
    g = StateGraph(DebateState)
    g.add_node("validate_packet", _guard_node("validate_packet", validate_packet))
    g.add_node("bull_researcher",
               _guard_node("bull_researcher", lambda s: bull_researcher(s, chat=chat)))
    g.add_node("bear_researcher",
               _guard_node("bear_researcher", lambda s: bear_researcher(s, chat=chat)))
    g.add_node("debate_merge", _guard_node("debate_merge", debate_merge))
    g.add_node("notion_report",
               _guard_node("notion_report", lambda s: notion_report(s, uploader=uploader)))
    g.set_entry_point("validate_packet")
    g.add_conditional_edges("validate_packet", _route_after_validate)
    g.add_edge("bull_researcher", "debate_merge")
    g.add_edge("bear_researcher", "debate_merge")
    g.add_edge("debate_merge", "notion_report")
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
            "citations": state.get("citations", {}),
            "independence": state.get("independence", {}),
            "contested": state.get("contested", {}),
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
            # 이 파이프라인은 판정을 만들지 않는다 - 소비자가 착각하지 않게 계약으로 박는다.
            "authoritative": False,
            "produces_order_intent": False}


def run_bull_bear_debate(research_packet: dict, *, chat=None, uploader=None) -> dict:
    """본부 단독 실행 - 리스크/QA/리서치의 run_<dept>_department 와 같은 외부 인터페이스."""
    try:
        # tracing_context 는 enabled 를 건드리지 않는다 - LANGSMITH_TRACING 이 꺼져 있으면
        # 그대로 꺼진 채고, 켜져 있을 때만 트레이딩본부 Project 로 보낸다.
        with tracing_context(project_name=_ls_project()):
            state = build_pipeline(chat=chat, uploader=uploader).invoke(
                {"research_packet": research_packet, "fallbacks": []})
    except Exception as exc:
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

    citations, independence = out.get("citations") or {}, out.get("independence") or {}
    contested = out.get("contested") or {}
    lines += [
        "", "## 결정론 검증", "",
        "| 검사 | 결과 |",
        "|---|---|",
        f"| 색인에 없는 인용 (날조) | {_md_refs(citations.get('unknown_refs'))} |",
        f"| Bull 무인용 | {'**예**' if citations.get('bull_uncited') else '아니오'} |",
        f"| Bear 무인용 | {'**예**' if citations.get('bear_uncited') else '아니오'} |",
        f"| 독립성 위반 (문장 복제) | {_md_cell(independence.get('violations', 0))} |",
        "",
        "## 쟁점 대조 — TRD-03(PM)이 읽을 산출물",
        "",
        f"- **양측이 다툰 Claim:** {_md_refs(contested.get('contested_refs'))}",
        f"- **Bull 만 인용:** {_md_refs(contested.get('bull_only_refs'))}",
        f"- **Bear 만 인용:** {_md_refs(contested.get('bear_only_refs'))}",
        f"- **아무도 다루지 않은 Claim:** {_md_refs(contested.get('untouched_refs'))}",
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


def _run(packet, **kw):
    """자체 점검용 실행기 - uploader 를 반드시 스텁으로 채운다(네트워크 금지 규칙을
    검사마다 반복해 적는 대신 여기 한 곳에서 강제한다)."""
    kw.setdefault("uploader", _no_upload)
    return run_bull_bear_debate(packet, **kw)


def _stub(bull=_BULL_OK, bear=_BEAR_OK, capture=None):
    def chat(persona, task):
        # 역할 코드로 가른다 - Bull 페르소나도 "the Bear Researcher"를 언급하므로(병렬 독립
        # 선언) 이름 부분일치로 가르면 Bull 을 Bear 로 오인한다.
        who = "bear" if "(TRD-02)" in persona else "bull"
        if capture is not None:
            capture[who] = task
        payload = bear if who == "bear" else bull
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
    # 이 파이프라인의 존재 이유. Bear 에게 가는 payload 에 Bull 출력이 섞이면 실패한다.
    seen: dict = {}
    out = _run(_PACKET, chat=_stub(capture=seen))
    assert "bull" in seen and "bear" in seen, seen.keys()
    for distinctive in (_BULL_OK["bull_case"], _BULL_OK["upside_scenario"]):
        assert distinctive not in seen["bear"], "Bear task 에 Bull 출력이 샜다"
    assert seen["bull"] == _bull_task({"research_packet": _PACKET, "claims": out["claims"]})
    assert out["independence"]["violations"] == 0
    print("  Bear 독립성 (배선)         OK")


def _check_citation_guard():
    # 색인에 없는 Claim ID 를 인용하면 grounded 가 무너지고 escalate 된다.
    out = _run(_PACKET, chat=_stub(bear={**_BEAR_OK, "claim_refs": ["fact:0", "fact:99"]}))
    assert out["citations"]["unknown_refs"] == ["fact:99"], out["citations"]
    assert out["grounded"] is False and out["escalate"] is True
    # 인용이 아예 없어도 통과시키지 않는다
    out2 = _run(_PACKET, chat=_stub(bull={**_BULL_OK, "claim_refs": []}))
    assert out2["citations"]["bull_uncited"] is True and out2["grounded"] is False
    print("  인용 검증 (결정론)         OK")


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
        if "(TRD-02)" in persona:
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
    # 거부 사유가 다음 시도 프롬프트에 실려야 같은 실수를 반복하지 않는다
    assert "was rejected" in calls[-1] or "was rejected" in calls[-2]

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
    out = _run(_PACKET, chat=_stub())
    c = out["contested"]
    assert c["contested_refs"] == ["fact:0"]          # 양측이 같이 다툰 Claim
    assert c["bull_only_refs"] == ["fact:1"]
    assert c["bear_only_refs"] == ["invalid:0"]
    assert "catalyst:0" in c["untouched_refs"]        # 아무도 안 건드린 Claim 이 드러난다
    assert out["grounded"] is True and out["escalate"] is False
    assert out["produces_order_intent"] is False      # 이 본부 단계에서 주문은 안 만든다
    print("  쟁점 대조 + 판정 부재      OK")


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
    _check_secret_redaction()
    _check_langsmith_observability()
    print("트레이딩본부 토론 파이프라인 17개 영역 통과. 실행은 --run (Hermes + Notion 필요)")
