"""law.go.kr Open API에서 compliance-policy-worker 도메인 원문을 가져와 data/raw/에 저장한다.

코퍼스 범위는 RISK_COMPLIANCE_RAG_DATA_SPEC.md의 Mandate/Restricted List/Order-Approval
도메인에 한정한다 (전체 법령을 적재하지 않는다 — YAGNI). 상위법령(자본시장법 9개 조),
금융감독기관 행정규칙(금융투자업규정 정보교류차단 조항), 사법부 판례(부정거래 관련
대법원 판결) 세 카테고리를 실제 라이브 API로 가져온다. "지능형 법령검색"·"조문-법령용어
연계"·금융위원회/증권선물위원회 전용 법령해석례 필터는 target 코드를 확정하지 못해
이번 fetch 범위에서 제외한다 (계획 문서의 non-goal 참고).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # departments/03-risk

from integrations.law_api import LawApiClient  # noqa: E402

RAW_DIR = Path(__file__).resolve().parent / "data" / "raw"

# 자본시장법(MST=283193) 중 compliance-policy-worker 도메인에 직접 관련된 조문만 고른다.
CAPITAL_MARKETS_ACT_MST = "283193"
CAPITAL_MARKETS_ACT_ARTICLE_NUMBERS = [
    "54",  # 직무관련 정보의 이용 금지
    "63",  # 임직원의 금융투자상품 매매
    "71",  # 불건전 영업행위의 금지
    "172",  # 내부자의 단기매매차익 반환
    "173",  # 임원 등의 특정증권등 소유상황 보고
    "174",  # 미공개중요정보 이용행위 금지
    "176",  # 시세조종행위 등의 금지
    "178",  # 부정거래행위 등의 금지
    "443",  # 벌칙
]

# 금융투자업규정(행정규칙, ID는 search_admrul로 확인된 최신 시행본)
FINANCIAL_INVESTMENT_BUSINESS_REGULATION_ID = "2100000282072"
ADMRUL_EXCERPT_ANCHOR = "제4-6조(금융투자업자의 정보교류의 차단)"
ADMRUL_EXCERPT_CHARS = 1800

# 부정거래행위(제178조) 관련 대법원 판례
PRECEDENT_ID = "601495"  # 2019도12887


@dataclass(frozen=True)
class RawDocument:
    source: str  # "law" | "admrul" | "prec"
    doc_id: str
    page_id: str  # 위키 페이지 slug (Obsidian [[link]] 대상, grep 대상)
    clause_id: str
    title: str
    authority: str
    effective_from: str | None
    text: str
    origin_url: str
    source_sha256: str


def _slug_safe(text: str) -> str:
    """공백·구두점을 지운 위키 slug 조각. 한글/영숫자/밑줄만 남긴다."""

    cleaned = re.sub(r"[^\w가-힣]", "", text)
    return cleaned


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _flatten_article(unit: dict[str, Any]) -> str:
    """법령 조문단위(항/호 nested dict)를 사람이 읽는 순서의 평문으로 편다."""

    lines = [unit.get("조문내용", "").strip()]
    hangs = unit.get("항") or []
    if isinstance(hangs, dict):
        hangs = [hangs]
    for hang in hangs:
        hang_text = hang.get("항내용", "").strip()
        if hang_text:
            lines.append(hang_text)
        hos = hang.get("호") or []
        if isinstance(hos, dict):
            hos = [hos]
        for ho in hos:
            ho_text = ho.get("호내용", "").strip()
            if ho_text:
                lines.append(ho_text)
    return "\n".join(line for line in lines if line)


def fetch_capital_markets_act(client: LawApiClient) -> list[RawDocument]:
    body = client.get_law(mst=CAPITAL_MARKETS_ACT_MST)
    law = body.get("법령", {})
    basic = law.get("기본정보", {})
    title = basic.get("법령명_한글", "자본시장과 금융투자업에 관한 법률") if isinstance(basic, dict) else "자본시장과 금융투자업에 관한 법률"
    units = law.get("조문", {}).get("조문단위", [])
    wanted = set(CAPITAL_MARKETS_ACT_ARTICLE_NUMBERS)
    docs: list[RawDocument] = []
    seen: set[str] = set()
    for unit in units:
        number = unit.get("조문번호")
        if number not in wanted or number in seen:
            continue
        if unit.get("조문여부") != "조문" or not unit.get("조문제목"):
            continue
        text = _flatten_article(unit)
        if not text:
            continue
        seen.add(number)
        article_title = unit.get("조문제목", "")
        docs.append(
            RawDocument(
                source="law",
                doc_id=f"law-{CAPITAL_MARKETS_ACT_MST}-{number}",
                page_id=f"자본시장법_제{number}조_{_slug_safe(article_title)}",
                clause_id=f"제{number}조",
                title=f"{title} {article_title}",
                authority="금융위원회",
                effective_from=_normalize_date(unit.get("조문시행일자")),
                text=text,
                origin_url=(
                    f"https://www.law.go.kr/DRF/lawService.do?target=law"
                    f"&MST={CAPITAL_MARKETS_ACT_MST}&type=HTML"
                ),
                source_sha256=_sha256(text),
            )
        )
    missing = wanted - seen
    if missing:
        raise RuntimeError(f"자본시장법 조문 누락: {sorted(missing)}")
    return docs


def fetch_admrul_excerpt(client: LawApiClient) -> RawDocument:
    body = client.get_admrul(FINANCIAL_INVESTMENT_BUSINESS_REGULATION_ID)
    service = body.get("AdmRulService", {})
    full_text = service.get("조문내용", "")
    idx = full_text.find(ADMRUL_EXCERPT_ANCHOR)
    if idx < 0:
        raise RuntimeError(f"admrul 앵커를 찾지 못함: {ADMRUL_EXCERPT_ANCHOR!r}")
    excerpt = full_text[idx : idx + ADMRUL_EXCERPT_CHARS].strip()
    basic = service.get("행정규칙기본정보", {})
    title = basic.get("행정규칙명", "금융투자업규정") if isinstance(basic, dict) else "금융투자업규정"
    return RawDocument(
        source="admrul",
        doc_id=f"admrul-{FINANCIAL_INVESTMENT_BUSINESS_REGULATION_ID}",
        page_id="금융투자업규정_제4의6조_정보교류의차단",
        clause_id="제4-6조",
        title=f"{title} 제4-6조(금융투자업자의 정보교류의 차단)",
        authority="금융위원회",
        effective_from=_normalize_date(
            basic.get("발령일자") if isinstance(basic, dict) else None
        ),
        text=excerpt,
        origin_url=(
            "https://www.law.go.kr/DRF/lawService.do?target=admrul"
            f"&ID={FINANCIAL_INVESTMENT_BUSINESS_REGULATION_ID}&type=HTML"
        ),
        source_sha256=_sha256(excerpt),
    )


def fetch_precedent(client: LawApiClient) -> RawDocument:
    body = client.get_prec(PRECEDENT_ID)
    prec = body.get("PrecService", body)
    case_name = prec.get("사건명", "")
    case_number = prec.get("사건번호", "")
    summary = prec.get("판시사항") or prec.get("판결요지") or prec.get("전문") or ""
    if isinstance(summary, list):
        summary = "\n".join(str(s) for s in summary)
    text = f"사건명: {case_name}\n사건번호: {case_number}\n\n{summary}".strip()
    return RawDocument(
        source="prec",
        doc_id=f"prec-{PRECEDENT_ID}",
        page_id="대법원_2019도12887_부정거래행위판단기준",
        clause_id=case_number or PRECEDENT_ID,
        title=case_name or f"대법원 판례 {case_number}",
        authority=prec.get("법원명", "대법원"),
        effective_from=_normalize_date(prec.get("선고일자")),
        text=text,
        origin_url=(
            f"https://www.law.go.kr/DRF/lawService.do?target=prec&ID={PRECEDENT_ID}&type=HTML"
        ),
        source_sha256=_sha256(text),
    )


def _normalize_date(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.replace(".", "").replace("-", "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    return None


def fetch_all(client: LawApiClient) -> list[RawDocument]:
    docs = list(fetch_capital_markets_act(client))
    docs.append(fetch_admrul_excerpt(client))
    docs.append(fetch_precedent(client))
    return docs


def save(docs: list[RawDocument], out_dir: Path = RAW_DIR) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for doc in docs:
        path = out_dir / f"{doc.doc_id}.json"
        path.write_text(
            json.dumps(asdict(doc), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        paths.append(path)
    return paths


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[4] / ".env")
    with LawApiClient.from_env() as client:
        docs = fetch_all(client)
    paths = save(docs)
    print(f"저장됨: {len(paths)}개 문서 -> {RAW_DIR}")
    for path in paths:
        print(" -", path.name)


if __name__ == "__main__":
    main()
