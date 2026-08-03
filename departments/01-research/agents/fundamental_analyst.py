#!/usr/bin/env python3
"""fundamental-analyst - 리서치본부 RES-05 펀더멘털 분석가 실행 실체.

담당: 재일 (리서치/퀀트)
근거: departments/01-research/hermes/config.yaml 의 fundamental-analyst 페르소나
      (RES-05: PIT 정합 재무 평가, 보고 기간과 공시 시점 분리, 없는 재무를
      추정치로 사실처럼 내지 않는다)
      CLAUDE.md 개발 원칙 - 재무 집계·비율·증감 계산은 전부 결정론 코드,
      LLM 은 해석 서술만. LLM 출력은 Pydantic 검증(news_sentiment_analyst 와
      같은 판정-검증 분리 패턴).

흐름 (fetch -> compute -> narrate -> verify):
  1. fetch    research-api /evidence/financials 만 읽는다. **DB 직접 접근 금지.**
  2. compute  결정론 - 최신 기간의 핵심 계정, YoY, 영업이익률·부채비율을
              계산하고 각 값에 (period_end, published_at) 출처를 남긴다.
  3. narrate  LLM(agent-research)은 readout 의 값을 **인용해 서술만** 한다.
              재계산·외부지식 금지. 출력은 FundamentalNote 로 검증, 실패 1회 재시도.
  4. verify   결정론 검증 - 환각 필드 제거(개수로 드러낸다), 서술 속 % 가
              readout 값 ±0.1%p 밖이면 플래그, YoY 부호와 stance 정면 모순이면
              MIXED 강등 + 사유.

설계 결정 (근거를 남긴다):
  - account_code 는 opendart_financial.py 의 `{sj_div}:{account_nm}` scheme
    (dart_major_account_nm)이다 - 표준 IFRS 코드가 아니다. 실측(2026-08-01,
    /evidence/financials?symbol=000660)으로 확인한 계정: BS:부채총계/자본총계/
    자산총계/유동·비유동 자산·부채/자본금/이익잉여금, IS:매출액/영업이익/
    당기순이익(손실)/법인세차감전 순이익/총포괄손익. 매칭은 접두사(sj_div)를
    떼고 공백을 지운 이름으로 방어적으로 하되, 금융업 변형(영업수익 등)도
    후보에 둔다 - 없는 계정은 None 이다.
  - 수집기가 **당기만 적재**하므로(opendart_financial.py 결정 2) 전년 동기
    대응 행이 없는 것이 현재 정상 상태다. 이때 YoY 는 None("미확인")이며
    절대 metadata 의 전기 참고값으로 채우지 않는다 - revision 규칙이 없어
    어느 개정본인지 구분할 수 없기 때문이다.
  - consolidation_scope 는 CONSOLIDATED 우선, 없으면 SEPARATE 로 폴백하고
    폴백 사실을 cautions 에 남긴다. 두 scope 를 섞지 않는다.
  - 자본총계가 0 이거나 음수(자본잠식)면 부채비율은 None 이다 - 숫자를
    지어내지 않는다.

이 출력은 **Agent Decision 이 아니다.** Research Packet 의 입력 재료다
(CLAUDE.md: Agent Decision != Signal != Order).

실행:
  python agents/fundamental_analyst.py               # 자체 점검 (LLM·API 없음)
  python agents/fundamental_analyst.py --run 000660 [--as-of ISO]
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

# LLM 호출·서술 재시도의 단일 출처 - agents/ 를 스크립트로 실행하면 본부 루트가
# sys.path 에 없어 evidence/ 를 직접 넣는다(다른 분석가와 같은 관례)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evidence"))
from llm_client import chat as llm_chat  # noqa: E402
from number_guard import flag_unmatched  # noqa: E402
from llm_client import narrate as llm_narrate  # noqa: E402
from fundamental_scores import f_score, altman_z  # noqa: E402
from sector_baselines import (annotate as sector_annotate,  # noqa: E402
                              cautions_for, resolve_sector)

AGENT_VERSION = "research-fundamental-analyst-v1"
KST = timezone(timedelta(hours=9))

RESEARCH_API = os.environ.get("RESEARCH_API_URL", "http://127.0.0.1:8035")
OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
# 팀 표준 로컬 모델(departments/01-research/Modelfile). news_sentiment_analyst 와 동일.
MODEL = os.environ.get(
    "FUNDAMENTAL_MODEL",
    # 2026-08-01 역할 분담 실측: 2.3초, 환각 0·미확인 정직 처리. 근거: 가이드 J4.
    "exaone3.5:7.8b")
LLM_TIMEOUT = 120  # --run 실측 상한(초)

# 핵심 계정 -> (매칭 후보 이름들). sj_div 접두사·공백 제거 후 비교한다.
# 후보에 변형을 두는 이유 - DART 주요계정 이름이 회사·업종(금융업)에 따라 다르다.
_TARGETS: dict[str, tuple[str, ...]] = {
    "매출액": ("매출액", "수익(매출액)", "영업수익"),
    "영업이익": ("영업이익", "영업이익(손실)", "영업손익"),
    "당기순이익": ("당기순이익", "당기순이익(손실)", "연결당기순이익", "당기순손익"),
    "자본총계": ("자본총계",),
    "부채총계": ("부채총계",),
}
_STANCES = ("POSITIVE", "NEGATIVE", "MIXED", "INSUFFICIENT_DATA")


# ---------------------------------------------------------------------------
# LLM 출력 계약 (개발 원칙 2: 항상 Pydantic 으로 검증)
# ---------------------------------------------------------------------------

class FundamentalNote(BaseModel):
    stance: Literal["POSITIVE", "NEGATIVE", "MIXED", "INSUFFICIENT_DATA"]
    summary: str = Field(min_length=1, max_length=600, description="한국어 2~3문장")
    used_fields: list[str]
    cautions: list[str] = []

    # 소형 모델이 소문자·공백 섞인 stance 를 내는 경우가 있다 - 정규화해 받는다.
    # 목록 밖 값은 여전히 Literal 이 거부한다.
    @field_validator("stance", mode="before")
    @classmethod
    def _norm_stance(cls, v):
        return str(v).strip().upper()

    @field_validator("used_fields", "cautions", mode="before")
    @classmethod
    def _coerce_strs(cls, v):
        return [str(x).strip() for x in v] if isinstance(v, list) else v


# ---------------------------------------------------------------------------
# 1. fetch - research-api 만 안다
# ---------------------------------------------------------------------------

def fetch_financials(symbol: str, *, as_of: Optional[datetime] = None,
                     api_base: str = RESEARCH_API) -> list[dict]:
    url = f"{api_base}/evidence/financials?symbol={symbol}&limit=500"
    if as_of is not None:
        url += "&as_of=" + urllib.parse.quote(as_of.isoformat())
    # 페르소나를 밝힌다(Tool Gateway 이행, 2026-08-02)
    from api_client import get_json

    return get_json(url, persona="fundamental-analyst", timeout=20)


# ---------------------------------------------------------------------------
# 2. compute - 순수 함수. 모든 숫자는 여기서만 나온다
# ---------------------------------------------------------------------------

def _pdate(v) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _account_label(row: dict) -> str:
    """account_name 이 있으면 그것을, 없으면 account_code 의 sj_div 접두사를 뗀
    이름을 쓴다 - /evidence/financials 는 account_code 만 준다(실측)."""
    name = str(row.get("account_name") or "").strip()
    if not name:
        code = str(row.get("account_code") or "")
        name = code.split(":", 1)[1] if ":" in code else code
    return re.sub(r"\s+", "", name)


def _norm_candidates(cands: tuple[str, ...]) -> set[str]:
    return {re.sub(r"\s+", "", c) for c in cands}


def _pick_row(rows: list[dict]) -> tuple[dict, list[str]]:
    """같은 (계정, 기간) 행이 여럿이면 정정공시 가능성 - 최신 published_at 을 쓰되
    값이 상충하면 조용히 넘기지 않고 사유를 돌려준다."""
    rows = sorted(rows, key=lambda r: str(r.get("published_at") or ""), reverse=True)
    cautions = []
    vals = {r.get("value") for r in rows}
    if len(vals) > 1:
        cautions.append(
            f"{rows[0].get('account_code')}: 같은 기간 값 상충 {len(rows)}건 - "
            f"최신 공시({rows[0].get('published_at')}) 값 사용"
        )
    return rows[0], cautions


def _yoy_pct(cur: Optional[float], prior: Optional[float]) -> Optional[float]:
    # 전기가 0 이면 증감률이 정의되지 않는다. 음수 전기는 절대값 분모(관행) -
    # 부호 해석은 cautions 가 아니라 값 자체가 아닌 서술 검증이 지킨다.
    if cur is None or prior is None or prior == 0:
        return None
    return round((cur - prior) / abs(prior) * 100.0, 2)


def compute_fundamental_readout(facts: list[dict]) -> dict:
    """research-api 재무 사실 목록 -> 결정론 readout. LLM 은 여기 관여하지 않는다."""
    cautions: list[str] = []
    if not facts:
        return {"verdict": "INSUFFICIENT_DATA", "scope": None, "period_end": None,
                "fields": {}, "cautions": ["재무 Evidence 가 0건"]}

    # scope 는 섞지 않는다 - 연결 우선, 없으면 별도 폴백(사실을 남긴다)
    scopes = {str(r.get("consolidation_scope") or "") for r in facts}
    if "CONSOLIDATED" in scopes:
        scope = "CONSOLIDATED"
    elif "SEPARATE" in scopes:
        scope = "SEPARATE"
        cautions.append("연결(CONSOLIDATED) 데이터가 없어 별도(SEPARATE) 사용")
    else:
        return {"verdict": "INSUFFICIENT_DATA", "scope": None, "period_end": None,
                "fields": {}, "cautions": ["알 수 없는 consolidation_scope: "
                                           + ",".join(sorted(scopes))]}
    rows = [r for r in facts if r.get("consolidation_scope") == scope]

    latest = max((d for d in (_pdate(r.get("period_end")) for r in rows) if d),
                 default=None)
    if latest is None:
        return {"verdict": "INSUFFICIENT_DATA", "scope": scope, "period_end": None,
                "fields": {}, "cautions": ["period_end 를 읽을 수 없다"]}
    # 전년 동기 = 같은 월·일, 한 해 전 (2/29 는 2/28 로 - 결산일에는 없는 날짜다)
    try:
        prior_pe = latest.replace(year=latest.year - 1)
    except ValueError:
        prior_pe = latest.replace(year=latest.year - 1, day=28)

    fields: dict[str, dict] = {}
    matched_any = False
    for key, cands in _TARGETS.items():
        names = _norm_candidates(cands)
        cur_rows = [r for r in rows
                    if _pdate(r.get("period_end")) == latest
                    and _account_label(r) in names]
        prior_rows = [r for r in rows
                      if _pdate(r.get("period_end")) == prior_pe
                      and _account_label(r) in names]
        if not cur_rows:
            fields[key] = {"value": None, "period_end": str(latest),
                           "published_at": None, "account_code": None,
                           "note": "해당 계정 없음"}
        else:
            row, dup_c = _pick_row(cur_rows)
            cautions += dup_c
            matched_any = True
            v = row.get("value")
            fields[key] = {
                "value": float(v) if v is not None else None,
                "period_end": str(latest),
                "published_at": str(row.get("published_at")),
                "account_code": row.get("account_code"),
            }
        # YoY - 대응 행이 실제로 있을 때만. 없으면 None("미확인")이 정답이다.
        yoy = {"value": None, "period_end": str(latest),
               "prior_period_end": None, "prior_published_at": None}
        if cur_rows and prior_rows:
            prow, dup_c = _pick_row(prior_rows)
            cautions += dup_c
            yoy["value"] = _yoy_pct(fields[key]["value"], prow.get("value"))
            yoy["prior_period_end"] = str(prior_pe)
            yoy["prior_published_at"] = str(prow.get("published_at"))
        fields[key + "_yoy_pct"] = yoy

    if not matched_any:
        return {"verdict": "INSUFFICIENT_DATA", "scope": scope,
                "period_end": str(latest), "fields": {},
                "cautions": cautions + ["핵심 계정이 하나도 매칭되지 않았다"]}

    def _val(k):
        return fields[k]["value"]

    def _ratio(num_k, den_k, *, den_must_be_positive=False):
        num, den = _val(num_k), _val(den_k)
        if num is None or den is None or den == 0:
            return None
        if den_must_be_positive and den < 0:
            return None
        return round(num / den * 100.0, 2)

    margin = _ratio("영업이익", "매출액")
    debt = _ratio("부채총계", "자본총계", den_must_be_positive=True)
    if _val("자본총계") is not None and _val("자본총계") < 0:
        cautions.append("자본총계가 음수(자본잠식) - 부채비율 미산출")
    base = fields["영업이익"] if _val("영업이익") is not None else fields["매출액"]
    fields["영업이익률_pct"] = {"value": margin, "period_end": str(latest),
                               "published_at": base.get("published_at"),
                               "inputs": ["영업이익", "매출액"]}
    fields["부채비율_pct"] = {"value": debt, "period_end": str(latest),
                             "published_at": fields["부채총계"].get("published_at"),
                             "inputs": ["부채총계", "자본총계"]}

    # ── 종합 점수: F-Score / Altman Z ────────────────────────────────────────
    # ▶ evidence/fundamental_scores.py 는 진작 구현돼 있었고 methods.py 레지스트리에
    #   등재까지 돼 있었는데 **호출처가 0개**였다. 선언은 있고 실재는 없는 상태 -
    #   레지스트리를 믿고 "우리는 F-Score 를 본다" 고 말할 수 있어서 가장 나쁘다.
    #   개별 비율 두 개(영업이익률·부채비율)로는 재무 건전성을 말하기 어렵고,
    #   RES-05 가 인용할 확정치가 얇았던 것도 여기서 왔다.
    def _by_code(period) -> dict:
        m: dict = {}
        for r in rows:
            if _pdate(r.get("period_end")) != period:
                continue
            code, v = r.get("account_code"), r.get("value")
            if code is None or v is None:
                continue
            m.setdefault(str(code), float(v))     # 중복은 _pick_row 규율과 같이 첫 행
        return m

    cur_map, prior_map = _by_code(latest), _by_code(prior_pe)
    try:
        fs = f_score(cur_map, prior_map or None)
        zs = altman_z(cur_map)
    except Exception as e:                        # 종합 점수 실패가 재무 readout 을
        cautions.append(f"종합 점수 산출 실패: {type(e).__name__}")  # 통째로 죽이지 않는다
    else:
        # ▶ **분모를 감추지 않는다.** 6신호 중 2개만 계산됐는데 "F-Score 2점" 만
        #   쓰면 나쁜 회사처럼 읽힌다. 계산 가능 수를 항상 함께 싣는다.
        fields["f_score"] = {
            "value": fs.score, "available": fs.available, "label": fs.label,
            "period_end": str(latest),
            "signals": {sg.key: sg.passed for sg in fs.signals},
            "unavailable": list(fs.unavailable),
        }
        z = zs.as_dict()
        fields["altman_z"] = {
            "value": z["altman_z"], "label": z["altman_label"],
            "period_end": str(latest),
            # x4(시가총액/부채)는 재무제표만으로는 못 구해 장부가 대용을 쓴다.
            # 대용인 사실을 지우면 원본 Z 로 읽힌다.
            "x4_is_proxy": z["altman_x4_is_proxy"],
            "components": z["altman_components"],
        }
        # ▶ 구성요소를 **fields 최상위로도 올린다.** keyed_pool 은 컨테이너 한 겹만
        #   펼치므로 altman_z.components 안의 값은 확정치 풀에 안 들어간다.
        #   그대로 두면 LLM 이 "운전자본/자산 0.02" 라고 정확히 인용해도 창작으로
        #   몰린다 - 가드가 정상 인용을 위반으로 만들면 사람이 가드를 무시한다.
        for ck, cv in (z["altman_components"] or {}).items():
            if cv is not None:
                fields[f"altman_{ck}"] = {"value": cv, "period_end": str(latest),
                                          "inputs": ["altman_z"]}
        if not prior_map:
            cautions.append(f"전기({prior_pe}) 재무가 없어 F-Score 변화 신호 4개 제외")

    # ── 업종 문맥 ────────────────────────────────────────────────────────────
    # ▶ 실측(006800 미래에셋증권 2026-08-04): 부채비율 1015.01% 를 인사이트가
    #   "외부 충격에 대한 완충이 거의 없다" 로 읽었다. **증권사는 고객예탁금·
    #   차입이 구조적으로 부채에 잡혀 레버리지가 원래 높다** - 제조업 기준을
    #   그대로 들이댄 오독이다. 숫자도 계산도 맞았고 틀린 것은 문맥이었다.
    #
    #   재료가 업종을 안 실으면 분석가도 총괄도 인사이트도 같은 오독을
    #   반복한다 - 그 위에서 무엇을 해도 안 고쳐진다.
    # 공식 업종코드(KSIC)를 우선한다. /evidence/financials 가 issuers 를 이미
    # join 하고 있었는데 industry_code 를 안 실어서 몰랐다 - /bars 의 notional
    # 과 같은 유형이다. 코드가 없으면 계정 구조로 추론한다.
    ksic = next((r.get("industry_code") for r in rows if r.get("industry_code")),
                None)
    sector, sector_why = resolve_sector(
        ksic, [r.get("account_code") for r in rows])
    fields = sector_annotate(fields, sector, sector_why)
    cautions += cautions_for(sector, sector_why)

    return {"verdict": "OK", "scope": scope, "period_end": str(latest),
            "sector": sector, "fields": fields, "cautions": cautions}


# ---------------------------------------------------------------------------
# 3. narrate - LLM 은 여기서만 쓴다 (서술만, 숫자는 인용만)
# ---------------------------------------------------------------------------

_SYSTEM = """너는 리서치본부 펀더멘털 분석가(RES-05)다.
주어진 readout(결정론 코드가 계산한 재무 수치)만으로 한국어 해석 서술을 쓴다.

규칙 - 반드시 지켜라:
- 제공된 필드만 인용한다. 재계산 금지, 외부지식 금지, 다른 수치 창작 금지.
- % 수치는 제공된 값을 그대로(소수점 그대로) 인용한다. 표시값(예: 97.1조원)도
  제공된 문자열 그대로 쓴다.
- value 나 yoy 가 null 인 항목은 "미확인"으로 서술한다. 추정으로 채우지 않는다.
- stance: POSITIVE | NEGATIVE | MIXED | INSUFFICIENT_DATA 중 하나.
  YoY 가 전부 미확인이면 수익성·재무구조 수준만으로 조심스럽게 정하고
  cautions 에 "전년 대비 미확인"을 남긴다.
- used_fields 에는 summary 에서 실제 인용한 필드 키만 넣는다(주어진 키 그대로).
- summary 는 한국어 2~3문장, 결론 먼저. 투자 권유 표현 금지.

출력은 이 JSON 만 (설명·사족 금지):
{"stance":"POSITIVE","summary":"...","used_fields":["매출액","영업이익률_pct"],"cautions":["..."]}"""


def _fmt_krw(v: Optional[float]) -> Optional[str]:
    """표시값을 결정론으로 만들어 준다 - 소형 모델의 단위 환산 실수를 막는다."""
    if v is None:
        return None
    a = abs(v)
    if a >= 1e12:
        return f"{v / 1e12:.1f}조원"
    if a >= 1e8:
        return f"{v / 1e8:.0f}억원"
    return f"{v:,.0f}원"


def narrate(readout: dict, symbol: str, llm=None) -> FundamentalNote:
    """readout -> FundamentalNote. Schema 불합격이면 한 번 고쳐 부르고, 또 실패하면
    예외다 - 서술을 지어내지 않는다(호출부가 LLM_UNAVAILABLE 처리)."""
    if readout.get("verdict") == "INSUFFICIENT_DATA":
        # 데이터가 없으면 LLM 을 부를 이유가 없다 - 결정론으로 판단 불가를 말한다
        return FundamentalNote(
            stance="INSUFFICIENT_DATA",
            summary=f"{symbol}: 재무 Evidence 가 없어 펀더멘털 판단 불가.",
            used_fields=[], cautions=list(readout.get("cautions", [])))

    payload = {"symbol": symbol, "scope": readout["scope"],
               "period_end": readout["period_end"], "fields": {}}
    for k, info in readout["fields"].items():
        # ▶ 밑줄 접두는 **수치가 아니라 메타**다(_sector 등). value 를 기대하고
        #   읽으면 KeyError 로 서술이 통째로 죽는다 - 필드 표에 메타를 섞을
        #   때마다 여기가 깨지므로 규약으로 못박는다.
        if k.startswith("_") or not isinstance(info, dict) or "value" not in info:
            continue
        f = {"value": info["value"], "period_end": info.get("period_end"),
             "published_at": info.get("published_at")}
        if not k.endswith("_pct"):
            f["표시값"] = _fmt_krw(info["value"])
        payload["fields"][k] = f
    prompt = (f"인용 가능한 필드 키: {list(readout['fields'])}\n"
              "readout:\n" + json.dumps(payload, ensure_ascii=False, indent=1))

    return llm_narrate(_SYSTEM, prompt, FundamentalNote, llm or _ollama_call)


def _ollama_call(system: str, user: str) -> str:
    """로컬/팀 Ollama. 호출 모양은 evidence/llm_client 가 단일 출처다.

    여기 남는 것은 **이 분석가의 설정**뿐이다(모델·타임아웃). 예전에는 요청
    조립까지 7곳에 복붙돼 timeout 이 20/30/LLM_TIMEOUT 으로 갈라져 있었다.
    """
    return llm_chat(system, user, base=OLLAMA_BASE, model=MODEL,
                    timeout=LLM_TIMEOUT)


# ---------------------------------------------------------------------------
# 4. verify - 결정론 검증. LLM 이 손대지 못한다
# ---------------------------------------------------------------------------

def verify(note: FundamentalNote, readout: dict) -> dict:
    fields = readout.get("fields", {})

    # 4a. 환각 필드 - readout 에 없는 키는 버리되 개수로 드러낸다
    kept = [f for f in note.used_fields if f in fields]
    halluc = [f for f in note.used_fields if f not in fields]

    # 4b. 서술 속 수치가 readout 값과 안 맞으면 플래그.
    # 규칙은 evidence/number_guard 가 단일 출처다. 예전에는 여기만 (a) % 만 보고
    # (b) **부호 반전을 허용하지 않았다** - 코드가 -3.2 를 냈는데 서술이
    # "3.2% 감소"라고 쓰는 정상 문장을 위반으로 잡았다(2026-08-02 통일).
    known = [info["value"] for k, info in fields.items()
             if k.endswith("_pct") and info.get("value") is not None]
    pct_flags = [f"서술의 {raw} 가 readout 어느 값과도 ±0.1 안에 없다"
                 for raw in flag_unmatched(note.summary, known)]

    # 4c. YoY 부호와 stance 의 정면 모순 - 전 항목이 반대 부호면 MIXED 강등
    yoys = [info["value"] for k, info in fields.items()
            if k.endswith("_yoy_pct") and info.get("value") is not None]
    demotion = None
    if yoys:
        if note.stance == "POSITIVE" and all(v < 0 for v in yoys):
            demotion = "확인된 YoY 가 전부 감소인데 POSITIVE - MIXED 강등"
        elif note.stance == "NEGATIVE" and all(v > 0 for v in yoys):
            demotion = "확인된 YoY 가 전부 증가인데 NEGATIVE - MIXED 강등"

    cleaned = note.model_copy(update={
        "stance": "MIXED" if demotion else note.stance,
        "used_fields": kept,
        "cautions": note.cautions + ([demotion] if demotion else []),
    })
    return {"note": cleaned, "hallucinated_fields": halluc,
            "hallucination_count": len(halluc), "pct_flags": pct_flags,
            "demoted": bool(demotion), "demotion_reason": demotion}


# ---------------------------------------------------------------------------
# 파이프라인
# ---------------------------------------------------------------------------

def analyze(symbol: str, *, as_of: Optional[datetime] = None, llm=None,
            api_base: str = RESEARCH_API) -> dict:
    """API 조회 -> 계산 -> 서술 -> 검증. API 장애는 예외로 드러난다(fail-closed).
    LLM 장애만 결정론 readout 으로 강등한다 - 숫자는 이미 코드가 만들었다."""
    facts = fetch_financials(symbol, as_of=as_of, api_base=api_base)
    readout = compute_fundamental_readout(facts)
    result = {"agent": AGENT_VERSION, "symbol": symbol,
              "as_of": (as_of or datetime.now(timezone.utc)).isoformat(),
              "readout": readout}

    if readout["verdict"] == "INSUFFICIENT_DATA":
        note = narrate(readout, symbol)  # LLM 안 부른다
        result.update(verdict="INSUFFICIENT_DATA", note=note.model_dump(),
                      verification=None, llm="SKIPPED")
        return result

    try:
        note = narrate(readout, symbol, llm=llm)
    except (RuntimeError, OSError, ValueError) as e:
        result.update(verdict="READOUT_ONLY", note=None, verification=None,
                      llm="LLM_UNAVAILABLE", detail=str(e)[:200])
        return result

    v = verify(note, readout)
    result.update(verdict="NOTED", note=v["note"].model_dump(),
                  verification={k: v[k] for k in
                                ("hallucinated_fields", "hallucination_count",
                                 "pct_flags", "demoted", "demotion_reason")},
                  llm=MODEL)
    return result


# ---------------------------------------------------------------------------
# 자체 점검 - LLM·API 없이 (합성 fact, 가짜 LLM 주입)
# ---------------------------------------------------------------------------

def _fact(code: str, value, period_end: str, *, scope="CONSOLIDATED",
          published_at="2026-03-16T15:00:00+00:00") -> dict:
    return {"account_code": code, "value": value, "unit": "ABSOLUTE",
            "currency": "KRW", "period_end": period_end,
            "consolidation_scope": scope, "published_at": published_at,
            "observed_at": "2026-07-31T14:20:10+00:00"}


def _two_period_facts() -> list[dict]:
    cur, pri = "2025-12-31", "2024-12-31"
    old = "2025-03-15T15:00:00+00:00"
    return [
        _fact("IS:매출액", 120.0, cur), _fact("IS:매출액", 100.0, pri, published_at=old),
        _fact("IS:영업이익", 18.0, cur), _fact("IS:영업이익", 24.0, pri, published_at=old),
        _fact("IS:당기순이익(손실)", 12.0, cur),
        _fact("IS:당기순이익(손실)", 10.0, pri, published_at=old),
        _fact("BS:자본총계", 200.0, cur), _fact("BS:부채총계", 100.0, cur),
    ]


def _check_scores_wired_and_citable():
    """F-Score/Altman 이 **readout 에 실리고 확정치 풀에 들어가는가.**

    이 두 함수는 진작 구현돼 methods.py 레지스트리에 등재까지 됐는데 호출처가
    0개였다 - 선언과 실재가 어긋난 상태다. 배선만 하고 풀에 안 넣으면 LLM 이
    정확히 인용해도 창작으로 몰리므로 둘을 같이 검사한다.
    """
    from number_guard import keyed_pool

    def _rows(pe, ni, at, cl, ca, ncl, rev, op):
        d = {"IS:당기순이익(손실)": ni, "BS:자산총계": at, "BS:유동부채": cl,
             "BS:유동자산": ca, "BS:비유동부채": ncl, "IS:매출액": rev,
             "IS:영업이익": op, "BS:부채총계": ncl + cl,
             "BS:자본총계": at - (ncl + cl), "BS:이익잉여금": at * 0.3,
             "BS:자본금": at * 0.05}
        return [{"account_code": k, "account_name": k.split(":")[1],
                 "value": float(v), "period_end": pe,
                 "published_at": pe + "T00:00:00+09:00",
                 "consolidation_scope": "CONSOLIDATED"} for k, v in d.items()]

    r = compute_fundamental_readout(
        _rows("2025-12-31", 120, 1000, 200, 400, 300, 900, 150)
        + _rows("2024-12-31", 80, 950, 210, 360, 320, 850, 110))
    f = r["fields"]
    assert f["f_score"]["value"] == 5 and f["f_score"]["available"] == 6, f["f_score"]
    # 분모를 감춘 라벨은 만들지 않는다 - 6신호 중 2개만 계산됐는데 "2점" 만 쓰면
    # 나쁜 회사처럼 읽힌다
    assert f["f_score"]["available"] >= 1 and f["f_score"]["label"] != "", f["f_score"]
    assert f["altman_z"]["value"] is not None and f["altman_z"]["x4_is_proxy"] is True

    pool = keyed_pool(r)
    assert "f_score" in pool and "altman_z" in pool, sorted(pool)
    comps = [k for k in pool if k.lower().startswith("altman_x")]
    assert comps, f"Altman 구성요소가 풀 밖이다 - 정확한 인용이 창작으로 몰린다: {sorted(pool)}"

    # 전기가 없으면 변화 신호는 빠지고 그 사실이 cautions 에 남아야 한다
    solo = compute_fundamental_readout(_rows("2025-12-31", 120, 1000, 200, 400, 300, 900, 150))
    assert solo["fields"]["f_score"]["available"] < 6, solo["fields"]["f_score"]
    assert any("전기" in c for c in solo["cautions"]), solo["cautions"]


def _check_sector_context_is_wired():
    """업종 문맥이 **readout 에 실제로 실리는가.**

    모듈만 만들고 안 부르면 F-Score 때와 같은 상태가 된다 - 구현은 있는데
    호출처가 0개. 실측 오독(증권사 부채비율 1015% 를 위험으로 읽음)이
    고쳐졌는지는 여기서만 확인된다.
    """
    def _rows(pe, codes):
        return [{"account_code": k, "account_name": k.split(":")[1],
                 "value": float(v), "period_end": pe,
                 "published_at": pe + "T00:00:00+09:00",
                 "consolidation_scope": "CONSOLIDATED"}
                for k, v in codes.items()]

    # 증권사 - 006800 실측 계정 구조(예수부채, 유동 구분 없음)
    fin = _rows("2025-12-31", {
        "BS:예수부채": 50e12, "BS:당기손익-공정가치측정금융자산": 40e12,
        "BS:자산총계": 150e12, "BS:부채총계": 136e12, "BS:자본총계": 13e12,
        "BS:이익잉여금": 8e12, "BS:자본금": 1e12,
        "IS:매출액": 29e12, "IS:영업이익": 1.9e12,
        "IS:당기순이익(손실)": 1.5e12})
    r = compute_fundamental_readout(fin)
    assert r["sector"] == "FINANCIAL", r.get("sector")
    # 업종코드가 오면 그것이 우선한다(실측: 006800 = KSIC 66121)
    with_code = [dict(x, industry_code="66121") for x in fin]
    rk = compute_fundamental_readout(with_code)
    assert rk["sector"] == "FINANCIAL", rk.get("sector")
    assert "KSIC" in rk["fields"]["_sector"]["detected_by"], rk["fields"]["_sector"]
    f = r["fields"]
    # ▶ 값은 그대로 있고(사실이다), 적용 불가 표시가 붙는다
    assert f["부채비율_pct"]["value"] is not None, "값이 사라졌다"
    assert f["부채비율_pct"]["sector_applicable"] is False, f["부채비율_pct"]
    assert f["altman_z"]["sector_applicable"] is False, f["altman_z"]
    assert any("금융업" in c for c in r["cautions"]), r["cautions"]

    # 일반업 - 유동 구분이 있으면 기준이 적용된다
    gen = _rows("2025-12-31", {
        "BS:유동자산": 400.0, "BS:유동부채": 200.0, "BS:비유동부채": 300.0,
        "BS:자산총계": 1000.0, "BS:부채총계": 500.0, "BS:자본총계": 500.0,
        "BS:이익잉여금": 300.0, "BS:자본금": 50.0,
        "IS:매출액": 900.0, "IS:영업이익": 150.0,
        "IS:당기순이익(손실)": 120.0})
    r2 = compute_fundamental_readout(gen)
    assert r2["sector"] == "GENERAL", r2.get("sector")
    assert r2["fields"]["부채비율_pct"]["sector_applicable"] is True
    assert not any("금융업" in c for c in r2["cautions"]), r2["cautions"]


def _check_readout_math():
    r = compute_fundamental_readout(_two_period_facts())
    f = r["fields"]
    assert r["verdict"] == "OK" and r["scope"] == "CONSOLIDATED"
    assert r["period_end"] == "2025-12-31"
    # 손으로 계산한 정답과 대조 - 코드가 계산하고 코드가 검증한다
    assert f["매출액"]["value"] == 120.0
    assert f["매출액_yoy_pct"]["value"] == 20.0, f["매출액_yoy_pct"]
    assert f["영업이익_yoy_pct"]["value"] == -25.0
    assert f["당기순이익_yoy_pct"]["value"] == 20.0
    assert f["영업이익률_pct"]["value"] == 15.0     # 18/120
    assert f["부채비율_pct"]["value"] == 50.0       # 100/200
    # 출처 표기 - 서술 검증의 근거다
    assert f["영업이익"]["period_end"] == "2025-12-31"
    assert f["영업이익"]["published_at"] == "2026-03-16T15:00:00+00:00"
    assert f["매출액_yoy_pct"]["prior_period_end"] == "2024-12-31"
    assert f["매출액"]["account_code"] == "IS:매출액"
    print("  결정론 YoY·비율 계산     OK")


def _check_corrected_filing_and_scope():
    # 같은 (계정, 기간)에 정정공시 - 최신 published_at 값을 쓰고 상충을 드러낸다
    facts = _two_period_facts() + [
        _fact("IS:매출액", 121.0, "2025-12-31",
              published_at="2026-04-01T15:00:00+00:00")]
    r = compute_fundamental_readout(facts)
    assert r["fields"]["매출액"]["value"] == 121.0
    assert any("값 상충" in c for c in r["cautions"]), r["cautions"]
    # 연결이 없으면 별도로 폴백하되 사실을 남긴다
    sep = [_fact("IS:매출액", 50.0, "2025-12-31", scope="SEPARATE")]
    r2 = compute_fundamental_readout(sep)
    assert r2["scope"] == "SEPARATE"
    assert any("별도(SEPARATE)" in c for c in r2["cautions"])
    print("  정정공시·scope 폴백      OK")


def _check_no_prior_period():
    # 당기만 적재된 현재 데이터 상태 - YoY 는 None("미확인")이지 0 이 아니다
    facts = [f for f in _two_period_facts() if f["period_end"] == "2025-12-31"]
    r = compute_fundamental_readout(facts)
    assert r["verdict"] == "OK"
    for k in ("매출액", "영업이익", "당기순이익"):
        assert r["fields"][k + "_yoy_pct"]["value"] is None
        assert r["fields"][k + "_yoy_pct"]["prior_period_end"] is None
    print("  대응 기간 없음 -> None    OK")


def _check_division_guards():
    cur = "2025-12-31"
    # 자본 0 - 부채비율을 지어내지 않는다
    r = compute_fundamental_readout(
        [_fact("BS:자본총계", 0.0, cur), _fact("BS:부채총계", 100.0, cur)])
    assert r["fields"]["부채비율_pct"]["value"] is None
    # 자본 계정 자체가 없어도 None
    r2 = compute_fundamental_readout([_fact("BS:부채총계", 100.0, cur)])
    assert r2["fields"]["자본총계"]["value"] is None
    assert r2["fields"]["부채비율_pct"]["value"] is None
    # 자본잠식(음수) - 미산출 + 사유
    r3 = compute_fundamental_readout(
        [_fact("BS:자본총계", -50.0, cur), _fact("BS:부채총계", 100.0, cur)])
    assert r3["fields"]["부채비율_pct"]["value"] is None
    assert any("자본잠식" in c for c in r3["cautions"])
    # 매출 0 - 영업이익률 None
    r4 = compute_fundamental_readout(
        [_fact("IS:매출액", 0.0, cur), _fact("IS:영업이익", 10.0, cur)])
    assert r4["fields"]["영업이익률_pct"]["value"] is None
    # 전기 0 - YoY None
    assert _yoy_pct(10.0, 0) is None and _yoy_pct(None, 5.0) is None
    print("  0/None 나눗셈 방어        OK")


def _check_insufficient_data():
    r = compute_fundamental_readout([])
    assert r["verdict"] == "INSUFFICIENT_DATA"

    def never_llm(_s, _u):
        raise AssertionError("INSUFFICIENT_DATA 인데 LLM 이 불렸다")

    note = narrate(r, "999999", llm=never_llm)
    assert note.stance == "INSUFFICIENT_DATA" and note.used_fields == []
    print("  INSUFFICIENT_DATA        OK")


def _check_hallucinated_fields():
    r = compute_fundamental_readout(_two_period_facts())
    note = FundamentalNote(stance="MIXED", summary="테스트.",
                           used_fields=["매출액", "없는필드", "부채비율_pct"],
                           cautions=[])
    v = verify(note, r)
    assert v["hallucination_count"] == 1
    assert v["hallucinated_fields"] == ["없는필드"]
    assert v["note"].used_fields == ["매출액", "부채비율_pct"]
    print("  환각 필드 제거           OK")


def _check_pct_mismatch_flag():
    r = compute_fundamental_readout(_two_period_facts())
    ok = FundamentalNote(stance="MIXED", summary="영업이익률 15.0%, 부채비율 50.05% 수준.",
                         used_fields=["영업이익률_pct"], cautions=[])
    assert verify(ok, r)["pct_flags"] == []  # ±0.1%p 안이면 통과
    bad = FundamentalNote(stance="MIXED", summary="영업이익률은 37.5% 다.",
                          used_fields=["영업이익률_pct"], cautions=[])
    flags = verify(bad, r)["pct_flags"]
    assert flags and "37.5" in flags[0], flags
    print("  서술 % 검증(±0.1%p)      OK")


def _check_contradiction_demotion():
    cur, pri = "2025-12-31", "2024-12-31"
    down = compute_fundamental_readout([
        _fact("IS:매출액", 80.0, cur), _fact("IS:매출액", 100.0, pri),
        _fact("IS:영업이익", 5.0, cur), _fact("IS:영업이익", 10.0, pri)])
    note = FundamentalNote(stance="POSITIVE", summary="좋다.",
                           used_fields=["매출액"], cautions=[])
    v = verify(note, down)
    assert v["demoted"] and v["note"].stance == "MIXED"
    assert any("MIXED 강등" in c for c in v["note"].cautions)
    # 반대 방향도 - 전부 증가인데 NEGATIVE
    up = compute_fundamental_readout(_two_period_facts())
    up["fields"]["영업이익_yoy_pct"]["value"] = 5.0
    v2 = verify(FundamentalNote(stance="NEGATIVE", summary="나쁘다.",
                                used_fields=["매출액"], cautions=[]), up)
    assert v2["demoted"] and v2["note"].stance == "MIXED"
    # 부호가 섞였으면 강등하지 않는다 - 그건 해석의 영역이다
    mixed = compute_fundamental_readout(_two_period_facts())
    v3 = verify(FundamentalNote(stance="POSITIVE", summary="좋다.",
                                used_fields=["매출액"], cautions=[]), mixed)
    assert not v3["demoted"]
    print("  모순 강등(MIXED)         OK")


def _check_fake_llm_roundtrip():
    r = compute_fundamental_readout(_two_period_facts())
    good = json.dumps({"stance": "mixed", "summary": "영업이익률 15.0% 로 확인.",
                       "used_fields": ["영업이익률_pct"], "cautions": []},
                      ensure_ascii=False)
    note = narrate(r, "005930", llm=lambda s, u: good)
    assert note.stance == "MIXED"  # 소문자 stance 정규화까지 왕복 확인

    calls = {"n": 0}

    def flaky(_s, _u):
        calls["n"] += 1
        return "JSON 아님" if calls["n"] == 1 else good

    assert narrate(r, "005930", llm=flaky).stance == "MIXED" and calls["n"] == 2

    try:
        narrate(r, "005930", llm=lambda s, u: "no json ever")
        raise AssertionError("Schema 위반이 두 번인데 통과했다")
    except RuntimeError:
        pass
    print("  가짜 LLM 왕복/재시도     OK")


def _check_api_fail_closed():
    # 닫힌 포트 - API 장애가 조용히 INSUFFICIENT_DATA 로 둔갑하면 안 된다
    analyze("005930", llm=lambda s, u: "안 불려야 한다",
            api_base="http://127.0.0.1:1")
    raise AssertionError("닫힌 API 가 조용히 지나갔다")


def _print_run(result: dict):
    r = result["readout"]
    print(f"  verdict={result['verdict']} scope={r['scope']} "
          f"period_end={r['period_end']} llm={result['llm']}")
    for k in _TARGETS:
        info = r["fields"].get(k)
        if info is None:
            continue
        yoy = r["fields"][k + "_yoy_pct"]["value"]
        print(f"    {k:6} {_fmt_krw(info['value']) or '미확인':>12}  "
              f"YoY={'미확인' if yoy is None else f'{yoy:+.2f}%'}  "
              f"({info['period_end']}, 공시 {str(info['published_at'])[:10]}, "
              f"{info['account_code']})")
    for k in ("영업이익률_pct", "부채비율_pct"):
        info = r["fields"].get(k, {})
        v = info.get("value")
        print(f"    {k:12} {'미확인' if v is None else f'{v:.2f}%'}")
    if r["cautions"]:
        print(f"    readout cautions: {r['cautions']}")
    note = result.get("note")
    if note:
        print(f"  note.stance={note['stance']}")
        print(f"  note.summary={note['summary']}")
        print(f"  note.used_fields={note['used_fields']}")
        if note["cautions"]:
            print(f"  note.cautions={note['cautions']}")
    v = result.get("verification")
    if v:
        print(f"  검증: 환각 필드 제거 {v['hallucination_count']}"
              f"{' ' + str(v['hallucinated_fields']) if v['hallucinated_fields'] else ''}"
              f", pct_flags={v['pct_flags'] or '없음'}, demoted={v['demoted']}")
    if result.get("detail"):
        print(f"  detail: {result['detail']}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        a = sys.argv
        def opt(n, d): return a[a.index(n) + 1] if n in a else d
        sym = opt("--run", "005930")
        as_of = opt("--as-of", None)
        ts = datetime.fromisoformat(as_of) if as_of else None
        print(f"{AGENT_VERSION} 실행: {sym}")
        _print_run(analyze(sym, as_of=ts))
        raise SystemExit(0)

    print(f"{AGENT_VERSION} 자체 점검 (LLM·API 없음)")
    _check_readout_math()
    _check_scores_wired_and_citable()
    _check_sector_context_is_wired()
    _check_corrected_filing_and_scope()
    _check_no_prior_period()
    _check_division_guards()
    _check_insufficient_data()
    _check_hallucinated_fields()
    _check_pct_mismatch_flag()
    _check_contradiction_demotion()
    _check_fake_llm_roundtrip()
    try:
        _check_api_fail_closed()
    except AssertionError:
        raise
    except Exception:
        print("  API 장애 fail-closed     OK")  # URLError 등 - 조용히 넘기지 않는다
    print("펀더멘털 분석가 10개 영역 통과. 실행은 --run <종목코드>")
