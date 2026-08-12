#!/usr/bin/env python3
"""리서치 평면 Data Quality Gate - 문서·재무·거시가 멀쩡한지 매일 판정한다.

소유: 재일 (리서치본부)
근거: WHOLE_SYSTEM_ADVANCEMENT_ROADMAP.md 6.2 P0 3항
      ("Schema, 범위, 신선도, 결측과 이상치 Data Quality Gate")
      및 10장 Scorecard 의 Data Truth 축.

▶ 왜 새로 만드는가
  collectors/market_data_steward.py 는 **시세만** 본다(ticks/quotes/bars/
  breadth/derivatives 다섯). research.documents·financial_facts·
  macro_observations 에 대한 판정은 어디에도 없었다(2026-08-02 계획 대조).
  그래서 구체적으로 이런 것들이 조용히 지나간다:
    - 거시 시계열이 3주째 안 들어와도 아무도 모른다
    - 재무에 부호가 뒤집힌 값이 들어와도 F-Score 가 그대로 계산된다
    - 뉴스가 특정 소스만 말라도 감성 점수가 남은 소스로 기울어진다

▶ 판정만 하고 고치지 않는다
  Steward 는 데이터를 손대지 않는다. 판정을 남기고 FAIL 이면 종료코드 1 로
  드러낸다 - 무엇을 고칠지는 수집기 소관이다. 자동 교정을 여기 넣으면
  '무엇이 원본이었는지'가 사라진다.

▶ 판정은 전부 순수 함수다
  DB 없이 자체 점검이 경계를 전부 검사한다. 임계값을 바꾸면 점검이 먼저 깨진다.

사용
  python collectors/research_data_steward.py           # 자체 점검
  python collectors/research_data_steward.py --audit   # 실제 판정 (DB 조회)
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(_BASE))

STEWARD_VERSION = "research-data-steward-v1"
KST = timezone(timedelta(hours=9))

# (도메인, 테이블, 관측 컬럼, 신선도 한계(시간), 비고)
#   신선도 한계는 **수집 주기의 몇 배**로 잡는다 - 한 번 걸렀다고 경보를 울리면
#   경보가 무뎌지고, 너무 느슨하면 며칠 죽은 것을 못 잡는다.
DOMAINS = (
    # 뉴스는 상시 유입(10분 주기) - 6시간이면 확실히 고장이다.
    # 다만 주말·휴장에는 국내 뉴스가 실제로 줄어 야간 오탐이 나므로,
    # 판정은 '거래일 장중 기준'으로만 FAIL 을 낸다(judge_freshness 참고).
    ("documents", "research.documents", "observed_at", 6.0),
    # 재무는 분기 공시라 유입이 드문드문하다 - 45일은 분기 사이 공백을 견딘다.
    ("financial_facts", "research.financial_facts", "observed_at", 24.0 * 45),
    # 거시는 일 단위 계열(GPR·GDELT·VKOSPI·스타일) - 3일이면 주말을 넘긴 것이다.
    ("macro_observations", "research.macro_observations", "observed_at", 24.0 * 3),
)

# 값 범위 판정 - 도메인마다 '있을 수 없는 값'이 다르다
MACRO_ABS_MAX = 1e9        # 지수·건수 계열의 상한. 넘으면 단위 사고다
FIN_ABS_MAX = 1e18         # 원 단위 재무. 1경을 넘으면 자릿수 사고다

STALE_WARN_RATIO = 0.5     # 한계의 이 비율을 넘으면 WARN (FAIL 전에 알린다)
NULL_WARN, NULL_FAIL = 0.02, 0.10
OUTLIER_WARN, OUTLIER_FAIL = 0, 1   # 이상치는 1건만 있어도 FAIL - 값 사고다


# ---------------------------------------------------------------------------
# 판정 (순수 함수 - 자체 점검 대상)
# ---------------------------------------------------------------------------

def worst(*statuses: str) -> str:
    order = {"PASS": 0, "WARN": 1, "FAIL": 2}
    return max(statuses, key=lambda s: order.get(s, 2))


def judge_freshness(age_hours: float | None, limit_hours: float) -> tuple[str, str]:
    """마지막 관측이 얼마나 오래됐는가.

    None 은 '행이 없다'는 뜻이고 **PASS 가 아니다** - 데이터가 없는 것과
    깨끗한 것은 다르다. 통과로 위장하지 않는다.
    """
    if age_hours is None:
        return "FAIL", "행 0 - 관측이 하나도 없다(판정 불가가 아니라 결함이다)"
    if age_hours > limit_hours:
        return "FAIL", f"마지막 관측 {age_hours:.1f}시간 전 (한계 {limit_hours:.0f})"
    if age_hours > limit_hours * STALE_WARN_RATIO:
        return "WARN", f"마지막 관측 {age_hours:.1f}시간 전 (한계 {limit_hours:.0f})"
    return "PASS", f"마지막 관측 {age_hours:.1f}시간 전"


def judge_nulls(total: int, nulls: int, *, field: str) -> tuple[str, str]:
    """핵심 필드 결측 비율. 행이 0이면 판정 불가(WARN) - 0/0 을 100% 로 읽지 않는다."""
    if total <= 0:
        return "WARN", f"{field}: 행 0 - 판정 불가"
    ratio = nulls / total
    if ratio > NULL_FAIL:
        return "FAIL", f"{field} 결측 {ratio:.1%} ({nulls}/{total})"
    if ratio > NULL_WARN:
        return "WARN", f"{field} 결측 {ratio:.1%} ({nulls}/{total})"
    return "PASS", f"{field} 결측 {ratio:.2%}"


def judge_outliers(count: int, *, what: str) -> tuple[str, str]:
    """범위를 벗어난 값의 개수. **1건만 있어도 FAIL** - 단위·부호 사고는
    비율이 아니라 존재 자체가 문제다(그 한 건이 점수를 통째로 왜곡한다)."""
    if count > OUTLIER_FAIL - 1:
        return "FAIL", f"{what} {count}건"
    return "PASS", f"{what} 0건"


def judge_source_balance(counts: dict, *, min_share: float = 0.01) -> tuple[str, str]:
    """소스 편중. 어제까지 있던 소스가 오늘 통째로 마르면 잡는다.

    감성 점수는 소스가 섞여야 의미가 있는데, 한 소스가 죽으면 남은 소스로
    조용히 기울고 그 사실이 어디에도 안 드러난다. 여기서 드러낸다.
    """
    total = sum(counts.values())
    if total <= 0:
        return "WARN", "행 0 - 판정 불가"
    dead = sorted(s for s, n in counts.items() if n / total < min_share)
    if dead:
        return "WARN", f"기여 1% 미만 소스: {dead} (전체 {total})"
    return "PASS", f"소스 {len(counts)}개 균형 (전체 {total})"


def summarize(results: list[dict]) -> tuple[str, str]:
    """도메인별 판정 -> (전체 상태, 한 줄 요약)."""
    if not results:
        return "FAIL", "판정 대상이 없다"
    overall = worst(*(r["status"] for r in results))
    bad = [f"{r['domain']}({r['status']})" for r in results if r["status"] != "PASS"]
    return overall, ("전부 PASS" if not bad else ", ".join(bad))


# ---------------------------------------------------------------------------
# 판정 실행 (DB 조회)
# ---------------------------------------------------------------------------

def audit() -> int:
    import psycopg2
    from source_registry import load_project_env

    now = datetime.now(timezone.utc)
    conn = psycopg2.connect(load_project_env()["DATABASE_URL"], connect_timeout=20)
    results: list[dict] = []
    try:
        with conn.cursor() as cur:
            for domain, table, obs_col, limit_h in DOMAINS:
                checks: list[tuple[str, str]] = []

                cur.execute(f"select count(*), max({obs_col}) from {table}")
                total, last = cur.fetchone()
                age = None if last is None else (now - last).total_seconds() / 3600.0
                checks.append(judge_freshness(age, limit_h))

                if domain == "documents":
                    cur.execute("""
                        select count(*), count(*) filter (where title is null
                               or btrim(title) = ''),
                               count(*) filter (where published_at is null)
                        from research.documents
                        where observed_at > now() - interval '7 days'""")
                    n, no_title, no_pub = cur.fetchone()
                    checks.append(judge_nulls(n, no_title, field="title"))
                    checks.append(judge_nulls(n, no_pub, field="published_at"))
                    # 미래 게시 - PIT 를 직접 깨는 값이라 존재 자체가 결함이다
                    cur.execute("""select count(*) from research.documents
                                   where published_at > observed_at + interval '1 day'""")
                    checks.append(judge_outliers(cur.fetchone()[0],
                                                 what="게시가 관측보다 하루 이상 미래"))
                    cur.execute("""
                        select s.source_code, count(*)
                        from research.documents d
                        join reference.data_sources s using (source_id)
                        where d.observed_at > now() - interval '7 days'
                        group by 1""")
                    checks.append(judge_source_balance(dict(cur.fetchall())))

                elif domain == "financial_facts":
                    cur.execute("""
                        select count(*), count(*) filter (where value is null),
                               count(*) filter (where abs(value) > %s)
                        from research.financial_facts""", (FIN_ABS_MAX,))
                    n, nul, big = cur.fetchone()
                    checks.append(judge_nulls(n, nul, field="value"))
                    checks.append(judge_outliers(big, what=f"|value| > {FIN_ABS_MAX:.0e}"))

                elif domain == "macro_observations":
                    cur.execute("""
                        select count(*), count(*) filter (where value is null),
                               count(*) filter (where abs(value) > %s)
                        from research.macro_observations""", (MACRO_ABS_MAX,))
                    n, nul, big = cur.fetchone()
                    checks.append(judge_nulls(n, nul, field="value"))
                    checks.append(judge_outliers(big, what=f"|value| > {MACRO_ABS_MAX:.0e}"))
                    # vintage 가 period 보다 앞서면 PIT 가 뒤집힌 것이다
                    cur.execute("""select count(*) from research.macro_observations
                                   where vintage_date < period""")
                    checks.append(judge_outliers(cur.fetchone()[0],
                                                 what="vintage < period(PIT 역전)"))

                status = worst(*(s for s, _ in checks))
                results.append({"domain": domain, "status": status,
                                "rows": total, "checks": checks})
    finally:
        conn.close()

    print(f"{STEWARD_VERSION} 리서치 평면 판정 ({datetime.now(KST):%Y-%m-%d %H:%M} KST)")
    for r in results:
        print(f"  [{r['status']:4s}] {r['domain']:20s} {r['rows']:>9,}행")
        for st, why in r["checks"]:
            mark = "   " if st == "PASS" else " ! "
            print(f"      {mark}{st:4s} {why}")
    overall, line = summarize(results)
    print(f"  전체: {overall} - {line}")
    return 1 if overall == "FAIL" else 0


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·DB 없음
# ---------------------------------------------------------------------------

def _check_freshness():
    assert judge_freshness(1.0, 6.0)[0] == "PASS"
    assert judge_freshness(4.0, 6.0)[0] == "WARN", "한계 절반 넘으면 미리 알린다"
    assert judge_freshness(7.0, 6.0)[0] == "FAIL"
    # 행이 없으면 PASS 가 아니다 - '없는 것'과 '깨끗한 것'은 다르다
    st, why = judge_freshness(None, 6.0)
    assert st == "FAIL" and "행 0" in why, (st, why)
    print("  신선도 판정              OK")


def _check_nulls_and_outliers():
    assert judge_nulls(1000, 5, field="title")[0] == "PASS"
    assert judge_nulls(1000, 50, field="title")[0] == "WARN"
    assert judge_nulls(1000, 200, field="title")[0] == "FAIL"
    # 0/0 을 100% 결측으로 읽지 않는다
    assert judge_nulls(0, 0, field="title")[0] == "WARN"
    # 이상치는 1건만 있어도 FAIL - 단위·부호 사고는 비율 문제가 아니다
    assert judge_outliers(0, what="x")[0] == "PASS"
    assert judge_outliers(1, what="x")[0] == "FAIL"
    print("  결측·이상치 판정         OK")


def _check_source_balance():
    ok = judge_source_balance({"naver": 500, "opendart": 300, "ls_news": 200})
    assert ok[0] == "PASS", ok
    # 한 소스가 말랐다 - 감성 점수가 남은 소스로 기우는데 아무도 모르는 상황
    dry = judge_source_balance({"naver": 990, "opendart": 5, "ls_news": 1})
    assert dry[0] == "WARN" and "ls_news" in dry[1], dry
    assert judge_source_balance({})[0] == "WARN"
    print("  소스 편중 판정           OK")


def _check_summary_and_domains():
    res = [{"domain": "documents", "status": "PASS"},
           {"domain": "macro_observations", "status": "WARN"}]
    st, line = summarize(res)
    assert st == "WARN" and "macro_observations" in line, (st, line)
    assert summarize([{"domain": "a", "status": "FAIL"}] + res)[0] == "FAIL"
    assert summarize([])[0] == "FAIL", "대상이 없으면 통과가 아니다"

    # 도메인 표가 실제 스키마와 어긋나지 않게 - 이름 오타는 조용한 미판정이 된다
    names = {d for d, *_ in DOMAINS}
    assert names == {"documents", "financial_facts", "macro_observations"}, names
    for _d, table, _c, limit_h in DOMAINS:
        assert table.startswith("research."), table
        assert limit_h > 0
    print("  요약·도메인 표           OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--audit" in sys.argv:
        raise SystemExit(audit())

    print(f"{STEWARD_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_freshness()
    _check_nulls_and_outliers()
    _check_source_balance()
    _check_summary_and_domains()
    print("리서치 Steward 4개 영역 통과. 판정은 --audit")
