#!/usr/bin/env python3
"""strategy-hypothesis-agent (QNT-01) - 퀀트/백테스트본부 전략 가설 연구자 실행 실체.

담당: 재일 (퀀트/백테스트본부)
근거: departments/04-quant-backtest/hermes/config.yaml 의 strategy-research-agent
      페르소나(QNT-01) - "falsifiable strategy hypotheses with an explicit
      economic rationale", "a hypothesis without a stated failure mode is
      incomplete"
      supabase/migrations/20260729000300_research_quant_strategy.sql 의
      quant.hypotheses DDL (title, rationale, expected_edge, falsification_
      criteria, required_data_products, status, created_by, trace_id)
      departments/01-research/agents/technical_analyst.py 의 LLM 호출·Pydantic
      검증·verify·자체점검 패턴 (Ollama 모델만 agent-quant)
      pipeline/backtest_runner.py 의 DB 연결 방식(load_project_env)

▶ 이 파일이 지키는 QNT-01 계약
  - **가설 소급 변경 금지**: 등록(insert) 후 고치는 경로가 이 파일에 없다
    (update 문 부재). 바꾸려면 새 버전을 새 행으로 등록한다.
  - **실패 조건 없는 가설은 미완성이다**: falsification_criteria 가 측정
    가능한 2개 미만이면 verify 가 거부한다 - 등록까지 가지 못한다.
  - **LLM 은 가설 서술만, 등록은 결정론 레지스트라가**: LLM 출력이 DB 에
    직접 닿는 경로가 없다. verify 를 통과한 스키마 필드만 파라미터 바인딩으로
    insert 되고, status/created_by/trace_id 는 코드가 박는다.
  - **관찰 밖 지식 금지**: LLM 에게 주는 시장 입력은 build_observation_context
    가 market-api /regime/daily 에서 만든 사실 딕셔너리뿐이다. observation_refs
    는 그 키만 인용해야 하고, verify 가 환각 키를 제거·카운트한다.
  - **중복 가설 방지**: 같은 title 이 PROPOSED/TESTING 에 이미 있으면 등록을
    거부하고 기존 id 를 반환한다(select 후 insert - 단일 에이전트 운용 전제).
    REJECTED/ARCHIVED 로 죽은 가설의 제목 재사용은 막지 않는다.

흐름: build_observation_context(결정론) -> generate(LLM) -> verify(결정론)
      -> register(결정론 레지스트라, --register 일 때만)

이 출력은 **Agent Decision 도 Signal 도 아니다.** QNT-02/03 이 Experiment 로
가져갈 Hypothesis Spec 이다 (CLAUDE.md: Agent Decision != Signal != Order).

실행:
  python agents/strategy_hypothesis_agent.py                  # 자체 점검 (네트워크·DB 없음)
  python agents/strategy_hypothesis_agent.py --run            # 실측: 생성·검증까지 (등록 없음)
  python agents/strategy_hypothesis_agent.py --run --register # 등록까지
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

AGENT_VERSION = "quant-strategy-hypothesis-agent-v1"

MARKET_API = os.environ.get("MARKET_API_URL", "http://127.0.0.1:8036")
OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("STRATEGY_HYPO_MODEL", "agent-quant")
LLM_TIMEOUT = float(os.environ.get("STRATEGY_HYPO_LLM_TIMEOUT", "180"))  # 로컬 14b

REGIME_DAYS = 20          # 관찰 창 - /regime/daily 최근 20 거래일 단면
MIN_FALSIFICATION = 2     # QNT-01 계약: 실패 조건 최소 2개
# 실존 데이터 프로덕트 - quant.dataset_manifests 에 실제로 있는 것만.
# 새 Dataset 이 생기면 여기 추가한다 (verify 가 이 목록 밖을 제거한다).
KNOWN_DATA_PRODUCTS = ("krx-basket-daily/v1",)


# ---------------------------------------------------------------------------
# LLM 출력 계약 (개발 원칙: 항상 Pydantic 으로 검증)
# ---------------------------------------------------------------------------

class ExpectedEdge(BaseModel):
    type: str = Field(max_length=60, description="momentum | mean_reversion 등")
    horizon_days: int = Field(gt=0, le=365)
    universe: str = Field(max_length=200)

    @field_validator("type", "universe", mode="before")
    @classmethod
    def _strip(cls, v):
        return str(v).strip()


class HypothesisSpec(BaseModel):
    title: str = Field(max_length=300)
    rationale: str = Field(max_length=4000,
                           description="경제적 근거 - 왜 엣지가 존재하고 반대편은 누구인가")
    expected_edge: ExpectedEdge
    falsification_criteria: list[str]
    required_data_products: list[str]
    observation_refs: list[str] = Field(description="관찰 컨텍스트의 키만 인용")

    @field_validator("title", "rationale", mode="before")
    @classmethod
    def _strip(cls, v):
        return str(v).strip()

    @field_validator("falsification_criteria", "required_data_products",
                     "observation_refs", mode="before")
    @classmethod
    def _coerce_str_list(cls, v):
        return [str(x).strip() for x in v] if isinstance(v, list) else v


# ---------------------------------------------------------------------------
# 1. build_observation_context - 결정론. LLM 에게 주는 유일한 시장 관찰 입력
# ---------------------------------------------------------------------------

def _r2(v: float | None) -> float | None:
    return None if v is None else round(float(v), 2)


def _http_get(url: str, timeout: int = 20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def summarize_regime(rows: list[dict]) -> dict:
    """(순수 함수) market-api /regime/daily 행 목록 -> 사실 딕셔너리.

    API 는 최신순(desc)으로 주므로 여기서 오름차순 정렬한다. 등락 비율은
    advancers/(advancers+decliners), SMA20 상회 비율은 above_sma20/
    sma20_coverage - 분모 0 인 날은 수치로 위장하지 않고 None(제외)이다.
    이 딕셔너리의 키가 LLM 이 인용할 수 있는 사실의 전부다.
    """
    rs = sorted(rows, key=lambda r: str(r["trade_date"]))
    if not rs:
        raise RuntimeError("/regime/daily 가 빈 목록을 줬다 - 관찰할 단면이 없다")

    adv_shares: list[float | None] = []
    above_pcts: list[float | None] = []
    up_days = down_days = streak = max_streak = 0
    for r in rs:
        a = int(r.get("advancers") or 0)
        d = int(r.get("decliners") or 0)
        share = a / (a + d) * 100.0 if (a + d) > 0 else None
        adv_shares.append(share)
        cov = int(r.get("sma20_coverage") or 0)
        ab = int(r.get("above_sma20") or 0)
        above_pcts.append(ab / cov * 100.0 if cov > 0 else None)
        if share is None or a == d:
            streak = 0          # 판단 불가/보합일은 하락 연속을 잇지 않는다
        elif a > d:
            up_days += 1
            streak = 0
        else:
            down_days += 1
            streak += 1
            max_streak = max(max_streak, streak)

    valid_adv = [x for x in adv_shares if x is not None]
    valid_above = [x for x in above_pcts if x is not None]
    if not valid_adv and not valid_above:
        raise RuntimeError("레짐 단면 전부 계산 불가 (분모 0) - 관찰 없이 가설 없다")

    last = rs[-1]
    return {
        "source": "market-api /regime/daily (KRX ls_chart 일봉 단면)",
        "window_trade_days": len(rs),
        "window_start_date": str(rs[0]["trade_date"])[:10],
        "window_end_date": str(last["trade_date"])[:10],
        "latest_advancer_share_pct": _r2(adv_shares[-1]),
        "mean_advancer_share_pct": _r2(sum(valid_adv) / len(valid_adv)) if valid_adv else None,
        "up_breadth_days": up_days,
        "down_breadth_days": down_days,
        "max_consecutive_down_breadth_days": max_streak,
        "latest_pct_above_sma20": _r2(above_pcts[-1]),
        "mean_pct_above_sma20": _r2(sum(valid_above) / len(valid_above)) if valid_above else None,
        "min_pct_above_sma20": _r2(min(valid_above)) if valid_above else None,
        "max_pct_above_sma20": _r2(max(valid_above)) if valid_above else None,
        "pct_above_sma20_change_pp": (_r2(valid_above[-1] - valid_above[0])
                                      if len(valid_above) >= 2 else None),
        "latest_sma20_coverage": int(last.get("sma20_coverage") or 0),
        "latest_symbols": int(last.get("symbols") or 0),
    }


def build_observation_context(*, market_api: str | None = None,
                              days: int = REGIME_DAYS,
                              get: Callable = _http_get) -> dict:
    """market-api /regime/daily?days=N -> summarize_regime. 실패는 예외로
    드러낸다(fail-closed) - 관찰 없이 가설을 지어내지 않는다."""
    base = (market_api or MARKET_API).rstrip("/")
    return summarize_regime(get(f"{base}/regime/daily?days={days}"))


# ---------------------------------------------------------------------------
# 2. generate - LLM 은 여기서만 쓴다 (가설 서술만, 등록 권한 없음)
# ---------------------------------------------------------------------------

# 페르소나 원문: hermes/config.yaml strategy-research-agent (QNT-01)
_SYSTEM = """You are the Strategy Hypothesis Researcher (QNT-01). Your mission \
is to convert market observations into falsifiable strategy hypotheses with an \
explicit economic rationale - why should this edge exist and who is on the \
other side of the trade. Every hypothesis ships as an Experiment Spec with \
entry/exit logic, target universe, holding horizon, expected failure modes and \
the exact data required, ready for the Dataset and Backtest engineers. \
Official outputs: Hypothesis Spec, Experiment Spec, Expected Failure Mode. You \
never retro-fit a hypothesis after seeing backtest results - any change is \
registered as a new version, and a hypothesis without a stated failure mode is \
incomplete.

Binding rules for this task:
- The ONLY market evidence you may use is the observation context JSON the
  user gives you (code-computed from market-api /regime/daily). Do NOT use any
  other market knowledge, remembered prices, news or macro narratives as
  evidence for the hypothesis.
- Propose exactly ONE falsifiable strategy hypothesis grounded in that context.
- title: short Korean title of the hypothesis.
- rationale: Korean. The economic rationale - why should this edge exist and
  WHO is on the other side of the trade (who supplies the mispricing).
- expected_edge.type: one short token like "momentum", "mean_reversion",
  "breadth_rotation", "volatility".
- expected_edge.horizon_days: positive integer holding horizon.
- expected_edge.universe: short description of the target universe.
- falsification_criteria: Korean, at least 2 items, each MEASURABLE (explicit
  metric, threshold and window). A hypothesis without failure modes is
  incomplete and will be rejected.
- required_data_products: ONLY items from the allowed list the user gives you.
- observation_refs: ONLY keys that exist in the observation context JSON.
Output JSON only, exactly this shape, no other text:
{"title":"...","rationale":"...",
 "expected_edge":{"type":"mean_reversion","horizon_days":5,"universe":"..."},
 "falsification_criteria":["...","..."],
 "required_data_products":["krx-basket-daily/v1"],
 "observation_refs":["latest_pct_above_sma20"]}"""

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def generate(context: dict, llm: Callable | None = None) -> HypothesisSpec:
    """관찰 컨텍스트를 LLM 에 주고 HypothesisSpec 을 받는다. Schema 불합격이면
    한 번 고쳐 부르고, 또 실패하면 예외다 - 가설을 코드가 지어내지 않는다.
    llm 주입은 자체 점검용."""
    prompt = ("Observation context (code-computed, the ONLY permitted market "
              "evidence):\n"
              + json.dumps(context, ensure_ascii=False, indent=1)
              + "\n\nAllowed observation_ref keys: "
              + json.dumps(sorted(context.keys()))
              + "\nAllowed required_data_products: "
              + json.dumps(list(KNOWN_DATA_PRODUCTS))
              + "\nPropose ONE falsifiable strategy hypothesis now.")

    call = llm or _ollama_call
    last_err = None
    for attempt in range(2):
        text = call(_SYSTEM, prompt if attempt == 0 else
                    prompt + f"\n\nYour previous output failed validation: "
                             f"{last_err}. Return ONLY valid JSON for the schema.")
        text = _THINK_RE.sub("", text)  # qwen3 <think> 프리앰블 제거
        try:
            start = text.find("{")
            end = text.rfind("}")
            return HypothesisSpec.model_validate_json(text[start:end + 1])
        except (ValidationError, ValueError) as e:
            last_err = str(e)[:200]
    raise RuntimeError(f"LLM 가설이 Schema 를 두 번 어겼다: {last_err}")


def _ollama_call(system: str, user: str) -> str:
    """로컬/팀 Ollama (OpenAI 호환) - technical_analyst._ollama_call 과 동일
    패턴, 모델만 agent-quant."""
    req = urllib.request.Request(
        OLLAMA_BASE + "/v1/chat/completions", method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"},
        data=json.dumps({
            "model": MODEL,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.3,
            # 소형 모델의 JSON 규율 - 지원 안 하는 서버는 무시한다
            "response_format": {"type": "json_object"},
        }).encode(),
    )
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
        out = json.loads(r.read())
    return out["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# 3. verify - 결정론 검증. LLM 이 계약을 어기면 여기서 걸린다
# ---------------------------------------------------------------------------

def verify(spec: HypothesisSpec, context: dict
           ) -> tuple[HypothesisSpec | None, dict]:
    """(1) title/rationale 빈 값 -> 거부,
    (2) observation_refs 중 context 에 없는 키 제거(환각 인용 - 카운트),
    (3) required_data_products 중 실존 목록 밖 항목 제거+카운트, 전부
        제거되면 거부(검증할 데이터가 없는 가설은 실험 불가),
    (4) falsification_criteria 가 (중복·빈 문자열 정리 후) 2개 미만이면 거부
        - QNT-01 계약: 실패 조건 없는 가설은 미완성.
    거부 시 (None, issues). 통과 시 정리된 spec - 문장은 고치지 않는다."""
    issues: dict = {"hallucinated_refs": [], "removed_data_products": [],
                    "rejected": None, "notes": []}

    def _reject(reason: str):
        issues["rejected"] = reason
        return None, issues

    if not spec.title.strip():
        return _reject("title 이 빈 값 - 가설 거부")
    if not spec.rationale.strip():
        return _reject("rationale 이 빈 값 - 경제적 근거 없는 가설 거부")

    refs: list[str] = []
    for k in spec.observation_refs:
        if k in context:
            if k not in refs:
                refs.append(k)
        else:
            issues["hallucinated_refs"].append(k)

    products: list[str] = []
    for p in spec.required_data_products:
        if p in KNOWN_DATA_PRODUCTS:
            if p not in products:
                products.append(p)
        else:
            issues["removed_data_products"].append(p)
    if not products:
        return _reject(
            f"required_data_products 가 실존 목록{list(KNOWN_DATA_PRODUCTS)} 에 "
            f"하나도 없다(제거 {len(issues['removed_data_products'])}개) - "
            f"실험 불가 가설 거부")

    criteria: list[str] = []
    for c in spec.falsification_criteria:
        c = c.strip()
        if c and c not in criteria:
            criteria.append(c)
    if len(criteria) < MIN_FALSIFICATION:
        return _reject(
            f"falsification_criteria {len(criteria)}개 < {MIN_FALSIFICATION}개 - "
            f"실패 조건 없는 가설은 미완성 (QNT-01 계약)")

    if not refs:
        issues["notes"].append(
            "observation_refs 가 (환각 제거 후) 비었다 - 관찰 인용 없는 가설")

    return (spec.model_copy(update={"observation_refs": refs,
                                    "required_data_products": products,
                                    "falsification_criteria": criteria}),
            issues)


# ---------------------------------------------------------------------------
# 4. register - 결정론 레지스트라 (LLM 무관). select 후 insert
# ---------------------------------------------------------------------------

_SELECT_DUP = """
select hypothesis_id from quant.hypotheses
where title = %s and status in ('PROPOSED', 'TESTING')
order by created_at limit 1
"""

_INSERT = """
insert into quant.hypotheses
  (title, rationale, expected_edge, falsification_criteria,
   required_data_products, status, created_by, trace_id)
values (%s, %s, %s::jsonb, %s::jsonb, %s::jsonb, 'PROPOSED', %s, %s)
returning hypothesis_id
"""


def _connect():
    import psycopg2

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                           / "01-research" / "collectors"))
    from source_registry import load_project_env

    env = load_project_env()
    return psycopg2.connect(env["DATABASE_URL"], connect_timeout=20)


def register(spec: HypothesisSpec, conn=None) -> tuple[str, bool]:
    """quant.hypotheses 에 status='PROPOSED' 로 insert -> (hypothesis_id, 신규 여부).

    같은 title 이 이미 PROPOSED/TESTING 이면 등록을 거부하고 기존 id 를
    (id, False) 로 반환한다 - 중복 가설 방지. observation_refs 는 DDL 에 전용
    컬럼이 없어 expected_edge jsonb 에 동승시킨다(관찰 근거는 엣지 주장의
    일부다). conn 주입은 자체 점검용 - 주입된 연결은 닫지 않는다."""
    own = conn is None
    if own:
        conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_SELECT_DUP, (spec.title,))
            row = cur.fetchone()
            if row is not None:
                return str(row[0]), False

            edge = spec.expected_edge.model_dump()
            edge["observation_refs"] = spec.observation_refs
            cur.execute(_INSERT, (
                spec.title, spec.rationale,
                json.dumps(edge, ensure_ascii=False),
                json.dumps(spec.falsification_criteria, ensure_ascii=False),
                json.dumps(spec.required_data_products, ensure_ascii=False),
                AGENT_VERSION, str(uuid.uuid4())))
            hid = str(cur.fetchone()[0])
        conn.commit()
        return hid, True
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 5. run - 관찰 -> 생성 -> 검증 -> (옵션) 등록
# ---------------------------------------------------------------------------

def run(register_db: bool = False, *, llm: Callable | None = None,
        get: Callable | None = None, conn=None) -> dict:
    """기본은 등록하지 않는다 - register_db=True(--register) 일 때만 insert.
    각 단계 실패는 상태로 드러낸다(fail-closed): NO_OBSERVATION /
    LLM_UNAVAILABLE / REJECTED / REGISTER_FAILED."""
    out = {"agent": AGENT_VERSION, "model": MODEL,
           "as_of": datetime.now(timezone.utc).isoformat()}
    try:
        context = build_observation_context(get=get or _http_get)
    except Exception as e:  # noqa: BLE001 - 관찰 없이 가설을 지어내지 않는다
        return {**out, "status": "NO_OBSERVATION",
                "reason": f"market-api /regime/daily 실패: {type(e).__name__}: {e}"}
    out["context"] = context

    try:
        raw = generate(context, llm=llm)
    except Exception as e:  # noqa: BLE001 - Ollama 다운/Schema 반복 위반
        return {**out, "status": "LLM_UNAVAILABLE",
                "reason": f"{type(e).__name__}: {str(e)[:300]}"}

    spec, issues = verify(raw, context)
    out["issues"] = issues
    if spec is None:
        return {**out, "status": "REJECTED", "reason": issues["rejected"],
                "raw_spec": raw.model_dump()}
    out["spec"] = spec.model_dump()
    out["status"] = "VERIFIED"

    if register_db:
        try:
            hid, created = register(spec, conn=conn)
        except Exception as e:  # noqa: BLE001 - DB 장애를 성공으로 위장하지 않는다
            return {**out, "status": "REGISTER_FAILED",
                    "reason": f"{type(e).__name__}: {str(e)[:300]}"}
        out["hypothesis_id"] = hid
        out["registered"] = created
        out["status"] = "REGISTERED" if created else "DUPLICATE"
    return out


def _print_result(out: dict) -> None:
    print(f"  status={out['status']} (model={out.get('model')})")
    if out.get("reason"):
        print(f"  reason: {out['reason']}")
    ctx = out.get("context")
    if ctx:
        print(f"  관찰 창: {ctx['window_start_date']} ~ {ctx['window_end_date']} "
              f"({ctx['window_trade_days']}거래일, 심볼 {ctx['latest_symbols']})")
        for k in ("latest_advancer_share_pct", "mean_advancer_share_pct",
                  "up_breadth_days", "down_breadth_days",
                  "max_consecutive_down_breadth_days", "latest_pct_above_sma20",
                  "mean_pct_above_sma20", "pct_above_sma20_change_pp"):
            print(f"    {k:36} {ctx[k]}")
    spec = out.get("spec") or out.get("raw_spec")
    if spec:
        print("  ─ Hypothesis Spec " + "─" * 40)
        print(f"  title: {spec['title']}")
        print(f"  rationale: {spec['rationale']}")
        e = spec["expected_edge"]
        print(f"  expected_edge: type={e['type']} horizon_days={e['horizon_days']} "
              f"universe={e['universe']}")
        for i, c in enumerate(spec["falsification_criteria"], 1):
            print(f"  falsification[{i}]: {c}")
        print(f"  required_data_products: {spec['required_data_products']}")
        print(f"  observation_refs: {spec['observation_refs']}")
    iss = out.get("issues")
    if iss:
        print(f"  issues: 환각 refs {len(iss['hallucinated_refs'])}"
              f"{iss['hallucinated_refs'] or ''} / 미실존 데이터 제거 "
              f"{len(iss['removed_data_products'])}"
              f"{iss['removed_data_products'] or ''}")
        for n in iss["notes"]:
            print(f"  note: {n}")
    if "hypothesis_id" in out:
        dup = "" if out["registered"] else " (중복 - 기존 가설, 새로 등록 안 함)"
        print(f"  hypothesis_id: {out['hypothesis_id']}{dup}")


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·DB 없음 (합성 레짐 + 가짜 LLM + 가짜 DB 커서)
# ---------------------------------------------------------------------------

# API 최신순(desc) 그대로 - 정렬은 summarize_regime 책임
_REGIME_ROWS_DESC = [
    {"trade_date": "2026-07-24", "advancers": 20, "decliners": 70, "unchanged": 10,
     "above_sma20": 30, "sma20_coverage": 100, "symbols": 100},
    {"trade_date": "2026-07-23", "advancers": 40, "decliners": 50, "unchanged": 0,
     "above_sma20": 45, "sma20_coverage": 90, "symbols": 95},
    {"trade_date": "2026-07-22", "advancers": 30, "decliners": 60, "unchanged": 10,
     "above_sma20": 40, "sma20_coverage": 100, "symbols": 100},
    {"trade_date": "2026-07-21", "advancers": 60, "decliners": 30, "unchanged": 10,
     "above_sma20": 50, "sma20_coverage": 100, "symbols": 100},
    # 첫 수집일: prev_close 전무 + SMA20 커버리지 0 - 비율 전부 판단 불가
    {"trade_date": "2026-07-20", "advancers": 0, "decliners": 0, "unchanged": 0,
     "above_sma20": 0, "sma20_coverage": 0, "symbols": 100},
]


def _good_spec(**kw) -> HypothesisSpec:
    base = dict(
        title="하락 광폭 후 저SMA20 비율 반등 가설",
        rationale="연속 하락으로 손절 매도가 몰린 뒤에는 유동성 공급자가 "
                  "반대편에 서고, 광폭 지표가 바닥권이면 되돌림이 나온다.",
        expected_edge={"type": "mean_reversion", "horizon_days": 5,
                       "universe": "KRX 일봉 바스켓"},
        falsification_criteria=[
            "SMA20 상회 비율 30% 미만 진입 후 5거래일 수익률 평균이 0 이하",
            "3년 백테스트에서 비용 차감 Sharpe 0.5 미만"],
        required_data_products=["krx-basket-daily/v1"],
        observation_refs=["latest_pct_above_sma20",
                          "max_consecutive_down_breadth_days"])
    base.update(kw)
    return HypothesisSpec.model_validate(base)


class _FakeCursor:
    """psycopg2 커서 흉내 - register 의 select 후 insert 만 지원한다."""

    def __init__(self, db):
        self.db = db
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        s = " ".join(sql.split()).lower()
        self.db.executed.append((s, params))
        if s.startswith("select"):
            hit = self.db.rows.get(params[0])
            self._row = ((hit["id"],)
                         if hit and hit["status"] in ("PROPOSED", "TESTING")
                         else None)
        elif s.startswith("insert"):
            assert "'proposed'" in s, "status 를 코드가 박지 않았다"
            hid = str(uuid.uuid4())
            self.db.rows[params[0]] = {"id": hid, "status": "PROPOSED",
                                       "params": params}
            self.db.inserts.append(params)
            self._row = (hid,)
        else:
            raise AssertionError(f"레지스트라가 예상 밖 SQL 을 실행했다: {s[:60]}")

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self):
        self.rows: dict = {}      # title -> {id, status, params}
        self.inserts: list = []
        self.executed: list = []
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1

    def close(self):
        raise AssertionError("주입된 연결을 register 가 멋대로 닫았다")


def _check_context_math():
    """수작업 계산과 대조 (오름차순: 07-20 ~ 07-24).
    등락 비율: 66.6667, 33.3333, 44.4444, 22.2222 (07-20 은 분모 0 - 제외)
    -> latest 22.22, mean 166.6667/4=41.67. 상승일 1(07-21), 하락일 3(연속 3).
    SMA20 상회: 50, 40, 50, 30 -> latest 30, mean 42.5, min 30, max 50,
    변화 30-50=-20pp."""
    c = summarize_regime(list(_REGIME_ROWS_DESC))
    exp = {"window_trade_days": 5, "window_start_date": "2026-07-20",
           "window_end_date": "2026-07-24",
           "latest_advancer_share_pct": 22.22, "mean_advancer_share_pct": 41.67,
           "up_breadth_days": 1, "down_breadth_days": 3,
           "max_consecutive_down_breadth_days": 3,
           "latest_pct_above_sma20": 30.0, "mean_pct_above_sma20": 42.5,
           "min_pct_above_sma20": 30.0, "max_pct_above_sma20": 50.0,
           "pct_above_sma20_change_pp": -20.0,
           "latest_sma20_coverage": 100, "latest_symbols": 100}
    for k, v in exp.items():
        assert c[k] == v, f"{k}: {c[k]} != {v}"
    print("  관찰 컨텍스트 수작업 대조   OK")


def _check_context_fail_closed():
    try:
        summarize_regime([])
        raise AssertionError("빈 레짐이 통과했다")
    except RuntimeError:
        pass
    # 전부 분모 0 이면 관찰 불가 - 가짜 수치를 만들지 않는다
    try:
        summarize_regime([{"trade_date": "2026-07-20", "advancers": 0,
                           "decliners": 0, "unchanged": 0, "above_sma20": 0,
                           "sma20_coverage": 0, "symbols": 0}])
        raise AssertionError("전부 분모 0 인데 통과했다")
    except RuntimeError:
        pass
    # market-api 다운 -> run 은 NO_OBSERVATION (LLM 을 부르지 않는다)
    def down(_u):
        raise OSError("connection refused")

    out = run(get=down, llm=lambda s, u: (_ for _ in ()).throw(
        AssertionError("관찰 없는데 LLM 이 불렸다")))
    assert out["status"] == "NO_OBSERVATION" and "connection refused" in out["reason"]
    print("  관찰 실패 fail-closed       OK")


def _check_generate_roundtrip():
    ctx = summarize_regime(list(_REGIME_ROWS_DESC))

    def fake_llm(system, user):
        assert "QNT-01" in system and "falsifiable" in system
        assert "latest_pct_above_sma20" in user  # 관찰이 프롬프트에 실렸다
        assert "krx-basket-daily/v1" in user     # 허용 데이터 목록도
        return ("<think>브레드스가 약하다 {테스트}</think>\n"
                + _good_spec().model_dump_json())

    spec = generate(ctx, llm=fake_llm)
    assert spec.title and spec.expected_edge.horizon_days == 5

    calls = {"n": 0}

    def flaky_llm(_s, _u):
        calls["n"] += 1
        if calls["n"] == 1:
            return "죄송하지만 JSON 이 아닙니다"
        return _good_spec().model_dump_json()

    spec = generate(ctx, llm=flaky_llm)
    assert calls["n"] == 2 and spec.title

    try:
        generate(ctx, llm=lambda s, u: "no json ever")
        raise AssertionError("Schema 위반이 두 번인데 통과했다")
    except RuntimeError:
        pass
    print("  가짜 LLM generate 왕복      OK")


def _check_verify_falsification_reject():
    ctx = summarize_regime(list(_REGIME_ROWS_DESC))
    spec, issues = verify(_good_spec(
        falsification_criteria=["5일 수익률 평균이 0 이하"]), ctx)
    assert spec is None and "미완성" in issues["rejected"]
    # 중복·빈 문자열은 개수로 안 쳐준다 - 부풀린 2개도 거부
    spec2, issues2 = verify(_good_spec(
        falsification_criteria=["같은 조건", "같은 조건", "  "]), ctx)
    assert spec2 is None and issues2["rejected"]
    # 제대로 2개면 통과
    spec3, issues3 = verify(_good_spec(), ctx)
    assert spec3 is not None and issues3["rejected"] is None
    assert len(spec3.falsification_criteria) == 2
    print("  falsification<2 거부        OK")


def _check_verify_hallucinated_refs():
    ctx = summarize_regime(list(_REGIME_ROWS_DESC))
    spec, issues = verify(_good_spec(
        observation_refs=["latest_pct_above_sma20", "kospi_per",
                          "latest_pct_above_sma20", "vix_level"]), ctx)
    assert spec is not None
    assert spec.observation_refs == ["latest_pct_above_sma20"]  # 중복도 정리
    assert issues["hallucinated_refs"] == ["kospi_per", "vix_level"]
    # 전부 환각이면 거부는 아니고 노트로 남긴다 (관찰 인용 없음 경고)
    spec2, issues2 = verify(_good_spec(observation_refs=["vix_level"]), ctx)
    assert spec2 is not None and spec2.observation_refs == []
    assert any("비었다" in n for n in issues2["notes"])
    print("  환각 observation_refs 제거  OK")


def _check_verify_data_products():
    ctx = summarize_regime(list(_REGIME_ROWS_DESC))
    spec, issues = verify(_good_spec(
        required_data_products=["krx-basket-daily/v1", "us-equity-minute/v9"]),
        ctx)
    assert spec is not None
    assert spec.required_data_products == ["krx-basket-daily/v1"]
    assert issues["removed_data_products"] == ["us-equity-minute/v9"]
    # 실존 데이터가 하나도 안 남으면 실험 불가 - 거부
    spec2, issues2 = verify(_good_spec(
        required_data_products=["us-equity-minute/v9"]), ctx)
    assert spec2 is None and "실험 불가" in issues2["rejected"]
    print("  미실존 data_product 제거    OK")


def _check_verify_empty_title_rationale():
    ctx = summarize_regime(list(_REGIME_ROWS_DESC))
    spec, issues = verify(_good_spec(title="   "), ctx)
    assert spec is None and "title" in issues["rejected"]
    spec2, issues2 = verify(_good_spec(rationale=""), ctx)
    assert spec2 is None and "rationale" in issues2["rejected"]
    print("  빈 title/rationale 거부     OK")


def _check_register_duplicate():
    db = _FakeConn()
    spec = _good_spec()
    hid1, created1 = register(spec, conn=db)
    assert created1 and db.commits == 1 and len(db.inserts) == 1
    ins = db.inserts[0]
    assert ins[0] == spec.title and ins[5] == AGENT_VERSION
    uuid.UUID(ins[6])                                   # trace_id 형식
    edge = json.loads(ins[2])
    assert edge["horizon_days"] == 5
    assert edge["observation_refs"] == spec.observation_refs  # edge 에 동승
    assert json.loads(ins[3]) == spec.falsification_criteria
    assert json.loads(ins[4]) == ["krx-basket-daily/v1"]

    # 같은 title 재등록 - 거부하고 기존 id 반환, insert/commit 없음
    hid2, created2 = register(spec, conn=db)
    assert hid2 == hid1 and not created2
    assert len(db.inserts) == 1 and db.commits == 1

    # REJECTED 로 죽은 가설의 제목은 재사용을 막지 않는다
    db.rows["죽은 가설"] = {"id": "old-id", "status": "REJECTED", "params": None}
    hid3, created3 = register(_good_spec(title="죽은 가설"), conn=db)
    assert created3 and hid3 != "old-id"
    print("  중복 title 등록 거부        OK")


def _check_run_pipeline():
    def fake_get(url):
        assert "/regime/daily" in url and f"days={REGIME_DAYS}" in url, url
        return list(_REGIME_ROWS_DESC)

    def fake_llm(_s, _u):  # 환각 ref + 미실존 데이터를 탑재한 그럴듯한 가설
        return _good_spec(
            observation_refs=["latest_pct_above_sma20", "kospi_per"],
            required_data_products=["krx-basket-daily/v1", "us-equity-minute/v9"],
        ).model_dump_json()

    db = _FakeConn()
    out = run(register_db=True, llm=fake_llm, get=fake_get, conn=db)
    assert out["status"] == "REGISTERED" and out["registered"], out["status"]
    assert out["issues"]["hallucinated_refs"] == ["kospi_per"]
    assert out["issues"]["removed_data_products"] == ["us-equity-minute/v9"]
    assert out["spec"]["required_data_products"] == ["krx-basket-daily/v1"]
    assert out["spec"]["observation_refs"] == ["latest_pct_above_sma20"]

    # 재실행: 같은 title - DUPLICATE 로 기존 id 반환, insert 는 그대로 1건
    out2 = run(register_db=True, llm=fake_llm, get=fake_get, conn=db)
    assert out2["status"] == "DUPLICATE" and not out2["registered"]
    assert out2["hypothesis_id"] == out["hypothesis_id"]
    assert len(db.inserts) == 1

    # 기본값은 등록하지 않는다 - VERIFIED 에서 멈추고 DB 를 건드리지 않음
    db3 = _FakeConn()
    out3 = run(llm=fake_llm, get=fake_get, conn=db3)
    assert out3["status"] == "VERIFIED" and "hypothesis_id" not in out3
    assert not db3.executed

    # falsification 1개짜리 가설은 REJECTED - 등록 시도조차 없다
    db4 = _FakeConn()
    out4 = run(register_db=True, get=fake_get, conn=db4,
               llm=lambda _s, _u: _good_spec(
                   falsification_criteria=["하나뿐"]).model_dump_json())
    assert out4["status"] == "REJECTED" and not db4.executed
    print("  run 파이프라인·중복 거부    OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        reg = "--register" in sys.argv
        print(f"{AGENT_VERSION} 실측: market-api /regime/daily + Ollama {MODEL}"
              + (" + DB 등록" if reg else " (등록 없음)"))
        result = run(register_db=reg)
        _print_result(result)
        raise SystemExit(0 if result["status"] in
                         ("VERIFIED", "REGISTERED", "DUPLICATE") else 1)

    print(f"{AGENT_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_context_math()
    _check_context_fail_closed()
    _check_generate_roundtrip()
    _check_verify_falsification_reject()
    _check_verify_hallucinated_refs()
    _check_verify_data_products()
    _check_verify_empty_title_rationale()
    _check_register_duplicate()
    _check_run_pipeline()
    print("strategy-hypothesis-agent 9개 영역 통과. 실측은 --run [--register]")
