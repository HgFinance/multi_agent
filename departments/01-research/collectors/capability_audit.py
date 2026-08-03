#!/usr/bin/env python3
"""역량 격차 감사 - "우리가 쓸 수 있는데 안 쓰는 것"을 스스로 찾는다.

소유: 재일 (리서치본부)
근거: 재일님 지시 2026-08-02 "내가 주는 이런 정보들을 내가 안 줘도 에이전트가
      알아서 수집해서 output 이 좋아지게".

▶ 이 감사가 생긴 이유 (실화)
  VKOSPI 는 재일님이 알려줘서 들어왔다. 그런데 찾는 과정은 기계적이었다 -
  LS t8424 로 업종 252개를 열거하니 205번에 VKOSPI 가 있었다. 즉 **우리는
  이미 접근 가능한 목록을 한 번도 전수 조회하지 않았을 뿐**이다.
  사람의 착상이 필요했던 게 아니라 재고 조사를 안 했던 것이다.
  이 파일은 그 재고 조사를 주기적으로 자동화한다.

▶ 자동화할 수 있는 것과 없는 것을 섞지 않는다
  자동: (1) 접근 가능한데 미사용인 재고 열거 (2) 방법론이 요구하는 입력 중
        우리에게 없는 것 (3) 분석가가 판단 불가로 끝난 빈도 (4) 채점하지
        못한 주장.  - 전부 **우리 시스템에 대한 사실**이라 LLM 이 필요 없다.
  수동: 실제 도입(코드 작성·검증)은 사람이 한다. 감사는 '무엇을 놓치고
        있는가'를 증거와 함께 들이밀 뿐이고, 자동으로 채택하지 않는다.
        발견을 자동화하는 것과 채택을 자동화하는 것은 다른 문제다.

사용
  python collectors/capability_audit.py           # 자체 점검
  python collectors/capability_audit.py --audit   # 감사 1회 (LS·DB 필요)
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evidence"))

AUDIT_VERSION = "research-capability-audit-v1"
KST = timezone(timedelta(hours=9))

# 우리가 이미 수집 중인 LS 업종코드 (미사용 목록에서 뺀다)
USED_UPCODES = {"001", "301", "205"}      # KOSPI 종합, KOSDAQ 종합, VKOSPI

# 지표로서 값이 있을 법한 이름 신호. 여기 걸리는 미사용 항목을 위로 올린다.
# (전 업종을 다 수집하자는 게 아니다 - 국면 판단에 쓰이는 축을 찾는 것)
INTEREST_PATTERNS = (
    ("변동성", "변동성 국면"), ("VKOSPI", "변동성 국면"), ("공포", "심리"),
    ("국채", "금리·안전자산"), ("선물", "파생 포지셔닝"),
    ("레버리지", "위험선호"), ("인버스", "위험선호"),
    ("배당", "스타일"), ("가치", "스타일"), ("성장", "스타일"),
    ("모멘텀", "스타일"), ("퀄리티", "스타일"), ("소형", "규모"),
    ("대형", "규모"), ("중형", "규모"),
)


@dataclass
class Gap:
    kind: str            # INVENTORY_UNUSED / METHOD_INPUT_MISSING / ...
    title: str
    detail: str
    evidence: str        # 수치 근거 - 없으면 등재하지 않는다
    priority: int = 3    # 1(높음) ~ 5

    def line(self) -> str:
        return (f"[P{self.priority}] {self.kind} — {self.title}\n"
                f"      {self.detail}\n      근거: {self.evidence}")


def classify_interest(name: str) -> str | None:
    for pat, why in INTEREST_PATTERNS:
        if pat in name:
            return why
    return None


def unused_index_gaps(all_codes: list[tuple[str, str]],
                      used: set[str] | None = None) -> list[Gap]:
    """t8424 전체 업종 - 우리가 쓰는 것 = 미사용 재고. 관심 신호가 있는 것만."""
    used = USED_UPCODES if used is None else used
    out: list[Gap] = []
    for code, name in all_codes:
        if code in used or not name:
            continue
        why = classify_interest(name)
        if why is None:
            continue
        out.append(Gap(
            kind="INVENTORY_UNUSED",
            title=f"LS 업종 {code} '{name}' 미사용",
            detail=f"{why} 축으로 쓸 수 있는 지수인데 수집하지 않는다. "
                   f"t1511 upcode={code} 한 번이면 일별 계열이 된다.",
            evidence=f"t8424 전체 {len(all_codes)}개 중 우리가 쓰는 것은 "
                     f"{len(used)}개",
            priority=2 if "변동성" in name or "국채" in name else 3))
    return out


def method_input_gaps(methods, available_accounts: set[str]) -> list[Gap]:
    """방법론이 요구하는 재무 계정 중 우리에게 없는 것.

    BLOCKED 방법이 왜 막혔는지를 사람이 기억하지 않아도 되게 한다.
    """
    out: list[Gap] = []
    for m in methods:
        if m.status == "ADOPTED" and not m.partial_reason:
            continue
        missing = [i for i in m.inputs
                   if i.startswith(("BS:", "IS:")) and i not in available_accounts]
        # 계정 접두사 없이 적힌 입력은 이름만으로 대조한다
        loose = [i for i in m.inputs
                 if not i.startswith(("BS:", "IS:"))
                 and not any(i in a for a in available_accounts)]
        if not missing and not loose:
            continue
        out.append(Gap(
            kind="METHOD_INPUT_MISSING",
            title=f"{m.name} - 입력 부족",
            detail=f"필요하지만 없는 입력: {', '.join(missing + loose)[:160]}",
            evidence=f"status={m.status}, 인용={m.citation[:60]}",
            priority=2 if m.status == "BLOCKED" else 4))
    return out


def verdict_gaps(counts: dict[str, dict[str, int]], *, min_runs: int = 5,
                 threshold: float = 0.3) -> list[Gap]:
    """분석가별 '판단 불가' 비율. 자주 못 하면 재료가 없다는 뜻이다."""
    out: list[Gap] = []
    for node, by_verdict in sorted(counts.items()):
        total = sum(by_verdict.values())
        if total < min_runs:
            continue
        bad = sum(v for k, v in by_verdict.items()
                  if k in ("INSUFFICIENT_DATA", "HALTED", None))
        ratio = bad / total
        if ratio >= threshold:
            out.append(Gap(
                kind="ANALYST_BLIND",
                title=f"{node} 분석가가 {ratio:.0%} 확률로 판단 불가",
                detail="재료가 모자라 결론을 못 낸다 - 입력 데이터를 먼저 "
                       "채워야 기법 추가가 의미를 갖는다.",
                evidence=f"최근 {total}회 중 {bad}회 INSUFFICIENT_DATA",
                priority=1))
    return out


def unscored_claim_gaps(rows: list[tuple[str, int]]) -> list[Gap]:
    """채점하지 못한 주장 - 선순환이 끊긴 지점."""
    out: list[Gap] = []
    for note, n in rows:
        if not note or n <= 0:
            continue
        out.append(Gap(
            kind="FEEDBACK_BROKEN",
            title="발행했지만 채점하지 못한 주장이 있다",
            detail=str(note)[:180],
            evidence=f"{n}건",
            priority=2))
    return out


def collector_health_gaps(rows: list[tuple]) -> list[Gap]:
    """24시간 안에 실패한 수집 Job. **조용한 실패가 가장 큰 위험이다.**

    2026-08-02: geopolitical·bluesky-watch 두 Job 이 컨테이너에서 계속 실패
    하고 있었는데 로그에만 남아 아무도 몰랐다. 그 부류를 여기서 매일 들이민다.
    rows: (job_name, runs, ok, skip, bad, last_ok_at, last_status, err_tail)
    """
    out: list[Gap] = []
    for job, runs, ok, skip, bad, last_ok, last_status, err in rows or []:
        if not bad:
            continue
        # SKIP 은 실패가 아니다(휴장 등) - 성공률에서 이미 분리돼 있다
        detail = f"최근 24시간 {runs}회 중 실패 {bad}회 (성공 {ok} / SKIP {skip})"
        if last_ok is None:
            detail += " — **24시간 내 성공 기록이 아예 없다**"
        out.append(Gap(
            kind="COLLECTOR_FAILING",
            title=f"수집 Job '{job}' 실패 중 (마지막 상태 {last_status})",
            detail=detail + (f"\n      마지막 오류: {str(err)[:160]}" if err else ""),
            evidence="research.collector_health 조회",
            priority=1))
    return out


def render_report(gaps: list[Gap], *, now: str) -> str:
    lines = ["# 리서치본부 — 역량 격차 감사 (결정론 생성)", "",
             f"- 생성: {now}", f"- 발견: {len(gaps)}건", "",
             "> 발견은 자동, 채택은 수동이다. 아래 항목은 '무엇을 놓치고 있는가'의",
             "> 증거일 뿐 자동으로 도입되지 않는다.", ""]
    by_kind: dict[str, list[Gap]] = {}
    for g in sorted(gaps, key=lambda x: (x.priority, x.kind, x.title)):
        by_kind.setdefault(g.kind, []).append(g)
    for kind, items in by_kind.items():
        lines += [f"## {kind} ({len(items)}건)", ""]
        for g in items:
            lines += [f"- **[P{g.priority}] {g.title}**",
                      f"  - {g.detail}", f"  - 근거: {g.evidence}"]
        lines += [""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------

def audit() -> int:
    import psycopg2
    from methods import METHODS
    from source_registry import load_project_env

    gaps: list[Gap] = []
    env = load_project_env()

    # 1) LS 업종 재고 - VKOSPI 를 찾아낸 바로 그 경로
    try:
        from ls_client import LsRestClient
        from market_breadth_collector import fetch_industry_codes

        codes = fetch_industry_codes(LsRestClient(), "0")
        gaps += unused_index_gaps(codes)
        print(f"  LS 업종 {len(codes)}개 열거 - 미사용 관심 {len([g for g in gaps])}건",
              flush=True)
    except Exception as e:  # noqa: BLE001 - 한 축 실패가 감사 전체를 죽이지 않는다
        print(f"  ⚠ LS 업종 열거 실패: {type(e).__name__} {str(e)[:90]}", flush=True)

    conn = psycopg2.connect(env["DATABASE_URL"], connect_timeout=20)
    try:
        with conn.cursor() as cur:
            cur.execute("select distinct account_code from research.financial_facts")
            accounts = {r[0] for r in cur.fetchall()}
            gaps += method_input_gaps(METHODS, accounts)

            cur.execute("""
                select coalesce(v.node, '?'), coalesce(v.verdict, 'NULL'), count(*)
                from research.pipeline_runs r
                left join lateral jsonb_each_text(
                    coalesce(r.metadata->'analyst_verdicts', '{}'::jsonb))
                    as v(node, verdict) on true
                where r.started_at > now() - interval '30 days'
                group by 1, 2
            """)
            counts: dict[str, dict[str, int]] = {}
            for node, verdict, n in cur.fetchall():
                counts.setdefault(node, {})[verdict] = n
            gaps += verdict_gaps(counts)

            cur.execute("""
                select note, count(*) from research.packet_outcomes
                where note is not null and note like '%채점 제외%'
                group by 1
            """)
            gaps += unscored_claim_gaps(cur.fetchall())

            # 24시간 운영의 핵심 - 지금 뭐가 고장나 있는가
            try:
                cur.execute("""
                    select job_name, runs_24h, ok_24h, skip_24h, bad_24h,
                           last_ok_at, last_status, last_error_tail
                    from research.collector_health order by bad_24h desc
                """)
                gaps += collector_health_gaps(cur.fetchall())
            except Exception as e:  # noqa: BLE001 - 뷰 미적용 환경도 있다
                conn.rollback()
                print(f"  ⚠ collector_health 조회 실패(마이그레이션 001500 확인): "
                      f"{type(e).__name__}", flush=True)
    finally:
        conn.close()

    now = datetime.now(KST).isoformat(timespec="seconds")
    report = render_report(gaps, now=now)
    out_dir = Path(__file__).resolve().parents[1] / "reports"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"capability_audit_{datetime.now(KST):%Y%m%d}.md"
    path.write_text(report, encoding="utf-8")

    print(f"\n{AUDIT_VERSION}: 격차 {len(gaps)}건")
    for g in sorted(gaps, key=lambda x: x.priority)[:12]:
        print(g.line())
    print(f"\n리포트: {path.name}")
    return 0


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·DB 없음
# ---------------------------------------------------------------------------

def _check_finds_vkospi_case():
    """실화 재현 - 전수 목록만 있으면 VKOSPI 를 스스로 집어낸다."""
    codes = [("001", "종       합"), ("205", "VKOSPI"), ("526", "KRX 최소변동성지수"),
             ("606", "국채선물지수"), ("013", "음식료품"), ("014", "섬유의복")]
    # 아직 VKOSPI 를 안 쓰던 시점을 가정
    gaps = unused_index_gaps(codes, used={"001", "301"})
    titles = " ".join(g.title for g in gaps)
    assert "VKOSPI" in titles, gaps
    assert "국채선물지수" in titles
    assert "음식료품" not in titles, "관심 신호 없는 업종까지 올리면 소음이다"
    assert all(g.evidence for g in gaps), "근거 없는 격차는 등재하지 않는다"
    # 이미 쓰는 것은 다시 올리지 않는다
    after = unused_index_gaps(codes, used={"001", "301", "205"})
    assert "VKOSPI" not in " ".join(g.title for g in after)
    print("  미사용 재고 탐지(VKOSPI) OK")


def _check_method_inputs():
    from methods import METHODS

    have = {"BS:자산총계", "IS:당기순이익(손실)"}
    gaps = method_input_gaps(METHODS, have)
    keys = " ".join(g.title for g in gaps)
    assert "Beneish" in keys, "BLOCKED 방법의 입력 부족이 보고되지 않았다"
    assert any(g.priority == 2 for g in gaps), "BLOCKED 는 우선순위가 높아야 한다"
    print("  방법론 입력 부족 탐지    OK")


def _check_verdict_ratio():
    counts = {"fundamental": {"NOTED": 9, "INSUFFICIENT_DATA": 1},
              "geopolitical": {"SHOCK": 2, "INSUFFICIENT_DATA": 8},
              "thin": {"INSUFFICIENT_DATA": 3}}
    gaps = verdict_gaps(counts)
    titles = " ".join(g.title for g in gaps)
    assert "geopolitical" in titles, "판단 불가 80% 를 놓쳤다"
    assert "fundamental" not in titles, "10% 는 격차가 아니다"
    assert "thin" not in titles, "표본 3회로 결론내면 잡음이다"
    print("  판단불가 비율 탐지       OK")


def _check_collector_health():
    """실패는 올리고 SKIP 은 올리지 않는다 - 휴장마다 경보가 울리면 아무도 안 본다."""
    rows = [
        ("geopolitical", 3, 0, 0, 3, None, "FAILED", "FileNotFoundError: themes"),
        ("vkospi", 2, 0, 2, 0, None, "SKIP", None),          # 휴장 - 정상
        ("disclosure", 60, 59, 0, 1, "2026-08-02T09:00", "OK", "timeout"),
    ]
    gaps = collector_health_gaps(rows)
    titles = " ".join(g.title for g in gaps)
    assert "geopolitical" in titles and "disclosure" in titles, gaps
    assert "vkospi" not in titles, "SKIP 만 있는 Job 을 실패로 올렸다"
    geo = next(g for g in gaps if "geopolitical" in g.title)
    assert "성공 기록이 아예 없다" in geo.detail, geo.detail
    assert all(g.priority == 1 for g in gaps), "고장은 최우선이다"
    assert not collector_health_gaps([]), "빈 입력에 격차를 만들면 안 된다"
    print("  수집 Job 고장 탐지       OK")


def _check_report_render():
    g = [Gap("INVENTORY_UNUSED", "t", "d", "e", 1)]
    md = render_report(g, now="2026-08-02T00:00:00+09:00")
    assert "역량 격차 감사" in md and "[P1] t" in md
    assert "발견은 자동, 채택은 수동" in md, "자동 채택 오해를 막는 문장이 빠졌다"
    assert render_report([], now="x").count("발견: 0건") == 1
    print("  리포트 렌더러            OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--audit" in sys.argv:
        raise SystemExit(audit())

    print(f"{AUDIT_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_finds_vkospi_case()
    _check_method_inputs()
    _check_verdict_ratio()
    _check_collector_health()
    _check_report_render()
    print("역량 격차 감사 5개 영역 통과. 감사는 --audit")
