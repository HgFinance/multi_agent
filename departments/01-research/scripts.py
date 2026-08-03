#!/usr/bin/env python3
"""리서치본부 LangGraph 파이프라인 - run_research_department(symbol) -> Research Packet.

담당: 재일 (리서치/퀀트)
근거: 동규님의 QA 부서 패턴(departments/06-ai-qa-audit/scripts.py - LangGraph
      실무진 + 부서장 정제)을 우리 본부에 적용 (재일님 지시 2026-07-31).

QA 패턴과의 차이 - **노드가 프롬프트가 아니라 실구현 직원이다**:
  check_universe      universe_manager (결정론 - LS 공식 목록, 실전 검증 347/3)
  assemble_evidence   evidence/bundle.py 조립기 (읽기 전용 API 2종 + 결정론
                      price_context - qwen 이 +27% 급등을 하락으로 서술한 실측
                      사고 후 등락률 계산을 코드로 이관)
  analyze_sentiment   news_sentiment_analyst (로컬 LLM + 환각 검증, 10/10 실측)
  draft_packet        research-supervisor 페르소나 (hermes/config.yaml 원문 사용)

원칙 (전 노드 공통):
  - LLM 은 판단·서술만. 수치·필터·검증은 코드가 한다.
  - 에이전트는 DB 를 모른다 - research-api/market-api 만 호출한다.
  - 근거 부족은 insufficient_evidence 로 끝낸다. 지어내지 않는다.
  - LLM 주소는 환경변수(OLLAMA_BASE_URL)다 - 특정 PC 주소를 하드코딩하지 않는다.

실행:
  python scripts.py               # 자체 점검 (LLM·API 없음)
  python scripts.py --run 005490  # 실제 Packet 생성
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, TypedDict

_BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(_BASE))  # evidence 패키지 - 임포트 실행에서도 찾도록
sys.path.insert(0, str(_BASE / "collectors"))
sys.path.insert(0, str(_BASE / "agents"))

from langgraph.graph import END, StateGraph  # noqa: E402

from evidence.forecast import falsification_note  # noqa: E402
from evidence.highlights import pick_highlights  # noqa: E402
from evidence.highlights import render_line as render_highlights  # noqa: E402
from evidence.forecast import probability_for_claim  # noqa: E402
from evidence.llm_client import chat as llm_chat  # noqa: E402

PIPELINE_VERSION = "research-department-pipeline-v2"  # v2: 분석가 3인 통합 + 수치 가드
KST = timezone(timedelta(hours=9))

OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
# ▶ 총괄은 별도 엔드포인트를 쓸 수 있다 (2026-08-03)
#   실측: pipeline_runs 19회 중 10회가 총괄 스키마 이탈로 실패했다
#   (evidence_quality 에 문장을 넣거나, 키를 통째로 빠뜨리거나, 한 겹 싸거나).
#   qwen3:14b 가 6인 분석가 readout 을 종합하는 이 자리에서 반복적으로 무너진다.
#   RESEARCH_SUPERVISOR_BASE 를 Claude Code 프록시로 돌리면 그 자리만 올릴 수
#   있다 - 분석가 6인은 그대로 로컬 무료다(비용·한도 소모가 예측 가능하다).
SUPERVISOR_MODEL = os.environ.get("RESEARCH_SUPERVISOR_MODEL", "qwen3:14b")
SUPERVISOR_BASE = os.environ.get("RESEARCH_SUPERVISOR_BASE", OLLAMA_BASE).rstrip("/")
MARKET_API = os.environ.get("MARKET_API_URL", "http://127.0.0.1:8036")
RESEARCH_API = os.environ.get("RESEARCH_API_URL", "http://127.0.0.1:8035")


class ResearchState(TypedDict, total=False):
    symbol: str
    universe: dict          # 결정론 판정 결과
    evidence: dict          # Evidence Bundle (API 수집분)
    sentiment: dict         # 검증된 sentiment 요약
    technical: dict         # RES-04 기술적 소견 (결정론 readout + 검증된 서술)
    fundamental: dict       # RES-05 펀더멘털 소견 (결정론 readout + 검증된 서술)
    regime: dict            # RES-07 시장 레짐 (라벨은 코드, 서술만 LLM)
    geopolitical: dict      # RES-09 지정학 국면 (라벨·driver 는 코드, 서술만 LLM)
    microstructure: dict    # RES-03 미시구조 (판정은 코드, 서술만 LLM)
    packet: dict            # 최종 Research Packet
    halted: str             # 중단 사유 (거래 불가 등)


def _get(url: str):
    """파이프라인 공용 조회 - Evidence 조립 경로가 쓴다.

    페르소나는 큐레이터(RES-08)다. 뉴스·공시를 모아 분석가에게 넘기는
    큐레이션이 그 직무이고, 총괄은 허용목록에서 "자체 조회 없음"으로
    선언돼 있다(evidence/bundle.py 와 같은 판단).
    """
    sys.path.insert(0, str(_BASE / "evidence"))
    from api_client import get_json

    return get_json(url, persona="rag-librarian-evidence-curator", timeout=30)


# ── 노드 1: Universe 판정 (결정론 직원) ────────────────────────────────────
def check_universe(state: ResearchState) -> dict:
    from universe_manager import run as universe_run

    d = universe_run(basket=(state["symbol"],))
    if state["symbol"] in d.excluded:
        return {"universe": {"tradable": False, "reason": d.excluded[state["symbol"]]},
                "halted": f"거래 불가({d.excluded[state['symbol']]}) - 분석 중단"}
    return {"universe": {"tradable": True, "as_of": d.as_of.isoformat()}}


# ── 노드 2: Evidence Bundle 조립 (결정론 - evidence/bundle.py) ─────────────
def assemble_evidence(state: ResearchState) -> dict:
    # 수집·등락률 계산은 전부 모듈이 한다 - 노드는 배선만 (계약: 기존 키 유지)
    from evidence.bundle import assemble_bundle

    ev = assemble_bundle(state["symbol"], market_api=MARKET_API,
                         research_api=RESEARCH_API, get=_get)
    # RES-08 사서의 공시 원문 발췌(결정론 - 종목 링크 확인 문서의 머리 청크만).
    # 사서 조회 실패는 Packet 전체를 죽일 일이 아니다 - 미확인으로 명시한다.
    try:
        from rag_librarian import recent_excerpts_for_symbol

        ev["disclosure_excerpts"] = recent_excerpts_for_symbol(state["symbol"])
    except Exception as e:
        ev["disclosure_excerpts"] = {"status": "UNAVAILABLE",
                                     "reason": f"{type(e).__name__}: {e}"[:120]}
    return {"evidence": ev}


# ── 노드 3: Sentiment (검증된 LLM 직원) ────────────────────────────────────
def analyze_sentiment(state: ResearchState) -> dict:
    from news_sentiment_analyst import run as senti_run

    r = senti_run(state["symbol"], hours=24.0, read_bodies=False)
    return {"sentiment": {"verdict": r.verdict, "score": r.score,
                          "articles": r.articles_used,
                          "dropped": r.articles_dropped}}


# ── 노드 3b/3c: 기술적·펀더멘털 분석가 (검증된 LLM 직원 + 결정론 계산기) ──
def _norm_note(note):
    """분석가 note 가 pydantic 모델일 수 있다 - 프롬프트 직렬화 가능하게 통일."""
    return note.model_dump() if hasattr(note, "model_dump") else note


def analyze_technical(state: ResearchState) -> dict:
    from technical_analyst import analyze as tech_analyze

    r = tech_analyze(state["symbol"], market_api=MARKET_API)
    return {"technical": {"verdict": r.get("verdict"),
                          "readout": r.get("readout"),
                          "note": _norm_note(r.get("note")),
                          "llm_status": r.get("llm_status"),
                          "reason": r.get("reason")}}


def analyze_fundamental(state: ResearchState) -> dict:
    from fundamental_analyst import analyze as fund_analyze

    r = fund_analyze(state["symbol"])
    return {"fundamental": {"verdict": r.get("verdict"),
                            "readout": r.get("readout"),
                            "note": _norm_note(r.get("note")),
                            "verification": r.get("verification")}}


def analyze_regime(state: ResearchState) -> dict:
    """시장 단면 레짐 - 종목이 아니라 시장 전체의 맥락 (RES-07)."""
    from sector_regime_analyst import analyze as regime_analyze

    r = regime_analyze(market_api=MARKET_API)
    return {"regime": {"verdict": r.get("verdict"),
                       "readout": r.get("readout"),
                       "note": _norm_note(r.get("note"))}}


def analyze_geopolitical(state: ResearchState) -> dict:
    """지정학 국면 - 종목도 시장 단면도 아닌 **바깥 환경** (RES-09).

    레짐(RES-07)이 국내 시장 내부 단면이라면 이쪽은 외생 충격이다. 둘 다
    종목 무관이라 결과가 종목별로 달라지지 않는다 - 그래도 매 Packet 마다
    부르는 이유는 as_of 시점 국면이 Packet 의 맥락이기 때문이다.
    """
    from geopolitical_analyst import analyze as geo_analyze

    r = geo_analyze(research_api=RESEARCH_API)
    return {"geopolitical": {"verdict": r.get("verdict"),
                             "driver": r.get("driver"),
                             "readout": r.get("readout"),
                             "note": _norm_note(r.get("note")),
                             "summary": r.get("summary"),
                             "transmission": r.get("transmission"),
                             "cautions": r.get("cautions"),
                             "llm_status": r.get("llm_status"),
                             "reason": r.get("reason")}}


def analyze_microstructure(state: ResearchState) -> dict:
    from microstructure_analyst import analyze as micro_analyze

    r = micro_analyze(state["symbol"], market_api=MARKET_API)
    return {"microstructure": {"verdict": r.get("verdict"),
                               "readout": r.get("readout"),
                               "note": _norm_note(r.get("note"))}}


PACKET_REQUIRED_KEYS = ("symbol", "thesis", "facts", "interpretation",
                        "invalidation", "evidence_quality")


def unwrap_packet(obj):
    """총괄이 Packet 을 한 겹 감싸 냈으면 벗긴다.

    실측 2026-08-02·08-03: 두 실행이 `missing keys: [전부]` 로 죽었다. 키가
    하나도 없다는 것은 형태가 조금 어긋난 게 아니라 **다른 층을 봤다**는
    뜻이고, 소형·중형 모델의 가장 흔한 이탈이 {"packet": {...}} 처럼 한 겹
    싸는 것이다. 거부하면 분석가 6인을 돌린 2~3분이 통째로 버려지므로,
    확실히 벗길 수 있을 때만 벗긴다 - **추측으로 구조를 바꾸지 않는다.**

    벗기는 조건: 값이 dict 이고 그 안에 필수 키가 **전부** 있을 때만.
    (일부만 있으면 그건 다른 문제이므로 건드리지 않는다.)
    """
    if not isinstance(obj, dict):
        return obj
    if all(k in obj for k in PACKET_REQUIRED_KEYS):
        return obj
    for v in obj.values():
        if isinstance(v, dict) and all(k in v for k in PACKET_REQUIRED_KEYS):
            return v
    return obj


def flatten_to_str_list(v) -> Optional[list[str]]:
    """총괄이 낸 값을 문자열 목록으로 평탄화. 불가능하면 None(재시도).

    실측 2026-08-02: 관통 4회가 모두 총괄 스키마 이탈로 죽었고 매번 모양이
    달랐다. 거부만 하면 분석가 6인을 돌린 2~3분이 버려지므로, **모양은 코드가
    바로잡고 내용 검증은 그대로 돌린다**(수치 재대조·라벨 가드는 평탄화된
    문자열에 그대로 걸리므로 오히려 검사가 더 잘 보게 된다).

    지어내지 않는 것이 원칙이라 값이 없으면 빈 목록이 아니라 None 을 낸다 -
    빈 목록은 '내용이 없다'는 사실을 만들어내는 것이다.
    """
    def one(x) -> str:
        if isinstance(x, str):
            return x.strip()
        if isinstance(x, dict):
            # {"claim": "...", "source": "..."} -> "claim: ... | source: ..."
            return " | ".join(f"{k}: {one(val)}" for k, val in x.items())
        if isinstance(x, (list, tuple)):
            return " ; ".join(one(i) for i in x)
        return str(x)

    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return [s] if s else None
    if isinstance(v, dict):
        out = [f"{k}: {one(val)}" for k, val in v.items()]
    elif isinstance(v, (list, tuple)):
        out = [one(x) for x in v]
    else:
        return None
    out = [s for s in (x.strip() for x in out) if s]
    return out or None


# ── 노드 4: Packet 초안 (supervisor 페르소나 - config.yaml 원문) ───────────
def _supervisor_persona() -> str:
    cfg = (_BASE / "hermes" / "config.yaml").read_text(encoding="utf-8")
    return re.search(r'research-supervisor: "(.*?)"\n', cfg, re.S).group(1)


def draft_packet(state: ResearchState, *, llm=None) -> dict:
    evidence = dict(state["evidence"])
    # 가격 수치는 코드가 계산한 확정값 - qwen 이 +27% 급등을 하락으로 서술한
    # 실측 사고 재발 방지로, LLM 이 재계산하지 못하게 별도 블록으로 분리 주입
    price_ctx = evidence.pop("price_context", None) or {
        "status": "UNAVAILABLE", "reason": "evidence 에 price_context 가 없다"}
    bundle = {"symbol": state["symbol"], "universe": state["universe"],
              "sentiment": state["sentiment"], **evidence}
    # 분석가 소견 - 각자의 결정론 readout 과 검증 통과한 서술만 온다
    analysts = {
        "technical": state.get("technical") or {"status": "NOT_RUN"},
        "fundamental": state.get("fundamental") or {"status": "NOT_RUN"},
        "market_regime": state.get("regime") or {"status": "NOT_RUN"},
        "geopolitical": state.get("geopolitical") or {"status": "NOT_RUN"},
        "microstructure": state.get("microstructure") or {"status": "NOT_RUN"},
    }

    # 프롬프트에는 압축 다이제스트만 - 실측: 원자료(중첩 dict)를 통째로 주면
    # 모델이 출력 구조까지 dict 로 따라가 스키마(list[str])를 어긴다.
    # 수치 재대조(confirmed)는 아래에서 원본 analysts 로 한다 - 상위집합 허용.
    def _digest(a: dict) -> dict:
        """분석가 결과 -> 총괄 프롬프트용 압축본. **상한이 있는 것이 핵심이다.**

        실측 2026-08-02(분석가 6인 확장): 다이제스트가 커지자 qwen3 의 <think>
        가 길어져 JSON 끝이 잘렸다("missing keys: ['invalidation']" - 마지막
        키만 사라지는 전형적 절단). 분석가를 늘릴 때마다 총괄이 깨지면 확장이
        불가능하므로, 분석가 수와 무관하게 프롬프트 크기가 상한을 갖게 한다.
        버려지는 원문은 리포트의 '분석가 소견 원문'에 그대로 남는다.
        """
        if not a or a.get("status") == "NOT_RUN":
            return {"status": "NOT_RUN"}
        # 분석가마다 서술 위치가 다르다 - note.summary(기술·펀더멘털·레짐·
        # 미시구조) 와 최상위 summary(지정학). 한쪽만 보면 RES-09 다이제스트가
        # 통째로 null 이 되어 총괄이 맥락 없이 쓴다(2026-08-02 실측).
        note = a.get("note") or {}
        summary = (note.get("summary") or a.get("summary") or "")[:DIGEST_SUMMARY_CHARS]
        cautions = [str(c)[:DIGEST_CAUTION_CHARS]
                    for c in (note.get("cautions") or a.get("cautions") or [])
                    ][:DIGEST_MAX_CAUTIONS]
        nums = {k: v for k, v in (a.get("readout") or {}).items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)}
        if len(nums) > DIGEST_MAX_NUMBERS:      # 앞에서부터 - readout 은 중요도순 정의다
            nums = dict(list(nums.items())[:DIGEST_MAX_NUMBERS])
        return {"verdict": a.get("verdict"), "summary": summary,
                "cautions": cautions, "key_numbers": nums}

    analysts_digest = {k: _digest(v) for k, v in analysts.items()}
    task = f"""Using ONLY the evidence below, draft a compact Research Packet in Korean.
Schema (JSON only):
{{"symbol": "...", "thesis": "1-2 sentences in Korean, facts first",
 "facts": ["cited facts only - reference ids like news:3"],
 "interpretation": ["clearly separated from facts"],
 "catalysts": ["upcoming checkpoints"],
 "invalidation": ["conditions that would kill the thesis"],
 "evidence_quality": "sufficient | partial | insufficient_evidence"}}
STRUCTURE RULES - non-negotiable:
- facts, interpretation, catalysts, invalidation MUST each be a FLAT JSON
  array of plain strings. No nested objects, no key-value maps.
- evidence_quality is exactly one of the three lowercase tokens above.
- Write all narrative text in Korean.

CONFIRMED PRICE FIGURES (deterministic, computed by code - NOT by you):
{json.dumps(price_ctx, ensure_ascii=False, indent=1)}
Rules for these figures - non-negotiable:
- Quote them verbatim. Do NOT recompute returns or infer price direction
  yourself from headlines or any other source.
- change_1d_pct > 0 means the price ROSE (상승); < 0 means it FELL (하락).
  Follow the sign exactly.
- If status is UNAVAILABLE or a field is null, write "미확인" - never guess.

CONFIRMED ANALYST FINDINGS (readouts computed by code; narratives already
hallucination-checked - NOT yours to recompute):
{json.dumps(analysts_digest, ensure_ascii=False, indent=1)}
Rules for analyst findings - non-negotiable:
- Quote their verdicts and numbers verbatim; do not invent new figures.
- If analysts DISAGREE (e.g., sentiment positive but technical BEARISH),
  preserve BOTH views side by side in interpretation. Never delete dissent,
  never average it away - state the conflict explicitly.
- NOT_RUN / INSUFFICIENT_DATA / null means 미확인 - never fill the gap.

Evidence:
{json.dumps(bundle, ensure_ascii=False, indent=1)}"""

    call = llm or _ollama_chat
    REQUIRED = ("symbol", "thesis", "facts", "interpretation", "invalidation",
                "evidence_quality")
    packet = None
    last_err = None
    repair = ""      # 실패 유형별 교정 지시 - 일반 문구만으로는 같은 실수를 반복한다
    for attempt in (1, 2, 3):   # 파싱·스키마 위반 재시도 - 실측: 토큰 이탈(high)이 2회 연속도 나온다
        extra = "" if attempt == 1 else (
            f"\n\nYour previous reply was rejected ({last_err}). Return ONLY the "
            f"JSON object with ALL required keys: {', '.join(REQUIRED)}.{repair}")
        out = re.sub(r"<think>.*?</think>", "",
                     call(_supervisor_persona(), task + extra), flags=re.S)
        s, e = out.find("{"), out.rfind("}")
        if s < 0 or e < s:
            last_err = "no JSON object in reply"
            continue
        try:
            candidate = json.loads(out[s:e + 1])
        except json.JSONDecodeError as err:
            last_err = f"invalid JSON: {err}"
            continue
        candidate = unwrap_packet(candidate)
        missing = [k for k in REQUIRED if k not in candidate]
        if missing:
            last_err = f"missing keys: {missing}"
            # **원문을 남긴다.** 예전에는 사유만 남아 다음 실패 때 무엇이
            # 왔는지 알 방법이 없었다(실측: 08-02 01:22 과 08-03 00:10 이 같은
            # 문구로 죽었는데 둘 다 원인을 못 봤다). 분석가 6인을 2~3분 돌린
            # 뒤에 죽는 자리라 재현 비용이 비싸다 - 그때 본 것을 그때 남긴다.
            _last_raw = out[s:e + 1][:600]
            print(f"⚠ 총괄 스키마 이탈({attempt + 1}회) - 받은 최상위 키: "
                  f"{sorted(candidate)[:12]} / 원문 앞부분: {_last_raw[:220]}",
                  file=sys.stderr, flush=True)
            repair = ('\nReturn a SINGLE flat JSON object whose TOP-LEVEL keys are '
                      'exactly: symbol, thesis, facts, interpretation, invalidation, '
                      'evidence_quality. Do NOT wrap it in another object such as '
                      '{"packet": {...}} or {"result": {...}}.')
            continue
        # 타입 - **거부가 아니라 교정한다.** 실측 2026-08-02: 관통 실행 4회가
        # 전부 여기서 죽었는데 매번 이탈 모양이 달랐다(facts 가 dict, 키 누락,
        # 토큰 이탈). 그때마다 **분석가 6인을 돌린 2~3분이 통째로 버려진다.**
        # 중첩을 거부한 원래 이유는 "항목 안에 창작 수치가 숨어 검사를 우회"
        # 였는데, 평탄화하면 오히려 수치 검사가 전부 보게 되므로 교정이 더
        # 안전하다. 무엇을 고쳤는지는 Packet 에 남겨 사람이 볼 수 있게 한다.
        coerced: list[str] = []
        bad_key = None
        for k in ("facts", "interpretation", "invalidation"):
            v = candidate.get(k)
            if isinstance(v, list) and all(isinstance(x, str) for x in v):
                continue
            flat = flatten_to_str_list(v)
            if flat is None:
                bad_key = k
                break
            candidate[k] = flat
            coerced.append(k)
        if bad_key is not None:
            last_err = f"{bad_key} 를 문자열 목록으로 바꿀 수 없다"
            repair = (f'\n"{bad_key}" must be a FLAT array of plain Korean '
                      f'sentences, e.g. ["첫 문장.", "둘째 문장."] - never an '
                      f'object, never nested.')
            continue
        candidate["_schema_coerced"] = coerced
        eq = str(candidate.get("evidence_quality", "")).lower().strip()
        if eq not in ("sufficient", "partial", "insufficient_evidence"):
            last_err = f"evidence_quality must be one of the 3 tokens, got {eq[:40]!r}"
            # 실측 2026-08-01·08-02: qwen3 이 'high'/'good' 같은 제 어휘를 3회
            # 연속 낸다. 일반 문구로는 안 고쳐진다 - 무엇을 냈고 무엇으로
            # 바꿔야 하는지 지목하고, 뜻이 가까운 토큰까지 알려준다.
            near = ("sufficient" if eq in ("high", "good", "strong", "ok")
                    else "insufficient_evidence" if eq in ("low", "poor", "none")
                    else "partial")
            repair = (f'\n"evidence_quality" must be EXACTLY one of these three '
                      f'lowercase strings: "sufficient", "partial", '
                      f'"insufficient_evidence". You wrote {eq[:20]!r}, which is '
                      f'not allowed. If you meant that, write "{near}".')
            continue
        candidate["evidence_quality"] = eq
        packet = candidate
        break
    if packet is None:
        # 결정론 가드: 두 번 다 스키마를 못 지키면 초안을 거부한다 - 통과 위장 금지
        raise ValueError(f"Packet 초안 거부 - {last_err}")
    # 결정론 가드 2: 서술 속 % 수치가 확정치(가격+분석가 readout) 밖이면 창작이다.
    # 대상은 **사실 서술**(thesis/facts/interpretation)만 - invalidation·catalysts
    # 는 모델이 제안하는 미래 조건("성장률 10% 미달 시")이라 확정치에 없는 것이
    # 정상이다(실측 2026-08-01: 제안 임계값 오탐). 중첩 우회 방지로 해당 키들을
    # 통째로 직렬화한다.
    from evidence.bundle import verify_narrative_numbers

    narrative = json.dumps({k: packet.get(k) for k in
                            ("thesis", "facts", "interpretation")},
                           ensure_ascii=False)
    packet["numeric_check"] = verify_narrative_numbers(
        narrative, {"price": price_ctx, "analysts": analysts})

    # 결정론 가드 3: 사실 서술에 확정치 밖 수치가 있으면 evidence_quality 를
    # **강등**한다. 실측 2026-08-02: 총괄이 매출 +18.3%·영업이익 +12.7% 를
    # 지어내 facts 에 출처까지 달아 넣었는데(분석가 실제값은 46.76%/101.16%)
    # 검사는 불일치를 표시만 하고 evidence_quality 는 sufficient 로 나갔다.
    # 표시만 하는 가드는 하류(트레이딩)에서 읽히지 않는다 - 등급을 낮춰야
    # 계약이 된다. 낮추기만 하고 올리지는 않는다(위험 방향 fail-closed).
    if not packet["numeric_check"].get("ok"):
        order = ("insufficient_evidence", "partial", "sufficient")
        cur = packet["evidence_quality"]
        if order.index(cur) > order.index("partial"):
            packet["evidence_quality"] = "partial"
            packet["numeric_check"]["downgraded_from"] = cur
    return {"packet": packet}


SUPERVISOR_TEMPERATURE = 0.2   # 총괄은 분석가(0.1)보다 조금 느슨하게 종합한다
SUPERVISOR_TIMEOUT = 600       # 14b + <think> - 분석가보다 훨씬 길다
# 실측: 기본값에서 JSON 이 잘려 스키마 위반 -> 4096 -> 분석가 6인 확장 후
# <think> 가 길어져 또 잘림(마지막 키 invalidation 소실)이라 8192.
# 입력 상한(_digest)과 함께 쓴다 - 한쪽만으로는 확장할 때마다 다시 깨진다.
SUPERVISOR_MAX_TOKENS = 8192


def _ollama_chat(system: str, user: str) -> str:
    """호출 모양은 evidence/llm_client 가 단일 출처다. 여기 남는 것은 총괄의
    설정뿐 - 이 셋만 분석가와 다를 이유가 실제로 있다."""
    return llm_chat(system, user, base=SUPERVISOR_BASE, model=SUPERVISOR_MODEL,
                    timeout=SUPERVISOR_TIMEOUT,
                    temperature=SUPERVISOR_TEMPERATURE,
                    max_tokens=SUPERVISOR_MAX_TOKENS)


# ── 그래프 조립 ────────────────────────────────────────────────────────────
def build_pipeline():
    g = StateGraph(ResearchState)
    g.add_node("check_universe", check_universe)
    g.add_node("assemble_evidence", assemble_evidence)
    g.add_node("analyze_sentiment", analyze_sentiment)
    g.add_node("analyze_technical", analyze_technical)
    g.add_node("analyze_fundamental", analyze_fundamental)
    g.add_node("analyze_regime", analyze_regime)
    g.add_node("analyze_geopolitical", analyze_geopolitical)
    g.add_node("analyze_microstructure", analyze_microstructure)
    g.add_node("draft_packet", draft_packet)
    g.set_entry_point("check_universe")
    # 거래 불가면 즉시 종료 - 죽은 종목에 분석 비용을 쓰지 않는다
    g.add_conditional_edges("check_universe",
                            lambda s: "END" if s.get("halted") else "go",
                            {"END": END, "go": "assemble_evidence"})
    # 분석가 6인은 순차다 - GPU 하나에 모델 하나(agent-research 공유)라
    # LLM 호출은 어차피 직렬화된다. 형태만 병렬로 꾸미지 않는다.
    g.add_edge("assemble_evidence", "analyze_sentiment")
    g.add_edge("analyze_sentiment", "analyze_technical")
    g.add_edge("analyze_technical", "analyze_fundamental")
    g.add_edge("analyze_fundamental", "analyze_regime")
    # 레짐(국내 단면) 다음에 지정학(외생 환경) - 안에서 밖으로 넓히는 순서
    g.add_edge("analyze_regime", "analyze_geopolitical")
    g.add_edge("analyze_geopolitical", "analyze_microstructure")
    g.add_edge("analyze_microstructure", "draft_packet")
    g.add_edge("draft_packet", END)
    return g.compile()


def _node_models() -> dict:
    """노드별 실사용 모델 - 각 에이전트 모듈의 MODEL 상수에서 직접 읽는다
    (env 기본값을 여기 복제하면 배정 변경 시 어긋난다)."""
    models = {"supervisor": SUPERVISOR_MODEL}
    for node, mod in (("sentiment", "news_sentiment_analyst"),
                      ("technical", "technical_analyst"),
                      ("fundamental", "fundamental_analyst"),
                      ("regime", "sector_regime_analyst"),
                      ("geopolitical", "geopolitical_analyst"),
                      ("microstructure", "microstructure_analyst")):
        try:
            models[node] = getattr(__import__(mod), "MODEL", None)
        except Exception:
            models[node] = None
    return models


# ── 선순환 1단: 반증 가능한 주장 발행 (코드가 만든다) ─────────────────────
# 재일님 지시 2026-08-01 "실제 투자 판단 -> 결과에 영향을 줄 때 선순환".
# Packet 의 산문 무효화 조건("연계성 약화 가능성")은 사람도 판정이 갈려
# 채점되지 않는다. 채점 가능한 주장은 코드가 결정론으로 발행한다 -
# LLM 에게 자기 채점 기준을 쓰게 두면 맞히기 쉬운 조건 쪽으로 굽는다.
# 총괄 프롬프트 상한 - 분석가를 늘려도 총괄이 깨지지 않게 하는 방어선
DIGEST_SUMMARY_CHARS = 240
DIGEST_CAUTION_CHARS = 140
DIGEST_MAX_CAUTIONS = 2
DIGEST_MAX_NUMBERS = 10

CLAIM_HORIZONS = (5, 20)          # 거래일. 5일=단기 반응, 20일=한 달 검증
DRAWDOWN_PCT = 10.0               # 기준가 대비 하락 발동선
RALLY_PCT = 10.0


def build_packet_claims(state: dict, packet: dict) -> list[dict]:
    """Packet 시점 상태 -> 기계가 채점할 수 있는 주장 목록 (순수 함수).

    기준가가 없으면 가격 주장을 만들지 않는다 - 기준 없는 채점은 무의미하고,
    없는 기준을 추정해 넣으면 통계 전체가 오염된다.
    """
    claims: list[dict] = []
    price = ((state.get("evidence") or {}).get("price_context") or {})
    # 키 이름은 bundle.compute_price_context 계약이다("last_close"). 실측
    # 2026-08-02: "close" 로 잘못 읽어 가격 주장이 통째로 발행되지 않았고,
    # 그런데도 조용히 넘어갔다 - 그래서 아래 자체점검이 이 키를 못 박는다.
    base = price.get("last_close")
    try:
        base = float(base) if base is not None else None
    except (TypeError, ValueError):
        base = None

    if base and base > 0:
        for h in CLAIM_HORIZONS:
            claims.append({"kind": "PRICE_DRAWDOWN", "metric": "close", "op": "<=",
                           "threshold": round(base * (1 - DRAWDOWN_PCT / 100), 4),
                           "baseline": base, "horizon_days": h,
                           "source_node": "price_context"})
            claims.append({"kind": "PRICE_RALLY", "metric": "close", "op": ">=",
                           "threshold": round(base * (1 + RALLY_PCT / 100), 4),
                           "baseline": base, "horizon_days": h,
                           "source_node": "price_context"})

    regime = (state.get("regime") or {}).get("verdict")
    if regime and regime != "INSUFFICIENT_DATA":
        claims.append({"kind": "REGIME_FLIP", "metric": "regime_label", "op": "!=",
                       "threshold_text": regime, "baseline_text": regime,
                       "horizon_days": 20, "source_node": "regime"})

    geo = (state.get("geopolitical") or {}).get("verdict")
    if geo and geo not in ("INSUFFICIENT_DATA", "SHOCK"):
        # 이미 SHOCK 이면 "SHOCK 으로 악화"는 발동 불가라 주장이 되지 않는다
        claims.append({"kind": "GEO_ESCALATION", "metric": "geo_risk_label",
                       "op": "==", "threshold_text": "SHOCK",
                       "baseline_text": geo, "horizon_days": 20,
                       "source_node": "geopolitical"})
    # ▶ 사전 확률·반증 조건을 붙인다 (2026-08-03, P0)
    #   확률이 없으면 Brier Score·Calibration Error 를 원리적으로 못 센다 -
    #   발동 여부만 세면 과신하는 분석가와 소심한 분석가가 구분되지 않는다.
    #   **코드가 낸다** - LLM 이 자기 확률을 쓰면 맞히기 쉬운 쪽으로 굽는다
    #   (origin='code' 와 같은 이유).
    closes = price.get("closes") or []
    for c in claims:
        prob, method = probability_for_claim(c, closes)
        if prob is not None:
            c["probability"] = prob
            c["probability_method"] = method
            c["method_key"] = method
        c["falsification_note"] = falsification_note(c)
    return claims


def _record_packet_claims(*, trace_id: str, symbol: str, claims: list[dict],
                          conn=None, as_known_at=None) -> int:
    """research.packet_claims 적재. 기록 실패가 Packet 을 죽이지 않는다.

    as_known_at 은 이 주장을 발행할 때의 증거 컷오프다 - 사후 채점이
    hindsight 로 오염되지 않았는지 확인하는 기준이라 주장과 함께 남긴다.
    """
    if not claims:
        return 0
    try:
        own = conn is None
        if own:
            import psycopg2

            from source_registry import load_project_env

            conn = psycopg2.connect(load_project_env()["DATABASE_URL"],
                                    connect_timeout=10)
        with conn.cursor() as cur:
            for c in claims:
                cur.execute(
                    """
                    insert into research.packet_claims
                      (trace_id, symbol, kind, metric, op, threshold,
                       threshold_text, baseline, baseline_text, horizon_days,
                       source_node, as_known_at, probability,
                       probability_method, method_key, falsification_note)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s)
                    on conflict (trace_id, kind, metric, horizon_days) do nothing
                    """,
                    (trace_id, symbol, c["kind"], c["metric"], c["op"],
                     c.get("threshold"), c.get("threshold_text"),
                     c.get("baseline"), c.get("baseline_text"),
                     c["horizon_days"], c.get("source_node"), as_known_at,
                     c.get("probability"), c.get("probability_method"),
                     c.get("method_key"), c.get("falsification_note")))
        conn.commit()
        if own:
            conn.close()
        return len(claims)
    except Exception as e:
        print(f"⚠ packet_claims 기록 실패(Packet 은 정상): {type(e).__name__}: {e}",
              file=sys.stderr)
        return 0


def _record_pipeline_run(*, symbol: str, trace_id: str, started, ended,
                         status: str, packet: dict | None = None,
                         halt_reason: str | None = None, conn=None,
                         analyst_verdicts: dict | None = None) -> str | None:
    """research.pipeline_runs 기록 - 기록 실패가 Packet 을 죽이면 안 된다.

    audit.agent_runs 는 workforce 프로필 FK(HR 권한)가 선행이라 우리 소유
    스키마에 기록한다(마이그레이션 001300 주석). 좌표·지문만 남긴다.
    """
    import hashlib

    models = _node_models()
    input_hash = hashlib.sha256(
        f"{symbol}|{PIPELINE_VERSION}|{json.dumps(models, sort_keys=True)}".encode()
    ).hexdigest()
    packet_hash = None if packet is None else hashlib.sha256(
        json.dumps(packet, sort_keys=True, ensure_ascii=False,
                   default=str).encode()).hexdigest()
    nc = (packet or {}).get("numeric_check") or {}
    try:
        own = conn is None
        if own:
            import psycopg2

            from source_registry import load_project_env

            conn = psycopg2.connect(load_project_env()["DATABASE_URL"],
                                    connect_timeout=10)
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into research.pipeline_runs
                  (trace_id, pipeline_version, symbol, input_hash, node_models,
                   packet_hash, evidence_quality, numeric_check_ok, status,
                   halt_reason, started_at, ended_at, as_known_at, metadata)
                values (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s::jsonb)
                returning pipeline_run_id
                """,
                (trace_id, PIPELINE_VERSION, symbol, input_hash,
                 json.dumps(models), packet_hash,
                 (packet or {}).get("evidence_quality"),
                 nc.get("ok") if nc else None, status, halt_reason,
                 started, ended,
                 # as_known_at = 증거 컷오프. 이 실행이 "무엇까지 알 수 있었나"
                 # 의 경계다. 지금은 진입 시각(started)이 그 경계다 - 분석가
                 # 일부가 아직 as_of 를 안 받아 최신을 보기 때문이다.
                 # **기록이 먼저다**: 기록이 없으면 무엇이 파라미터화됐는지조차
                 # 확인할 수 없고, 사후 채점이 hindsight 로 오염됐는지도 모른다.
                 started,
                 # 판정 스냅샷 - 사후 채점(packet_outcome_scorer)이 "이 분석가가
                 # 그때 뭐라고 했는지"를 여기서 읽는다. 없으면 되먹임 통계
                 # (analyst_calibration)가 통째로 비어 선순환이 끊긴다.
                 json.dumps({"analyst_verdicts": analyst_verdicts or {}},
                            ensure_ascii=False)))
            run_id = str(cur.fetchone()[0])
        conn.commit()
        if own:
            conn.close()
        return run_id
    except Exception as e:
        print(f"⚠ pipeline_runs 기록 실패(Packet 은 정상): {type(e).__name__}: {e}",
              file=sys.stderr)
        return None


def run_research_department(symbol: str) -> dict:
    """본부 단독 실행 - QA 부서의 run_qa_department 와 같은 외부 인터페이스."""
    import uuid

    trace_id = str(uuid.uuid4())
    started = datetime.now(timezone.utc)
    try:
        out = build_pipeline().invoke({"symbol": symbol})
    except Exception as e:
        _record_pipeline_run(symbol=symbol, trace_id=trace_id, started=started,
                             ended=datetime.now(timezone.utc), status="FAILED",
                             halt_reason=f"{type(e).__name__}: {e}"[:200])
        raise
    ended = datetime.now(timezone.utc)
    if out.get("halted"):
        _record_pipeline_run(symbol=symbol, trace_id=trace_id, started=started,
                             ended=ended, status="HALTED",
                             halt_reason=str(out["halted"])[:200])
        return {"symbol": symbol, "verdict": "HALTED", "reason": out["halted"],
                "trace_id": trace_id}
    packet = out["packet"]
    packet["trace_id"] = trace_id
    # 증거 컷오프. 진입 시각을 그대로 쓴다 - 분석가 일부가 아직 as_of 를 안 받아
    # 최신을 보므로 '이 시각 이후 관측은 안 썼다'가 아니라 '이 시각에 실행했다'가
    # 지금의 정확한 뜻이다. 그래도 기록해야 나중에 무엇이 파라미터화됐는지
    # 확인할 수 있고, 사후 채점이 hindsight 인지 아닌지도 이 값으로 따진다.
    as_known_at = started
    packet["as_known_at"] = as_known_at.isoformat()
    packet["_analyst_verdicts"] = {          # 리포트용 메타 (Packet 본문과 분리)
        "sentiment": (out.get("sentiment") or {}).get("verdict"),
        "technical": (out.get("technical") or {}).get("verdict"),
        "fundamental": (out.get("fundamental") or {}).get("verdict"),
        "regime": (out.get("regime") or {}).get("verdict"),
        "geopolitical": (out.get("geopolitical") or {}).get("verdict"),
        "microstructure": (out.get("microstructure") or {}).get("verdict"),
    }
    _record_pipeline_run(
        symbol=symbol, trace_id=trace_id, started=started, ended=ended,
        status="COMPLETED", packet=packet,
        analyst_verdicts={k: v for k, v in packet["_analyst_verdicts"].items() if v})
    # 분석가 서술 - 리포트를 풍성하게. 검증(환각·수치·라벨)을 통과한 문장만 온다
    # ▶ 핵심 수치는 **코드가** 싣는다 (2026-08-03 실측 개선)
    #   계측 결과: 미시구조 재료 25개 중 LLM 이 3개만 인용(12%), 지정학 18개 중
    #   3개(18%). 서술이 "2~3문장 600자"로 묶여 있어 애초에 다 못 쓴다 -
    #   모델을 탓할 문제가 아니다. 그런데 Packet 에는 summary 만 실리므로
    #   나머지 22개는 계산해놓고 버려졌다. 무엇이 중요한 재료인지도 판정의
    #   일부이므로 코드가 골라 그대로 싣는다(판정은 코드, 서술만 LLM).
    packet["_analyst_notes"] = {
        node: {"summary": ((out.get(key) or {}).get("note") or {}).get("summary")
                          or (out.get(key) or {}).get("summary"),
               "cautions": ((out.get(key) or {}).get("note") or {}).get("cautions")
                           or (out.get(key) or {}).get("cautions") or [],
               "highlights": pick_highlights(
                   (out.get(key) or {}).get("readout"),
                   flags=((out.get(key) or {}).get("readout") or {}).get("flags") or [])}
        for node, key in (("technical", "technical"),
                          ("fundamental", "fundamental"),
                          ("regime", "regime"),
                          ("geopolitical", "geopolitical"),
                          ("microstructure", "microstructure"))
    }
    # 선순환 1단: 반증 가능한 주장을 발행해 남긴다. 채점은 지평이 지난 뒤
    # collectors/packet_outcome_scorer.py 가 시세로 대조한다.
    claims = build_packet_claims(out, packet)
    packet["_claims"] = claims
    packet["_claims_recorded"] = _record_packet_claims(
        trace_id=trace_id, symbol=symbol, claims=claims,
        as_known_at=as_known_at)
    return packet


def _render_packet_md(packet: dict, *, now: Optional[str] = None) -> str:
    """Packet 을 그대로 옮겨 적는 순수 함수 - 동규님 리스크본부 리포트 패턴
    (departments/03-risk/scripts.py _render_report_md, 2026-08-01) 채택.
    LLM 이 리포트 구조·내용을 창작하지 않는다. now 주입은 자체점검 결정성용."""
    nc = packet.get("numeric_check") or {}
    verdicts = packet.get("_analyst_verdicts") or {}
    lines = [
        "# 리서치본부 — Research Packet (결정론적 생성, LLM 자유 서술 아님)",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        f"| **symbol** | `{packet.get('symbol')}` |",
        f"| **evidence_quality** | **{packet.get('evidence_quality')}** |",
        f"| **수치 재대조** | {'통과' if nc.get('ok') else '⚠ 불일치 ' + str(nc.get('unmatched', []) + nc.get('unmatched_counts', []))} "
        f"(% {nc.get('checked', 0)}건 / 셈단위 {nc.get('checked_counts', 0)}건) |",
        f"| **분석가 판정** | " + " · ".join(
            f"{k}={v}" for k, v in verdicts.items() if v) + " |",
        f"| **생성** | {PIPELINE_VERSION}, {now or datetime.now(timezone.utc).isoformat()} |",
        "",
        "## Thesis", "", str(packet.get("thesis", "")), "",
    ]
    for title, key in (("사실 (facts)", "facts"),
                       ("해석 (interpretation)", "interpretation"),
                       ("촉매 (catalysts)", "catalysts"),
                       ("무효화 조건 (invalidation)", "invalidation")):
        lines += [f"## {title}", ""]
        lines += [f"- {x}" for x in (packet.get(key) or [])] or ["- (없음)"]
        lines += [""]

    # 분석가 원문 소견 - 총괄이 압축하며 버린 맥락이 여기 남는다. 상충하는
    # 소견을 지우지 않는 것이 본부 원칙이라, 요약본 옆에 원문을 같이 싣는다.
    # 서술이 없어도(LLM 실패·침묵) **핵심 수치가 있으면 싣는다.** 예전에는
    # summary 가 없으면 그 분석가를 통째로 뺐는데, 그러면 결정론 계산 결과까지
    # 같이 사라진다 - LLM 이 죽었다고 코드가 만든 재료를 버릴 이유가 없다
    # (실측 2026-08-03: 5인 중 3인이 이 필터에 걸려 리포트에서 사라졌다).
    notes = {k: v for k, v in (packet.get("_analyst_notes") or {}).items()
             if v.get("summary") or (v.get("highlights") or {}).get("items")}
    if notes:
        lines += ["## 분석가 소견 원문 (검증 통과분)", ""]
        for node, n in notes.items():
            verdict = verdicts.get(node)
            lines += [f"### {node}" + (f" — `{verdict}`" if verdict else ""), ""]
            lines += ([str(n["summary"]), ""] if n.get("summary")
                      else ["_서술 없음 (LLM 미응답) - 아래 수치는 코드 계산이다_", ""])
            # 코드가 고른 핵심 수치 - LLM 이 문장에 못 담은 재료가 여기로 온다.
            # 실측(2026-08-03): 서술만 실을 때 미시구조는 계산한 25개 중 3개만
            # Packet 에 도달했다. 나머지가 버려지는 것을 막는다.
            h = n.get("highlights") or {}
            if h.get("items") or h.get("flags"):
                if h.get("flags"):
                    lines += [f"- **임계 위반**: {', '.join(h['flags'])}"]
                lines += [f"- **핵심 수치**: {render_highlights(h)}", ""]
            for c in n.get("cautions") or []:
                lines += [f"> ⚠ {c}"]
            if n.get("cautions"):
                lines += [""]

    # 선순환 - 나중에 기계가 채점할 주장. 산문 무효화 조건과 달리 판정이
    # 갈리지 않는다(코드가 발행하고 코드가 채점한다).
    claims = packet.get("_claims") or []
    if claims:
        lines += ["## 검증 예정 주장 (코드 발행 · 사후 자동 채점)", "",
                  "| 종류 | 조건 | 지평 | 사전확률 | 출처 |",
                  "|---|---|---|---|---|"]
        for c in claims:
            tgt = c.get("threshold")
            cond = (f"`{c['metric']} {c['op']} "
                    f"{tgt if tgt is not None else c.get('threshold_text')}`")
            if c.get("baseline") is not None:
                cond += f" (기준 {c['baseline']:,})"
            elif c.get("baseline_text"):
                cond += f" (기준 {c['baseline_text']})"
            # 확률은 코드가 낸 사전 확률(evidence/forecast). 없으면 '-' -
            # 라벨 주장은 변동성 모형의 대상이 아니라 확률을 내지 않는다.
            pr = c.get("probability")
            lines += [f"| {c['kind']} | {cond} | {c['horizon_days']}거래일 | "
                      f"{f'{pr:.0%}' if pr is not None else '-'} | "
                      f"{c.get('source_node') or '-'} |"]
        notes_f = [c for c in claims if c.get("falsification_note")]
        if notes_f:
            lines += ["", "**반증 조건** (발행 시점 고정 - 사후 해석 방지)", ""]
            lines += [f"- {c['falsification_note']}" for c in notes_f]
        lines += ["",
                  f"기록 {packet.get('_claims_recorded', 0)}건 — 채점은 "
                  f"`collectors/packet_outcome_scorer.py`, 누적 성과는 "
                  f"`research.analyst_calibration`.", ""]
    return "\n".join(lines)


# ── 자체 점검 (LLM·API 없음) ───────────────────────────────────────────────
def _check_graph_shape():
    p = build_pipeline()
    assert p is not None
    print("  그래프 컴파일            OK")


def _check_halt_short_circuit():
    # 거래 불가 종목이면 Evidence·LLM 을 부르지 않고 끝나는지 - conditional edge
    import universe_manager as um

    class _D:
        excluded = {"999999": "HALTED"}
        def __init__(self): from datetime import datetime; self.as_of = datetime.now(timezone.utc)
    orig = um.run
    um.run = lambda basket=(): _D()
    try:
        out = build_pipeline().invoke({"symbol": "999999"})
        assert out.get("halted"), "거래 불가가 계속 진행됐다"
        assert "packet" not in out
    finally:
        um.run = orig
    print("  거래불가 조기 종료       OK")


def _check_packet_guard():
    bad_llm = lambda s, u: '{"symbol":"X","thesis":"t"}'  # 필수 키 누락
    try:
        draft_packet({"symbol": "X", "universe": {}, "sentiment": {},
                      "evidence": {}}, llm=bad_llm)
        raise AssertionError("불완전 Packet 이 통과했다")
    except ValueError:
        pass
    print("  Packet 스키마 가드       OK")


def _check_price_context_injection():
    # +27% 사고 재발 방지 - 확정 수치 블록과 "재계산 금지" 지시가 프롬프트에
    # 실제로 들어가는지, evidence 에 price_context 가 없으면 UNAVAILABLE 로
    # 명시되는지 코드로 확인한다
    captured = {}

    def fake_llm(system, user):
        captured["user"] = user
        return json.dumps({"symbol": "X", "thesis": "t", "facts": [],
                           "interpretation": [], "catalysts": [],
                           "invalidation": [], "evidence_quality": "partial"})

    draft_packet({"symbol": "X", "universe": {}, "sentiment": {},
                  "evidence": {"price_context": {
                      "status": "OK", "change_1d_pct": 27.0,
                      "direction_1d": "상승"}}}, llm=fake_llm)
    u = captured["user"]
    assert "CONFIRMED PRICE FIGURES" in u and "27.0" in u and "상승" in u
    assert "Do NOT recompute" in u

    draft_packet({"symbol": "X", "universe": {}, "sentiment": {},
                  "evidence": {}}, llm=fake_llm)
    assert '"UNAVAILABLE"' in captured["user"]
    print("  가격 컨텍스트 주입       OK")


def _check_evidence_module():
    # 조립기 모듈 자체 점검을 파이프라인 점검에 포함 - 계약이 함께 지켜지는지
    from evidence import bundle as eb

    eb._check_surge_plus27()
    eb._check_bundle_contract()


def _check_packet_report_renderer():
    """리포트 렌더러 순수성 - 같은 Packet 이면 같은 MD, 수치 불일치는 ⚠ 로 드러난다."""
    pk = {"symbol": "000660", "evidence_quality": "sufficient",
          "thesis": "테스트 논지", "facts": ["사실 1"], "interpretation": ["해석 1"],
          "catalysts": [], "invalidation": ["조건 1"],
          "numeric_check": {"ok": True, "checked": 3, "checked_counts": 1,
                            "unmatched": [], "unmatched_counts": []},
          "_analyst_verdicts": {"sentiment": "SCORED", "technical": "BEARISH"}}
    a = _render_packet_md(pk, now="2026-08-01T00:00:00+00:00")
    b = _render_packet_md(pk, now="2026-08-01T00:00:00+00:00")
    assert a == b, "같은 입력이 다른 리포트를 냈다 - 순수성 위반"
    assert "000660" in a and "테스트 논지" in a and "- 사실 1" in a
    assert "통과" in a and "sentiment=SCORED" in a
    assert "- (없음)" in a                      # catalysts 빈 목록 표기
    bad = dict(pk, numeric_check={"ok": False, "checked": 2, "checked_counts": 0,
                                  "unmatched": [55.5], "unmatched_counts": []})
    assert "⚠ 불일치" in _render_packet_md(bad, now="2026-08-01T00:00:00+00:00")
    print("  Packet 리포트 렌더러     OK")


def _check_run_recorder():
    """실행 기록 - 지문·상태가 정확히 남고, 기록 실패는 Packet 을 죽이지 않는다."""
    captured = {}

    class _Cur:
        def execute(self, sql, params):
            captured["params"] = params
        def fetchone(self):
            return ("11111111-1111-1111-1111-111111111111",)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()
        def commit(self):
            captured["committed"] = True

    ts = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    pk = {"symbol": "X", "evidence_quality": "partial",
          "numeric_check": {"ok": True}}
    rid = _record_pipeline_run(symbol="X", trace_id="t-1", started=ts, ended=ts,
                               status="COMPLETED", packet=pk, conn=_Conn())
    assert rid and captured["committed"]
    p = captured["params"]
    assert p[8] == "COMPLETED" and p[6] == "partial" and p[7] is True
    assert len(p[3]) == 64 and len(p[5]) == 64      # input/packet sha256
    assert "supervisor" in json.loads(p[4])          # node_models 기록
    # 같은 packet 이면 같은 지문 (재현성)
    _record_pipeline_run(symbol="X", trace_id="t-2", started=ts, ended=ts,
                         status="COMPLETED", packet=pk, conn=_Conn())
    h1 = p[5]
    assert captured["params"][5] == h1

    class _Broken:
        def cursor(self):
            raise RuntimeError("DB down")
    assert _record_pipeline_run(symbol="X", trace_id="t-3", started=ts, ended=ts,
                                status="HALTED", conn=_Broken()) is None  # 예외 없음
    print("  실행 기록(pipeline_runs) OK")


def _check_analyst_conflict_and_numeric_guard():
    """상충 보존 지시가 프롬프트에 실제로 들어가고, 서술 속 창작 수치가
    결정론으로 적발되는지 - 다각 분석 통합의 두 가드."""
    captured = {}

    def fake_llm(system, user):
        captured["user"] = user
        return json.dumps({
            "symbol": "X", "thesis": "감성 긍정 대 기술 BEARISH 상충",
            "facts": ["20일 모멘텀 -21.44%"],
            "interpretation": ["창작 수치 +55.5% 상승"],
            "catalysts": [], "invalidation": [], "evidence_quality": "partial"})

    out = draft_packet(
        {"symbol": "X", "universe": {},
         "sentiment": {"verdict": "SCORED", "score": 0.7},
         "technical": {"verdict": "BEARISH",
                       "readout": {"momentum_20d_pct": -21.4449}},
         "fundamental": {"status": "NOT_RUN"},
         "evidence": {"price_context": {"status": "OK", "change_1d_pct": 29.95}}},
        llm=fake_llm)
    u = captured["user"]
    assert "CONFIRMED ANALYST FINDINGS" in u and "BEARISH" in u
    assert "preserve BOTH" in u, "상충 보존 지시가 프롬프트에 없다"
    nc = out["packet"]["numeric_check"]
    assert nc["checked"] == 2 and not nc["ok"], nc
    assert nc["unmatched"] == [55.5], "창작 수치(55.5%)가 적발되지 않았다"
    print("  상충 보존·수치 창작 적발 OK")


def _check_schema_coercion():
    """모양 이탈은 교정하고 내용 검증은 그대로 - 분석가 6인 실행을 버리지 않는다."""
    assert flatten_to_str_list(["a", "b"]) == ["a", "b"]
    assert flatten_to_str_list("한 문장") == ["한 문장"]
    # 실측 이탈 모양: dict 항목이 섞인 리스트
    got = flatten_to_str_list([{"claim": "매출 46.76% 증가", "source": "disclosure:5"}])
    assert got == ["claim: 매출 46.76% 증가 | source: disclosure:5"], got
    # 통째로 dict
    assert flatten_to_str_list({"a": "1", "b": "2"}) == ["a: 1", "b: 2"]
    # 값이 없으면 빈 목록이 아니라 None - 없는 내용을 만들어내지 않는다
    for empty in (None, "", "   ", [], {}, ["", "  "]):
        assert flatten_to_str_list(empty) is None, empty

    # 평탄화된 뒤에도 수치 검사가 창작을 잡는지 - 교정이 가드를 무디게 하면 안 된다
    def liar(system, user):
        return json.dumps({
            "symbol": "X", "thesis": "성장 지속",
            "facts": {"revenue": "전년 대비 18.3% 증가"},   # dict 이탈 + 창작 수치
            "interpretation": [], "catalysts": [], "invalidation": [],
            "evidence_quality": "sufficient"})

    out = draft_packet(
        {"symbol": "X", "universe": {}, "sentiment": {"verdict": "SCORED"},
         "fundamental": {"verdict": "NOTED", "readout": {"revenue_yoy_pct": 46.76}},
         "evidence": {"price_context": {"status": "OK"}}}, llm=liar)["packet"]
    assert out["facts"] == ["revenue: 전년 대비 18.3% 증가"], out["facts"]
    assert out["_schema_coerced"] == ["facts"], out.get("_schema_coerced")
    assert not out["numeric_check"]["ok"], "평탄화 후 창작 수치를 놓쳤다"
    assert out["evidence_quality"] == "partial", "강등이 안 걸렸다"
    # 한 겹 싸서 온 Packet 은 벗긴다 - 실측 2회가 '키 전부 누락'으로 죽었다
    inner = {k: "x" for k in PACKET_REQUIRED_KEYS}
    assert unwrap_packet({"packet": inner}) is inner
    assert unwrap_packet({"result": {"data": inner}}) != inner, \
        "두 겹까지 파고들지 않는다 - 추측으로 구조를 바꾸지 않는다"
    assert unwrap_packet(inner) is inner, "이미 평평하면 그대로 둔다"
    # 일부 키만 있는 dict 는 건드리지 않는다(다른 문제이므로 사유가 보여야 한다)
    partial = {"wrap": {"symbol": "x", "thesis": "y"}}
    assert unwrap_packet(partial) is partial
    assert unwrap_packet(["not", "a", "dict"]) == ["not", "a", "dict"]
    print("  스키마 교정·가드 유지    OK")


def _check_numeric_failure_downgrades_quality():
    """창작 수치 적발이 **등급까지 낮추는지** - 표시만 하면 하류가 안 읽는다.

    실측 2026-08-02: 총괄이 매출 +18.3%(실제 46.76%)를 facts 에 출처까지
    달아 넣었는데 evidence_quality 는 sufficient 로 나갔다.
    """
    def liar(system, user):
        return json.dumps({
            "symbol": "X", "thesis": "매출이 전년 대비 18.3% 늘었다",
            "facts": ["매출 성장률 18.3% (disclosure:5)"],
            "interpretation": [], "catalysts": [], "invalidation": [],
            "evidence_quality": "sufficient"})

    out = draft_packet(
        {"symbol": "X", "universe": {}, "sentiment": {"verdict": "SCORED"},
         "fundamental": {"verdict": "NOTED", "readout": {"revenue_yoy_pct": 46.76}},
         "evidence": {"price_context": {"status": "OK"}}}, llm=liar)
    p = out["packet"]
    assert not p["numeric_check"]["ok"]
    assert p["evidence_quality"] == "partial", \
        f"창작 수치가 있는데 등급이 {p['evidence_quality']} 로 남았다"
    assert p["numeric_check"]["downgraded_from"] == "sufficient"

    # 정직한 Packet 은 건드리지 않는다 - 강등은 낮추기만, 올리진 않는다
    def honest(system, user):
        return json.dumps({
            "symbol": "X", "thesis": "매출이 46.76% 늘었다",
            "facts": ["매출 성장률 46.76%"], "interpretation": [],
            "catalysts": [], "invalidation": [], "evidence_quality": "sufficient"})

    ok = draft_packet(
        {"symbol": "X", "universe": {}, "sentiment": {"verdict": "SCORED"},
         "fundamental": {"verdict": "NOTED", "readout": {"revenue_yoy_pct": 46.76}},
         "evidence": {"price_context": {"status": "OK"}}}, llm=honest)["packet"]
    assert ok["numeric_check"]["ok"] and ok["evidence_quality"] == "sufficient"
    print("  수치 불일치 = 등급 강등  OK")


def _check_claims_use_bundle_key():
    """가격 주장이 실제로 발행되는지 - 키 이름이 어긋나면 조용히 0건이 된다.

    실측 2026-08-02: price_context 의 종가 키는 'last_close' 인데 'close' 로
    읽어 가격 주장이 통째로 빠졌고 아무도 몰랐다(리포트에 레짐 1건만).
    """
    from evidence.bundle import compute_price_context

    ctx = compute_price_context([
        {"bucket_time": f"2026-07-{d:02d}T00:00:00+09:00", "close": 1000.0 + d}
        for d in range(1, 26)])
    assert "last_close" in ctx, "bundle 계약(last_close)이 바뀌었다"

    claims = build_packet_claims(
        {"evidence": {"price_context": ctx},
         "regime": {"verdict": "RISK_ON"},
         "geopolitical": {"verdict": "ELEVATED"}}, {})
    kinds = {c["kind"] for c in claims}
    assert {"PRICE_DRAWDOWN", "PRICE_RALLY", "REGIME_FLIP",
            "GEO_ESCALATION"} == kinds, kinds
    price_claims = [c for c in claims if c["kind"].startswith("PRICE_")]
    assert len(price_claims) == len(CLAIM_HORIZONS) * 2, price_claims
    base = float(ctx["last_close"])
    dd = next(c for c in price_claims if c["kind"] == "PRICE_DRAWDOWN")
    assert abs(dd["threshold"] - base * 0.9) < 1e-6, dd

    # 이미 SHOCK 이면 'SHOCK 으로 악화'는 발동 불가 - 주장이 되지 않는다
    no_geo = build_packet_claims({"evidence": {"price_context": ctx},
                                  "geopolitical": {"verdict": "SHOCK"}}, {})
    assert not any(c["kind"] == "GEO_ESCALATION" for c in no_geo)
    # 기준가가 없으면 가격 주장을 만들지 않는다
    none_ctx = build_packet_claims(
        {"evidence": {"price_context": {"status": "UNAVAILABLE"}}}, {})
    assert not any(c["kind"].startswith("PRICE_") for c in none_ctx)
    print("  주장 발행·기준가 계약    OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        sym = sys.argv[sys.argv.index("--run") + 1]
        print(f"{PIPELINE_VERSION} 실행: {sym}")
        packet = run_research_department(sym)
        print(json.dumps(packet, ensure_ascii=False, indent=1))
        # 결정론 MD 리포트 저장 (동규님 리스크본부 패턴 채택, 2026-08-01)
        rep_dir = _BASE / "reports"
        rep_dir.mkdir(exist_ok=True)
        rp = rep_dir / f"research_packet_{sym}_{datetime.now(KST):%Y%m%d_%H%M%S}.md"
        rp.write_text(_render_packet_md(packet), encoding="utf-8")
        print(f"리포트 저장: {rp.relative_to(_BASE)}")

        # Notion 은 **구속력 없는 Projection** 이다 - 실패해도 Packet 은 그대로다
        # (동규님 리스크 Reporter 와 같은 규약). 미설정이면 조용히 생략한다.
        try:
            from notion_reporter import upload_packet
            nr = upload_packet(packet, symbol=sym,
                               report_md=rp.read_text(encoding="utf-8"))
            print("Notion:", nr.get("url") or nr.get("reason") or nr)
        except Exception as e:  # noqa: BLE001
            print(f"Notion 업로드 예외(무시): {type(e).__name__}: {e}")
        raise SystemExit(0)

    print(f"{PIPELINE_VERSION} 자체 점검 (LLM·API 없음)")
    _check_graph_shape()
    _check_halt_short_circuit()
    _check_packet_guard()
    _check_price_context_injection()
    _check_evidence_module()
    _check_analyst_conflict_and_numeric_guard()
    _check_schema_coercion()
    _check_numeric_failure_downgrades_quality()
    _check_claims_use_bundle_key()
    _check_packet_report_renderer()
    _check_run_recorder()
    print("본부 파이프라인 11개 영역 통과. 실행은 --run <종목>")
