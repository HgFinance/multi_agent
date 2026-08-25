"""2층 - 뉴스·공시를 읽어 호재/악재 축을 만든다.

1층·1.5층이 숫자로 좁힌 후보에만 붙는다. 여기서만 LLM 을 쓰고, 쓰는 범위는
CLAUDE.md 가 정한 그대로다 - **관련성 판단과 서술.** 목표가·손절가·비중은
여기서 절대 안 나온다(그건 instrument_scoring 의 결정론 몫이다).

## 네이버 뉴스는 종목명으로 검색하면 노이즈가 많다

실측 2026-08-24: "삼성전자" 검색 1위가 연예 기사('S전자 부장♥' 이현이…)였다.
그래서 두 단계로 거른다.
  1) 결정론 사전 필터 - 제목·본문에 회사명이 없으면 LLM 에 보내지도 않는다.
  2) LLM 관련성 판정 - 회사명이 들어 있어도 주가와 무관한 기사가 있다
     (인사·협찬·부고). LLM 이 relevant=false 로 떨어뜨린다.

## "뉴스 없음"은 중립이 아니다

관련 기사가 하나도 없으면 축은 0 이 아니라 ABSTAINED 다. 근거가 없는 것과
근거를 보고 중립이라 판단한 것은 다른 상태이고, 0 으로 채우면 blend_axes 의
유효 가중치 계산이 거짓말을 하게 된다.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from instrument_scoring import STATUS_OK, AxisScore, abstain

# 영향도 -> 가중치. LLM 이 세 등급 중 하나를 고른다.
IMPACT_WEIGHT = {"high": 1.0, "medium": 0.6, "low": 0.3}
# 기여 합을 tanh 로 접을 때의 척도. 2.0 이면 최신 high 1건이 약 0.46,
# high 3건이 약 0.83 이 된다 - 근거가 쌓일수록 커지되 포화는 늦다.
EVIDENCE_SCALE = 2.0
# 같은 사건을 다룬 기사가 여러 건 들어오면 한 사건이 여러 표를 행사한다
# (실측: 다이나믹솔루션의 엔비디아 협력 기사가 10건 중 4건). 제목 토큰이
# 이 비율 이상 겹치면 같은 사건으로 보고 첫 건만 남긴다.
DUP_TOKEN_OVERLAP = 0.6
# 회사명을 뺀 뒤 남은 토큰이 이보다 적으면 접기 판정을 하지 않는다.
MIN_DEDUP_TOKENS = 3
POLARITY_SIGN = {"호재": 1.0, "악재": -1.0, "중립": 0.0}

# LLM 이 이 스키마 밖으로 못 나가게 vLLM 의 제약 디코딩에 그대로 넘긴다.
JUDGMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["ref", "relevant", "polarity", "impact", "why"],
                "properties": {
                    "ref": {"type": "string"},
                    "relevant": {"type": "boolean"},
                    "polarity": {"type": "string", "enum": ["호재", "악재", "중립"]},
                    "impact": {"type": "string", "enum": ["high", "medium", "low"]},
                    # why 는 짧을수록 빠르다. 출력 토큰이 생성 시간을 결정한다
                    # (실측 2026-08-25: 18건 판정 46초).
                    "why": {"type": "string", "maxLength": 60},
                },
            },
        }
    },
}

_SYSTEM = (
    "너는 한국 주식 트레이딩 데스크의 리서치 보조다. 주어진 제목만 보고 "
    "각 항목이 해당 종목의 **주가에** 호재인지 악재인지 판정한다.\n"
    "규칙:\n"
    "- 종목의 사업·실적·수급·규제와 무관하면 relevant=false (인사·부고·협찬·"
    "동명이인·단순 시황 나열 등).\n"
    "- 제목만으로 방향을 알 수 없으면 polarity=중립.\n"
    "- impact 는 주가 영향의 크기다. 계약·수주·증설·유상증자·감자·소송 결과는 "
    "high, 정기공시·소액 지분변동은 low.\n"
    "- why 는 한 문장, 60자 이내. 제목에 없는 사실을 지어내지 마라.\n"
    "- 주어진 ref 를 그대로 쓰고, 항목을 빠뜨리거나 추가하지 마라."
)


def _norm(text: str) -> str:
    return re.sub(r"[\s​]+", "", str(text or "")).lower()


def prefilter(items: Sequence[Mapping[str, Any]], company: str,
              *, text_keys: Sequence[str]) -> list[dict[str, Any]]:
    """회사명이 실제로 등장하는 항목만 남긴다. LLM 호출 전 결정론 단계.

    회사명은 공백·특수문자를 지우고 비교한다("에스케이하이닉스" 같은 표기
    차이까지는 못 잡지만, 연예 기사류는 여기서 대부분 걸린다).
    """
    needle = _norm(company)
    if not needle:
        return [dict(it) for it in items]
    kept = []
    for it in items:
        blob = _norm(" ".join(str(it.get(k) or "") for k in text_keys))
        if needle and needle in blob:
            kept.append(dict(it))
    return kept


def dedupe_by_headline(items: Sequence[Mapping[str, Any]], title_key: str,
                       company: str = "") -> tuple[list[dict[str, Any]], list[str]]:
    """제목 토큰이 크게 겹치는 기사를 한 건으로 접는다. 접힌 ref 는 돌려준다.

    같은 사건의 재탕 기사가 각각 한 표씩 행사하면, 보도량이 많은 사건이
    자동으로 강한 신호가 된다. 그건 사건의 크기가 아니라 언론 노출량이다.

    ▶ 회사명 토큰은 비교에서 뺀다. prefilter 를 지난 기사는 **전부** 회사명을
      갖고 있어서, 그대로 세면 모든 쌍의 겹침이 부풀고 서로 다른 사건까지
      접힌다("한섬 수주 발표" vs "한섬 증설 발표" 가 2/3=0.67 로 접혔다).
    """
    company_tokens = {t for t in re.split(r"[^0-9A-Za-z가-힣]+", _norm(company))
                      if len(t) > 1}

    def _tokens(raw: Any) -> set[str]:
        toks = {t.lower() for t in re.split(r"[^0-9A-Za-z가-힣]+", _strip_tags(raw))
                if len(t) > 1}
        # 회사명이 붙어 나오는 표기("다이나믹솔루션,")까지 잡으려면 부분일치로 뺀다.
        return {t for t in toks
                if not any(ct and (ct in t or t in ct) for ct in company_tokens)}

    kept: list[dict[str, Any]] = []
    kept_tokens: list[set[str]] = []
    folded: list[str] = []
    for it in items:
        tokens = _tokens(it.get(title_key))
        # 남은 토큰이 너무 적으면 겹침 비율이 불안정하다(2개 중 1개면 0.5).
        if len(tokens) < MIN_DEDUP_TOKENS:
            kept.append(dict(it)); kept_tokens.append(set()); continue
        dup = False
        for prev in kept_tokens:
            if not prev:
                continue
            overlap = len(tokens & prev) / min(len(tokens), len(prev))
            if overlap >= DUP_TOKEN_OVERLAP:
                dup = True
                break
        if dup:
            folded.append(str(it.get("ref")))
        else:
            kept.append(dict(it)); kept_tokens.append(tokens)
    return kept, folded


def _strip_tags(value: Any) -> str:
    # 네이버 응답 제목에는 <b> 하이라이트 태그가 섞여 있다.
    return re.sub(r"<[^>]+>", "", str(value or "")).strip()


def judge(
    axis_name: str,
    items: Sequence[Mapping[str, Any]],
    company: str,
    llm,
    *,
    title_key: str,
    date_key: str,
    max_items: int = 10,
) -> AxisScore:
    """LLM 에 관련성·방향을 물어 축 점수를 만든다. 실패는 기권이다."""
    if not items:
        return abstain(axis_name, f"{company} 관련 항목 0건")

    deduped, folded = dedupe_by_headline(items, title_key, company)
    trimmed = deduped[:max_items]
    listing = "\n".join(
        f"{it.get('ref')}. [{it.get(date_key) or '날짜미상'}] {_strip_tags(it.get(title_key))}"
        for it in trimmed
    )
    prompt = (
        f"종목: {company}\n"
        f"아래 {len(trimmed)}건을 판정하라.\n\n{listing}"
    )
    try:
        raw = llm(_SYSTEM, prompt, json_schema=JUDGMENT_SCHEMA)
        parsed = json.loads(raw)
        judgments = {str(j["ref"]): j for j in parsed.get("items", [])}
    except Exception as exc:  # noqa: BLE001 - 모델 실패는 기권이지 중립이 아니다
        return abstain(axis_name, f"LLM 판정 실패 {type(exc).__name__}: {str(exc)[:80]}")

    scored, dropped = [], 0
    for rank, it in enumerate(trimmed):
        j = judgments.get(str(it.get("ref")))
        if j is None:
            dropped += 1
            continue
        if not j.get("relevant"):
            dropped += 1
            continue
        sign = POLARITY_SIGN.get(str(j.get("polarity")), 0.0)
        weight = IMPACT_WEIGHT.get(str(j.get("impact")), 0.3)
        # 최신 항목에 더 큰 무게. 목록은 최신순으로 들어온다.
        recency = 1.0 / (1.0 + rank * 0.25)
        scored.append({
            "ref": it.get("ref"),
            "title": _strip_tags(it.get(title_key)),
            "published_at": it.get(date_key),
            "url": it.get("url"),
            "polarity": j.get("polarity"),
            "impact": j.get("impact"),
            "why": j.get("why"),
            "contribution": sign * weight * recency,
            "evidence_id": it.get("evidence_id"),
            "citation": it.get("citation"),
        })

    if not scored:
        return abstain(
            axis_name,
            f"{company} 관련 항목 {len(trimmed)}건 모두 무관 판정(주가 무관)")

    # 방향만 재고 크기를 버리면 안 된다. `합 / 절대값합` 으로 정규화하면 한쪽으로
    # 쏠린 순간 개수·영향도와 무관하게 정확히 ±1 로 포화한다(실측 2026-08-24:
    # 후보 3종목의 news 축이 전부 +1.000 이었다 - 약한 호재 1건과 강한 호재
    # 10건이 같은 점수였다는 뜻이다). 합을 그대로 tanh 로 접어 **근거가 많고
    # 강할수록 커지게** 한다.
    raw = sum(s["contribution"] for s in scored)
    value = math.tanh(raw / EVIDENCE_SCALE)

    positives = [s for s in scored if s["polarity"] == "호재"]
    negatives = [s for s in scored if s["polarity"] == "악재"]
    return AxisScore(
        axis=axis_name,
        status=STATUS_OK,
        value=value,
        detail={
            "judged": len(trimmed),
            "relevant": len(scored),
            "dropped_irrelevant": dropped,
            "folded_duplicates": folded,
            "호재": len(positives),
            "악재": len(negatives),
            "중립": len(scored) - len(positives) - len(negatives),
            "items": scored,
        },
        evidence_refs=tuple(
            str(s["evidence_id"] or s["citation"] or s["ref"]) for s in scored),
        reason="",
    )


def news_axis(items, company, llm, **kw) -> AxisScore:
    return judge("news", items, company, llm,
                 title_key="title", date_key="published_at", **kw)


def disclosure_axis(items, company, llm, **kw) -> AxisScore:
    return judge("disclosure", items, company, llm,
                 title_key="title", date_key="published_at", max_items=8, **kw)


def judge_combined(news_items: Sequence[Mapping[str, Any]],
                   disclosure_items: Sequence[Mapping[str, Any]],
                   company: str, llm, *,
                   news_max: int = 8, disclosure_max: int = 6
                   ) -> tuple[AxisScore, AxisScore]:
    """뉴스·공시를 **한 번에** 물어 두 축으로 가른다.

    호출이 절반이 되고(실측 3단 72초 -> 40초대), 모델이 둘을 같이 보고
    판단한다. 결과는 ref 접두사로 나눈다 - 뉴스는 `n*`, 공시는 `d*` 라
    `fetch_news`/`fetch_disclosures` 가 이미 그렇게 붙여 준다.

    한쪽이 비면 다른 쪽만 판정하고, 빈 쪽은 기권이다(0 이 아니다).
    """
    news_kept, news_folded = dedupe_by_headline(news_items, "title", company)
    disc_kept, disc_folded = dedupe_by_headline(disclosure_items, "title", company)
    news_kept = news_kept[:news_max]
    disc_kept = disc_kept[:disclosure_max]

    if not news_kept and not disc_kept:
        return (abstain("news", f"{company} 관련 뉴스 0건"),
                abstain("disclosure", f"{company} 관련 공시 0건"))

    lines = []
    if news_kept:
        lines.append("[뉴스]")
        lines += [f"{it.get('ref')}. [{it.get('published_at') or '날짜미상'}] "
                  f"{_strip_tags(it.get('title'))}" for it in news_kept]
    if disc_kept:
        lines.append("[공시]")
        lines += [f"{it.get('ref')}. [{it.get('published_at') or '날짜미상'}] "
                  f"{_strip_tags(it.get('title'))}" for it in disc_kept]
    prompt = (f"종목: {company}\n"
              f"아래 {len(news_kept) + len(disc_kept)}건을 판정하라. "
              f"뉴스와 공시가 같은 사건을 가리키면 각각 판정하되 "
              f"impact 를 중복해서 크게 잡지 마라.\n\n" + "\n".join(lines))

    try:
        raw = llm(_SYSTEM, prompt, json_schema=JUDGMENT_SCHEMA)
        judgments = {str(j["ref"]): j for j in json.loads(raw).get("items", [])}
    except Exception as exc:  # noqa: BLE001 - 모델 실패는 기권이지 중립이 아니다
        reason = f"LLM 판정 실패 {type(exc).__name__}: {str(exc)[:80]}"
        return abstain("news", reason), abstain("disclosure", reason)

    def build(axis_name: str, kept: list, folded: list) -> AxisScore:
        if not kept:
            return abstain(axis_name, f"{company} 관련 항목 0건")
        scored, dropped = [], 0
        for rank, it in enumerate(kept):
            j = judgments.get(str(it.get("ref")))
            if j is None or not j.get("relevant"):
                dropped += 1
                continue
            sign = POLARITY_SIGN.get(str(j.get("polarity")), 0.0)
            weight = IMPACT_WEIGHT.get(str(j.get("impact")), 0.3)
            recency = 1.0 / (1.0 + rank * 0.25)
            scored.append({
                "ref": it.get("ref"), "title": _strip_tags(it.get("title")),
                "published_at": it.get("published_at"), "url": it.get("url"),
                "polarity": j.get("polarity"), "impact": j.get("impact"),
                "why": j.get("why"), "contribution": sign * weight * recency,
                "evidence_id": it.get("evidence_id"), "citation": it.get("citation"),
            })
        if not scored:
            return abstain(axis_name,
                           f"{company} 관련 항목 {len(kept)}건 모두 무관 판정(주가 무관)")
        value = math.tanh(sum(s["contribution"] for s in scored) / EVIDENCE_SCALE)
        pos = [s for s in scored if s["polarity"] == "호재"]
        neg = [s for s in scored if s["polarity"] == "악재"]
        return AxisScore(
            axis=axis_name, status=STATUS_OK, value=value,
            detail={"judged": len(kept), "relevant": len(scored),
                    "dropped_irrelevant": dropped, "folded_duplicates": folded,
                    "호재": len(pos), "악재": len(neg),
                    "중립": len(scored) - len(pos) - len(neg), "items": scored},
            evidence_refs=tuple(str(s["evidence_id"] or s["citation"] or s["ref"])
                                for s in scored),
        )

    return (build("news", news_kept, news_folded),
            build("disclosure", disc_kept, disc_folded))


# ────────────────────────────────────────────────────────────────────────────
# 자체 점검 - 네트워크 없음, 가짜 LLM
# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from instrument_scoring import STATUS_ABSTAINED

    news = [
        {"ref": "n1", "title": "<b>한섬</b>, 3분기 영업이익 컨센서스 상회",
         "published_at": "Mon, 24 Aug 2026 09:00:00 +0900", "evidence_id": "e1"},
        {"ref": "n2", "title": "이현이, 한 컷 찍었을 뿐인데 역시 톱모델",
         "published_at": "Mon, 24 Aug 2026 08:00:00 +0900", "evidence_id": "e2"},
        {"ref": "n3", "title": "한섬 유상증자 결정… 주주가치 희석 우려",
         "published_at": "Sun, 23 Aug 2026 18:00:00 +0900", "evidence_id": "e3"},
    ]

    # 사전 필터 - 회사명이 없는 기사는 LLM 에 가지도 않는다
    kept = prefilter(news, "한섬", text_keys=("title",))
    assert [k["ref"] for k in kept] == ["n1", "n3"], kept

    def fake_llm(system, prompt, *, json_schema=None):
        assert json_schema is JUDGMENT_SCHEMA
        assert "한섬" in prompt
        # 태그가 벗겨진 채로 들어가야 한다
        assert "<b>" not in prompt, prompt
        return json.dumps({"items": [
            {"ref": "n1", "relevant": True, "polarity": "호재",
             "impact": "high", "why": "실적 컨센서스 상회"},
            {"ref": "n3", "relevant": True, "polarity": "악재",
             "impact": "high", "why": "유상증자로 지분 희석"},
        ]})

    ax = news_axis(kept, "한섬", fake_llm)
    assert ax.status == STATUS_OK, ax
    # n1(호재, rank0, recency 1.0) vs n3(악재, rank1, recency 0.8) -> 호재 우세
    assert ax.value > 0, ax.value
    assert ax.detail["호재"] == 1 and ax.detail["악재"] == 1
    assert ax.evidence_refs == ("e1", "e3"), ax.evidence_refs

    # 전부 무관하면 0 이 아니라 기권
    def all_irrelevant(system, prompt, *, json_schema=None):
        return json.dumps({"items": [
            {"ref": "n1", "relevant": False, "polarity": "중립",
             "impact": "low", "why": "주가 무관"},
            {"ref": "n3", "relevant": False, "polarity": "중립",
             "impact": "low", "why": "주가 무관"},
        ]})

    ax2 = news_axis(kept, "한섬", all_irrelevant)
    assert ax2.status == STATUS_ABSTAINED and ax2.value is None, ax2
    assert "모두 무관" in ax2.reason

    # 항목이 없으면 기권
    assert news_axis([], "한섬", fake_llm).status == STATUS_ABSTAINED

    # LLM 이 깨져도 중립으로 위장하지 않는다
    def broken(system, prompt, *, json_schema=None):
        raise TimeoutError("vLLM timeout")

    ax3 = news_axis(kept, "한섬", broken)
    assert ax3.status == STATUS_ABSTAINED and ax3.value is None
    assert "LLM 판정 실패" in ax3.reason

    # 스키마 밖 응답도 기권
    ax4 = news_axis(kept, "한섬", lambda s, p, *, json_schema=None: "not json")
    assert ax4.status == STATUS_ABSTAINED, ax4

    # 크기가 반영된다 - 한쪽 쏠림이라고 무조건 ±1 이 되면 안 된다
    def one_good(system, prompt, *, json_schema=None):
        return json.dumps({"items": [
            {"ref": "n1", "relevant": True, "polarity": "호재",
             "impact": "high", "why": "x"}]})

    def three_good(system, prompt, *, json_schema=None):
        return json.dumps({"items": [
            {"ref": f"g{i}", "relevant": True, "polarity": "호재",
             "impact": "high", "why": "x"} for i in range(1, 4)]})

    one = news_axis([{"ref": "n1", "title": "한섬 수주", "published_at": "d"}],
                    "한섬", one_good)
    three = news_axis(
        [{"ref": f"g{i}", "title": f"한섬 {w} 발표", "published_at": "d"}
         for i, w in enumerate(["수주", "증설", "특허"], 1)],
        "한섬", three_good)
    assert one.value < three.value < 1.0, (one.value, three.value)
    assert 0.3 < one.value < 0.6, one.value

    # 같은 사건 재탕은 접힌다 - 보도량이 신호 크기가 되면 안 된다
    dupes = [
        {"ref": "n1", "title": "다이나믹솔루션, 엔비디아와 BCI 기술협력", "published_at": "d"},
        {"ref": "n2", "title": "다이나믹솔루션 엔비디아와 BCI 기술협력 논의", "published_at": "d"},
        {"ref": "n3", "title": "다이나믹솔루션 3분기 흑자전환", "published_at": "d"},
    ]
    kept_d, folded = dedupe_by_headline(dupes, "title", "다이나믹솔루션")
    assert [k["ref"] for k in kept_d] == ["n1", "n3"], kept_d
    assert folded == ["n2"], folded

    # 합친 판정이 두 축으로 정확히 갈린다
    def combo_llm(system, prompt, *, json_schema=None):
        assert "[뉴스]" in prompt and "[공시]" in prompt, prompt
        return json.dumps({"items": [
            {"ref": "n1", "relevant": True, "polarity": "호재",
             "impact": "high", "why": "수주"},
            {"ref": "d1", "relevant": True, "polarity": "악재",
             "impact": "high", "why": "증자"},
        ]})

    nax, dax = judge_combined(
        [{"ref": "n1", "title": "한섬 대형 수주 공시", "published_at": "d"}],
        [{"ref": "d1", "title": "한섬 유상증자 결정", "published_at": "d"}],
        "한섬", combo_llm)
    assert nax.axis == "news" and nax.value > 0, nax
    assert dax.axis == "disclosure" and dax.value < 0, dax
    assert nax.detail["호재"] == 1 and dax.detail["악재"] == 1

    # 한쪽이 비면 그쪽만 기권이고 다른 쪽은 정상 판정된다
    nax2, dax2 = judge_combined(
        [{"ref": "n1", "title": "한섬 수주", "published_at": "d"}], [],
        "한섬", lambda s, p, *, json_schema=None: json.dumps({"items": [
            {"ref": "n1", "relevant": True, "polarity": "호재",
             "impact": "high", "why": "x"}]}))
    assert nax2.status == STATUS_OK and dax2.status == STATUS_ABSTAINED

    # 둘 다 비면 LLM 을 부르지 않는다
    called = []
    n3, d3 = judge_combined([], [], "한섬",
                            lambda *a, **k: called.append(1) or "{}")
    assert not called and n3.status == STATUS_ABSTAINED and d3.status == STATUS_ABSTAINED

    print("narrative_axes self-check OK")
