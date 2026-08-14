#!/usr/bin/env python3
"""공장이 **어디서 시간을 잃고 있는가**를 원장에서 직접 센다.

▶ 왜 이게 필요한가 (2026-08-12)
  지금까지 병목은 **사람이 하나씩 발견해 코드에 박았다.** `_structurally_blocked`
  는 "어휘 밖 edge_type" 과 "실행면이 안 읽는 파라미터" 를 본다 - 둘 다 그날
  사람이 만나서 넣은 것이다. 그래서 공장은 **넣어 준 병목만** 볼 수 있었고,
  못 넣어 준 것은 매 주기 조용히 같은 값을 태웠다.

  그건 자동화가 아니라 자동 실행이다. 돌린다고 똑똑해지지 않는다.

  이 모듈은 반대로 간다. **무엇이 병목인지 미리 정하지 않는다.** 원장에 남은
  실패·정체·미사용을 세고, 실패 사유는 **서명으로 군집**해서 이름을 모르는
  종류도 잡는다. 새 종류의 사고가 나면 다음 주기에 저절로 목록에 뜬다.

▶ 세는 것과 안 세는 것
  센다  : 원장에 사실로 남은 것 - 실패 횟수, 정체 일수, 미사용 데이터셋,
          관문에서 반복해서 걸리는 조항, 되풀이되는 교훈
  안 센다: "이게 문제일 것 같다" - 추정은 병목이 아니다. 못 재면 안 적는다

▶ 비용은 **주기**로 잰다
  "심각하다" 는 사람마다 다르다. 몇 번 시도를 태웠고 며칠 앉아 있었는지는
  같다. 그래서 순위는 관측된 낭비량으로만 매긴다.

사용:
    python bottleneck_census.py                 # 사람이 읽는 표
    python bottleneck_census.py --json          # 기계 판독
    python bottleneck_census.py --card          # 카드 본문(자동 조종이 쓴다)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[2]
for _p in (_ROOT / "departments" / "01-research" / "collectors",
           _ROOT / "departments" / "04-quant-backtest" / "pipeline"):
    if _p.is_dir():
        sys.path.insert(0, str(_p))

CENSUS_VERSION = "factory-bottleneck-census-v1"

# 정체로 보는 나이. 이보다 오래 비종결 상태면 "돌고 있는" 게 아니다.
STALE_DAYS = float(os.getenv("FACTORY_STALE_DAYS", "2"))
# 이 횟수 이상 같은 서명으로 실패하면 재시도로는 안 풀리는 종류다.
CHURN_ATTEMPTS = int(os.getenv("FACTORY_CHURN_ATTEMPTS", "3"))
# 되풀이 실패를 **현재 진행형**으로 보는 창(일). 이보다 오래된 이력만 있으면
# 차단기가 이미 잡은 것이므로 병목으로 세지 않는다(2026-08-14 실측: 이틀 전
# 끝난 42회 실패가 매 주기 브리핑을 차지하고 있었다).
CHURN_FRESH_DAYS = float(os.getenv("FACTORY_CHURN_FRESH_DAYS", "1.0"))
# 데이터를 모아 두고 이만큼 안 쓰면 수집이 헛돈 것이다.
IDLE_DATASET_DAYS = float(os.getenv("FACTORY_IDLE_DATASET_DAYS", "1"))


# ── 실패 서명 ────────────────────────────────────────────────────────────────
# ▶ **사유 문자열을 그대로 세면 아무것도 안 묶인다.** id·수·경로가 매번 달라서
#   86건이 86종류로 보인다. 변하는 것을 지우면 같은 사고가 하나로 모인다.
#   종류를 미리 알 필요가 없다는 게 핵심이다 - 새 사고도 저절로 묶인다.
_SCRUB = (
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"), "<id>"),
    (re.compile(r"\b[0-9a-f]{7,}\b"), "<hash>"),
    (re.compile(r"/[\w./-]+"), "<path>"),
    (re.compile(r"'[^']*'"), "<val>"),
    (re.compile(r'"[^"]*"'), "<val>"),
    (re.compile(r"\[[^\]]*\]"), "<list>"),
    (re.compile(r"-?\d+\.?\d*"), "<n>"),
    (re.compile(r"\s+"), " "),
)


def signature(reason: str, *, width: int = 110) -> str:
    """실패 사유 -> 군집 키. **변하는 것을 지운다.**"""
    s = str(reason or "").strip()
    for pat, rep in _SCRUB:
        s = pat.sub(rep, s)
    return s[:width].strip() or "(사유 없음)"


@dataclass
class Bottleneck:
    """관측된 병목 하나. **전부 원장에서 세어 나온 값이다.**"""

    kind: str                  # 종류 (군집 이름이 아니라 어디서 났나)
    key: str                   # 같은 병목인지 가르는 키
    cost: float                # 관측된 낭비 (주기·일·시도 수를 정규화)
    unit: str                  # cost 가 무엇의 수인가
    evidence: str              # 사실. 추정이 아니다
    owner: str                 # 어느 부서가 푸나
    hint: str = ""             # 어떤 스킬·도구로 여는가
    ids: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _conn():
    import psycopg2
    from source_registry import load_project_env
    return psycopg2.connect(load_project_env()["DATABASE_URL"], connect_timeout=20)


# ── 세는 것들 ────────────────────────────────────────────────────────────────
# 각 함수는 **못 세면 빈 목록**을 돌려준다. 지어내지 않는다.

def _job_churn(cur) -> list[Bottleneck]:
    """같은 서명으로 되풀이 실패하는 작업. 재시도로는 안 풀리는 종류다.

    ▶ **죽은 병목을 세지 않는다** (2026-08-14 실측)
      차단기(job_queue.REPEAT_BLOCK=3)가 이미 발주를 막고 있는데도 인구조사가
      **누적 이력**을 계속 세어 "같은 사유로 42회 실패" 를 매 주기 병목으로
      올렸다. 마지막 실패는 이틀 전(08-12 13:28)이고 그 뒤로 한 건도 없었다 -
      이미 고쳐진 문제가 브리핑을 차지하고 개선 카드를 태우는 동안 진짜 신규
      병목은 38건 속에 묻힌다. 문헌(품질관리·SRE)이 같은 것을 경고한다:
      **거짓 신호 이력이 쌓인 경보는 사람이 무시하게 된다.**

      그래서 시효를 본다 - 최근 창(CHURN_FRESH_DAYS) 안에 **다시 일어난** 것만
      병목이고, 옛것은 세지 않는다. 다만 조용히 지우지도 않는다: 창 밖 이력은
      증거 문장에 "최근 N일은 잠잠(차단기 작동 중)" 으로 남겨, 고친 사실이
      기록에서 사라지지 않게 한다.
    """
    try:
        cur.execute("""
            select coalesce(failure_reason,''), hypothesis_id::text, attempts,
                   extract(epoch from (now() - coalesce(updated_at, created_at)))/86400.0
              from quant.experiment_jobs
             where status = 'FAILED' and failure_reason is not null""")
        rows = cur.fetchall()
    except Exception:  # noqa: BLE001
        return []
    groups: dict[str, dict] = {}
    for reason, hid, attempts, age_days in rows:
        g = groups.setdefault(signature(reason),
                              {"attempts": 0, "hyps": set(), "raw": reason,
                               "fresh": 0, "min_age": None})
        n = int(attempts or 1)
        g["attempts"] += n
        g["hyps"].add(hid)
        age = float(age_days or 0.0)
        if age <= CHURN_FRESH_DAYS:
            g["fresh"] += n
        g["min_age"] = age if g["min_age"] is None else min(g["min_age"], age)
    out = []
    for sig, g in groups.items():
        if g["attempts"] < CHURN_ATTEMPTS:
            continue
        if g["fresh"] < CHURN_ATTEMPTS:
            # 최근 창에서 임계 미만 = 차단기가 잡고 있다. 병목이 아니다.
            continue
        quiet = ""
        if g["min_age"] is not None and g["min_age"] > 1.0:
            quiet = f" (마지막 실패 {g['min_age']:.1f}일 전)"
        out.append(Bottleneck(
            kind="작업 되풀이 실패", key=f"job:{sig}",
            cost=float(g["fresh"]), unit="시도",
            evidence=(f"최근 {CHURN_FRESH_DAYS}일에 같은 사유로 {g['fresh']}회 "
                      f"실패(누적 {g['attempts']}회) / 가설 {len(g['hyps'])}건"
                      f"{quiet}. 원문: {str(g['raw'])[:110]}"),
            owner="quant-backtest-department",
            hint="wiring-audit 로 층을 가른 뒤, 계약 문제면 리서치에 재등록을 요청한다",
            ids=sorted(g["hyps"])[:8]))
    return out


def _stalled(cur, now) -> list[Bottleneck]:
    """비종결 상태로 오래 앉아 있는 가설. **앉아 있는 것은 도는 게 아니다.**"""
    try:
        cur.execute("""
            select status, hypothesis_id::text,
                   extract(epoch from (now() - coalesce(status_changed_at, created_at)))/86400.0
              from quant.hypotheses
             where status not in ('SUPPORTED','REJECTED','ARCHIVED','INCONCLUSIVE')""")
        rows = cur.fetchall()
    except Exception:  # noqa: BLE001
        return []
    groups: dict[str, list] = {}
    for status, hid, age in rows:
        if float(age or 0) >= STALE_DAYS:
            groups.setdefault(str(status), []).append((hid, float(age)))
    out = []
    for status, items in groups.items():
        days = sum(a for _, a in items)
        oldest = max(a for _, a in items)
        out.append(Bottleneck(
            kind="가설 정체", key=f"stall:{status}",
            cost=days, unit="가설·일",
            evidence=(f"{status} 로 {len(items)}건이 평균 {days/len(items):.1f}일 "
                      f"(최장 {oldest:.1f}일) 앉아 있다"),
            owner="quant-backtest-department" if status != "PROPOSED"
                  else "research-department",
            hint="발주 게이트가 왜 보류했는지 브리핑의 [막혀 있는 가설] 을 본다",
            ids=[h for h, _ in sorted(items, key=lambda x: -x[1])][:8]))
    return out


def _gate_blockers(cur) -> list[Bottleneck]:
    """관문에서 **반복해서** 걸리는 조항. 한 번은 결과, 반복은 병목이다."""
    try:
        cur.execute("""
            select failed_criteria, trial_family_id
              from research.experiment_outcomes
             where decided_at > now() - interval '30 days'""")
        rows = cur.fetchall()
    except Exception:  # noqa: BLE001
        return []
    per: dict[str, set] = {}
    for crits, fam in rows:
        for c in (crits or []):
            per.setdefault(str(c), set()).add(str(fam))
    out = []
    for crit, fams in per.items():
        if len(fams) < 2:          # 한 계열만이면 그 전략의 특성이지 병목이 아니다
            continue
        out.append(Bottleneck(
            kind="관문 상습 조항", key=f"gate:{crit}",
            cost=float(len(fams)), unit="계열",
            evidence=f"'{crit}' 가 서로 다른 계열 {len(fams)}개를 막았다",
            owner="research-department",
            hint=("이 조항을 겨냥한 설계로 바꾼다. 실행면에 손잡이가 없으면 "
                  "그것부터 낸다(experiment-factory §4-b)"),
            ids=sorted(fams)[:8]))
    return out


def _unused_datasets(cur) -> list[Bottleneck]:
    """등재만 되고 실험에 한 번도 안 쓰인 데이터셋. **모아 두고 안 쓰면 헛돈다.**"""
    try:
        cur.execute("""
            select m.name, m.version, m.row_count,
                   extract(epoch from (now() - m.as_of))/86400.0,
                   (select count(*) from quant.experiments e
                     where e.dataset_id = m.dataset_id)
              from quant.dataset_manifests m""")
        rows = cur.fetchall()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for name, ver, rc, age, used in rows:
        if int(used or 0) > 0 or float(age or 0) < IDLE_DATASET_DAYS:
            continue
        out.append(Bottleneck(
            kind="미사용 데이터셋", key=f"dataset:{name}/{ver}",
            cost=float(age or 0), unit="일",
            evidence=(f"{name}/{ver} 가 {int(rc or 0):,}행으로 등재됐는데 "
                      f"{float(age or 0):.1f}일간 실험 0건"),
            owner="quant-backtest-department",
            hint=("dataset-engineering 의 probe_dataset.py 로 층별로 열어 본다 - "
                  "6층에서 막히면 실행면을 넓히는 것이 답이다"),
            ids=[f"{name}/{ver}"]))
    return out


def _feedback_gap(cur) -> list[Bottleneck]:
    """종결됐는데 환류가 없는 실험. **교훈이 사라지면 같은 실험을 다시 산다.**"""
    try:
        cur.execute("""
            select e.experiment_id::text
              from quant.experiments e
              left join research.experiment_outcomes o
                     on o.experiment_id = e.experiment_id::text
             where e.status = 'COMPLETED' and o.outcome_id is null""")
        miss = [r[0] for r in cur.fetchall()]
    except Exception:  # noqa: BLE001
        return []
    if not miss:
        return []
    return [Bottleneck(
        kind="환류 누락", key="feedback:missing",
        cost=float(len(miss)), unit="실험",
        evidence=f"종결된 실험 {len(miss)}건에 환류가 없다 - 그 교훈은 Gate 0 에 안 닿는다",
        owner="quant-backtest-department",
        hint="finalize() 가 환류와 상태 전이를 한 트랜잭션으로 묶는다. 왜 빠졌는지 본다",
        ids=miss[:8])]


def _repeat_lessons(cur) -> list[Bottleneck]:
    """되풀이되는 교훈. **한 번은 배움이고 다섯 번은 안 고쳐진 것이다.**"""
    try:
        cur.execute("""
            select lesson_codes, trial_family_id
              from research.experiment_outcomes
             where decided_at > now() - interval '30 days'""")
        rows = cur.fetchall()
    except Exception:  # noqa: BLE001
        return []
    per: dict[str, set] = {}
    for codes, fam in rows:
        for c in (codes or []):
            per.setdefault(str(c), set()).add(str(fam))
    out = []
    for code, fams in per.items():
        if len(fams) < 3:
            continue
        out.append(Bottleneck(
            kind="교훈 반복", key=f"lesson:{code}",
            cost=float(len(fams)), unit="계열",
            evidence=f"'{code}' 가 계열 {len(fams)}개에서 되풀이됐다 - 개별 설계 문제가 아니다",
            owner="research-department",
            hint="개별 기획안이 아니라 **구조**를 고쳐야 하는 신호다",
            ids=sorted(fams)[:8]))
    return out


def _market_data_quality(cur) -> list[Bottleneck]:
    """**시세 자체의 결함.** 원장이 아니라 시장 DB 를 본다.

    ▶ 왜 여기 있나 (2026-08-12 실측)
      `low_volatility` 가 10년 초과 +855.92%p 를 냈다. 관문 6/9 통과 - 역대
      가장 가까웠다. 그런데 수익의 34%가 한 종목의 한 체결이었고, 그 체결은
      **미조정 액면병합(×10.0000)** 을 먹은 것이었다.

      경로가 이렇다:
        거래정지일은 거래량 0 인데 종가가 이월된다
          -> 수익률 0 으로 계산되어 **변동성이 깎인다**
          -> 저변동성 선택이 정지 잦은 종목을 과다 선택한다
             (실측: 유니버스 평균 2.4% vs 선택된 종목 12.9%, 5.4배)
          -> 그 종목들이 정확히 미조정 갭을 갖고 있다

      **미관측을 0으로 읽은 것이 원인이다.** 이 저장소가 원장 층에서는 열두 번
      고친 원칙인데 시세 층에서 깨져 있었다. 사람이 손으로 찾았다 - 다음 것은
      공장이 찾아야 한다.

    ▶ **판정하지 않는다.** 거래량 0 을 어떻게 다룰지는 방법론 판단이고
      에이전트 몫이다. 여기서는 **얼마나 있는지 세어 보여줄 뿐**이다.
    """
    import os  # noqa: PLC0415

    dsn = os.getenv("TIMESCALE_DATABASE_URL")
    if not dsn:
        return []
    try:
        import psycopg2  # noqa: PLC0415

        mc = psycopg2.connect(dsn, connect_timeout=20)
    except Exception:  # noqa: BLE001 - 못 보면 안 적는다
        return []
    out: list[Bottleneck] = []
    try:
        m = mc.cursor()
        m.execute("""
            select count(*) tot,
                   count(*) filter (where volume is null or volume = 0) z,
                   count(distinct instrument_id) syms
              from market.market_bars
             where interval_code = '1D'
               and bucket_time >= now() - interval '10 years'""")
        tot, z, syms = m.fetchone()
        if tot and z and z / tot > 0.005:
            out.append(Bottleneck(
                kind="시세 미관측", key="market:zero_volume",
                cost=float(z), unit="봉",
                evidence=(f"일봉 {int(tot):,}개 중 거래량 0 이 {int(z):,}개 "
                          f"({100.0*z/tot:.1f}%). 종가는 이월되므로 수익률 0 으로 "
                          f"계산되고 변동성이 깎인다 - **미관측과 무변동이 섞여 있다**"),
                owner="quant-backtest-department",
                hint=("변동성·수익률 계산에서 이 날을 어떻게 다룰지 정하고 "
                      "실험으로 검증해라. dataset-engineering 참고. "
                      "저변동성·최소분산 계열이 특히 이 편향에 노출된다"),
                ids=[f"{int(syms or 0)}종목"]))

        m.execute("""
            with s as (select instrument_id, bucket_time::date d, close,
                              lag(close) over (partition by instrument_id
                                               order by bucket_time) pc
                         from market.market_bars
                        where interval_code='1D'
                          and bucket_time >= now() - interval '10 years')
            select count(*), min(d), max(d) from s
             where pc > 0 and close > 0
               and (abs(close/pc-2)<0.02 or abs(close/pc-3)<0.03
                    or abs(close/pc-5)<0.05 or abs(close/pc-10)<0.1
                    or abs(close/pc-0.5)<0.005 or abs(close/pc-0.2)<0.002
                    or abs(close/pc-0.1)<0.001)""")
        n, lo, hi = m.fetchone()
        if n:
            out.append(Bottleneck(
                kind="시세 정수배 갭", key="market:integer_gap",
                cost=float(n), unit="건",
                evidence=(f"일간 종가가 정확히 정수배로 뛴 곳 {int(n)}건 "
                          f"({lo} ~ {hi}). ×10.0000 같은 값은 액면분할·병합이 "
                          f"조정되지 않았다는 뜻이다 - 수익률로 읽으면 900% 다"),
                owner="quant-backtest-department",
                hint=("수집기는 이미 sujung=Y 다. 같은 배치에서도 남았으므로 "
                      "벤더 조정이 이 케이스를 안 덮는다 - 재수집으로는 안 풀린다. "
                      "빌드나 계산 층에서 다룰지 정해라"),
                ids=[str(lo), str(hi)]))
    except Exception:  # noqa: BLE001
        return []
    finally:
        mc.close()
    return out


LEAD_IDLE_DAYS = 3     # 이 정도 묵었는데 기획이 안 나오면 못 내는 것이다


def _idle_leads(cur) -> list[Bottleneck]:
    """**수확했는데 아무도 기획하지 않은 리드.** 실행면 수요가 여기 고인다.

    ▶ 왜 이 계수기가 필요했나 (2026-08-12 실측)
      통제 어휘(8종) 밖의 edge_type 은 Gate 0 이 `UNMAPPED_VOCAB` 으로 막는데,
      **그 반려가 한 번도 일어난 적이 없다.** 기획안 11건이 전부 어휘 안이다.
      막혀서가 아니라 브리핑이 "어휘에 없으면 가장 가까운 코드를 쓰라"고
      지시했고 기획자가 매번 얌전히 깎아 냈기 때문이다.

      그래서 이 병목은 **반려로는 절대 안 잡힌다.** 자기검열은 원장에 흔적을
      남기지 않는다 - 묻지 않은 것은 셀 수 없다. 대신 증상은 남는다:
      리드가 수확되고도 기획으로 안 바뀐 채 묵는다.

      담당을 퀀트로 두는 이유: 리서치 카드는 이미 안 쓴 리드를 맨 앞에 싣고
      있다(그쪽은 "기획해라"). 여기서 묻는 것은 다른 질문이다 -
      **"이 리드를 실행면에 올릴 수 있나"**. 리서치는 리드를 보지만 만들
      권한이 없고, 퀀트는 만들 수 있지만 리드를 못 보고 있었다.
    """
    try:
        cur.execute("""
            select l.lead_id, left(coalesce(l.claimed_edge,''), 78),
                   (now() - l.created_at)
              from research.methodology_leads l
             where not exists (select 1 from research.experiment_proposals p
                                where l.lead_id = any(p.lead_ids))
               and l.created_at < now() - interval '%s days'
             order by l.created_at
        """ % int(LEAD_IDLE_DAYS))
        rows = cur.fetchall()
    except Exception:  # noqa: BLE001 - 못 세면 안 적는다
        return []
    if not rows:
        return []
    oldest = max((r[2].days if r[2] else 0) for r in rows)
    titles = [r[1] for r in rows if r[1]]
    return [Bottleneck(
        kind="리드 사장", key="lead:idle",
        cost=float(len(rows)), unit="건",
        evidence=(f"수확했는데 {LEAD_IDLE_DAYS}일 넘게 기획으로 안 바뀐 리드 "
                  f"{len(rows)}건(최장 {oldest}일). 리서치는 리드를 보지만 "
                  f"실행면을 못 고치고, 실행면을 고칠 수 있는 쪽은 리드를 "
                  f"못 보고 있었다 - 그 사이에 고여 있다"),
        owner="quant-backtest-department",
        hint=("**어휘 밖이라 못 낸 것인지 먼저 갈라라.** 지금 EDGE_TYPE 은 "
              "8종이고 Gate 0 은 그 밖을 접수하지 않는다. 리드가 필요로 하는 "
              "신호가 PITView 재료(closes/notionals/daily_returns)로 만들어지면 "
              "`strategy_templates.TEMPLATES` 에 템플릿을 더해라 - 어휘는 거기서 "
              "파생되므로 그 순간 Gate 0 이 열린다. 재료가 모자라면 무엇이 "
              "없는지 `NOT_IMPLEMENTED` 에 사유와 함께 적어라(그러면 반려가 "
              "이유를 말해 준다). 어느 쪽도 아니면 리드가 나쁜 것이니 폐기 "
              "사유를 남겨라 - 셋 다 아닌 채로 두지 마라"),
        ids=titles[:5])]


def _soundness(cur) -> list[Bottleneck]:
    """**건전성 3조항 위반을 귀속해서 싣는다.** (2026-08-12)

    van der Aalst 의 워크플로 넷 건전성 - 완주가능 · 죽은전이 · 정상종결 -
    을 원장에 대고 재고(`soundness`), 각 위반을 개입 수준으로 갈라
    (`attribution`) 손댈 수 있는 부서로 보낸다.

    ▶ 왜 조항별이 아니라 (조항 x 수준) 으로 쪼개나
      같은 조항 위반이라도 처방이 다르다. `774f3b75`(고아, 새 기획안 또는
      폐기)와 `667f0a45`(기획안 있음, 다시 등록)은 둘 다 완주가능 위반인데
      갈 곳이 다르다. 한 덩어리로 묶어 한 부서에 보내면 절반은 권한이 없어
      되돌아온다 - 그게 오늘의 아킬레스건이었다.
    """
    try:
        import attribution as attr                 # noqa: PLC0415
        import soundness as snd                    # noqa: PLC0415
    except Exception:  # noqa: BLE001 - 못 재면 안 적는다
        return []
    conn = getattr(cur, "connection", None)
    if conn is None:
        return []

    def _blocked(c, ids):
        # 발주 게이트의 판정을 그대로 쓴다. 같은 판단을 두 곳에서 하면 갈린다.
        try:
            import factory_autopilot as fa         # noqa: PLC0415 (순환 회피)
            return fa._structurally_blocked(c, ids) or {}
        except Exception:  # noqa: BLE001
            return {}

    try:
        violations, _v = snd.report(conn, blocked_fn=_blocked)
    except Exception:  # noqa: BLE001
        return []
    if not violations:
        return []

    # ▶ **고아를 같은 묶음에 넣지 않는다.** 처방이 다르기 때문이다 - 섞으면
    #   카드에 지시가 둘 나가고, 어느 것이 누구 것인지 읽는 쪽이 모른다.
    groups: dict[tuple, list] = {}
    for x in violations:
        a = attr.attribute(x.why)
        orphan = "고아" in (x.ids or [])
        groups.setdefault((x.clause, a.level, a.owner, a.action, orphan),
                          []).append(x)

    out = []
    for (clause, level, owner, action, orphan), g in groups.items():
        if level == attr.TRANSIENT:
            continue          # 재시도로 풀리는 것은 병목이 아니다
        subjects = [x.subject for x in g]
        # ▶ **처방을 두 개 붙이지 않는다** (2026-08-12, 카드를 렌더해 보고 발견)
        #   `option_to_complete` 의 detail 은 리서치 관점("계약에 맞게 다시
        #   내라")으로 쓰여 있다. 구현 수준 위반에 그것을 이어 붙이면
        #   "템플릿을 더해라 / 다시 내면 된다" 가 되어 읽는 쪽이 어느 쪽을
        #   믿을지 모른다 - 오늘 같은 형태의 엇갈린 신호가 8일을 끌었다.
        #   고아만 예외다. 거기 detail 은 귀속이 모르는 정보(다시 등록할
        #   원본이 없다 -> 새 기획안 또는 폐기)를 들고 있다.
        detail = g[0].detail if orphan else ""
        out.append(Bottleneck(
            kind=f"건전성:{clause}",
            key=f"sound:{clause}:{level}" + (":고아" if orphan else ""),
            cost=float(len(g)), unit="건",
            evidence=(f"[{level} 수준{'·고아' if orphan else ''}] "
                      f"{g[0].why[:120]}"
                      + (f" (외 {len(g)-1}건)" if len(g) > 1 else "")),
            owner=owner,
            hint=(f"{detail} 원래 사유: {action}" if orphan else action),
            ids=subjects[:5]))
    return out


def _capability_blocks(cur) -> list[Bottleneck]:
    """**에이전트가 스스로 "이건 내 능력 밖" 이라고 신고한 카드.**

    ▶ 왜 이게 제일 강한 신호인가 (2026-08-12, 주기를 돌려 보고 발견)
      `t_6067c079` 이 `status=blocked, block_kind=capability, 연속실패 0,
      오류 없음` 으로 돌아왔다. 실패가 아니다 - 에이전트가 카드를 읽고
      **못 하겠다고 신고한 것**이다.

      그런데 공장은 카드를 걸 때 상태를 읽어서 **찍기만 하고 버렸다**
      (`_create_card` 가 `(blocked, assignee=...)` 를 출력하고 끝). 즉
      에이전트는 물었고 아무도 안 들었다. MAST 의 FM-2.2 가 "묻지 못하는
      것" 을 실패로 세는데, 여기는 그 반대 - **물었는데 안 듣는 것**이다.
      추론으로 만든 어떤 수요 계수기보다 이게 직접 관측이다.

      이 신호가 곧 실행면 수요다. 무엇이 없어서 못 하는지가 카드 본문에
      있으므로 귀속(`attribution`)이 그것을 수준으로 가른다.
    """
    try:
        import factory_autopilot as fa             # noqa: PLC0415 (순환 회피)
        rows = fa._board_rows(
            "select id, assignee, title, coalesce(block_kind,''), "
            "coalesce(block_recurrences,0) from tasks "
            "where status='blocked' and coalesce(block_kind,'') <> ''")
    except Exception:  # noqa: BLE001 - 보드를 못 읽으면 안 적는다
        return []
    if not rows:
        return []

    groups: dict[tuple, list] = {}
    for tid, who, title, kind, rec in rows:
        groups.setdefault((str(who), str(kind)), []).append(
            (str(tid), str(title), int(rec or 0)))

    # ▶ **막힘 사유를 두 갈래로 가른다** (2026-08-14, AWS Agentic AI Lens)
    #   문헌은 역량 부족(다른 에이전트·부서로 재라우팅할 문제)과 사람
    #   에스컬레이션(신뢰도·위험·재시도 예산 소진)을 **별도 트리거**로 두고,
    #   섞는 것을 안티패턴으로 규정한다. 우리는 14건을 `능력 밖 신고` 한
    #   덩어리로 묶어 놓아서 "누가 받아야 하나" 가 매번 사람 판단이었다.
    #   여기서는 이름만 가른다 - 자동 재라우팅은 별도 결정이다.
    _ROUTE = {"capability": "역량 부족(재라우팅 후보)",
              "needs_input": "입력 대기(사람 확인 필요)",
              "transient": "일시 실패(재시도 대상)"}

    out = []
    for (who, kind), g in groups.items():
        rec = sum(x[2] for x in g)
        lane = _ROUTE.get(kind, f"기타({kind})")
        out.append(Bottleneck(
            kind=f"막힘: {lane}", key=f"cap:{who}:{kind}",
            cost=float(len(g)), unit="건",
            evidence=(f"{who} 가 카드 {len(g)}건을 `{kind}` 로 막았다 - 실패가 "
                      f"아니라 **못 하겠다는 신고**다"
                      + (f" (재발 {rec}회)" if rec else "")
                      + f". 예: {g[0][1][:60]}"),
            owner=who,
            hint=("**네가 못 한다고 신고한 것이다.** 무엇이 없어서 못 하는지 "
                  "한 줄로 적어라 - 도구인지, 권한인지, 실행면에 자리가 "
                  "없는지. 만들 수 있는 것이면 만들어라(스킬은 "
                  "`skill-authoring`, 실행면은 experiment-factory §4 가 "
                  "허용한다). 만들 수 없는 것이면 그 사실이 남아야 다음 "
                  "주기가 같은 카드를 또 안 건다 - 지금은 신고가 아무 데도 "
                  "안 남고 매 주기 같은 카드가 다시 걸린다"),
            ids=[x[0] for x in g][:5]))
    return out


# ▶ `_capability_blocks` 는 여기 없다 (2026-08-12)
#   COUNTERS 의 계수기는 **원장 커서로** fail-closed 해야 한다는 계약이 있고
#   (`_check_counts_only_what_is_observed`), 이 계수기는 원장이 아니라 칸반
#   보드를 읽는다. 넣었더니 그 검사가 바로 잡았다 - 시세 DB 를 읽는
#   `_market_data_quality` 가 따로 불리는 것과 같은 이유다.
COUNTERS = (_job_churn, _gate_blockers, _unused_datasets, _feedback_gap,
            _repeat_lessons, _idle_leads, _soundness)


def census(conn=None) -> list[Bottleneck]:
    """관측된 병목 전부, 낭비량 내림차순."""
    own = conn is None
    conn = conn or _conn()
    now = datetime.now(timezone.utc)
    out: list[Bottleneck] = []
    try:
        cur = conn.cursor()
        for fn in COUNTERS:
            try:
                out.extend(fn(cur))
            except Exception:  # noqa: BLE001 - 한 계수기가 죽어도 나머지는 센다
                conn.rollback()
        try:
            out.extend(_stalled(cur, now))
        except Exception:  # noqa: BLE001
            conn.rollback()
        # 시세 DB 는 다른 연결이다 - 자기 커서를 안 쓴다
        try:
            out.extend(_market_data_quality(cur))
        except Exception:  # noqa: BLE001
            pass
        # 칸반 보드도 다른 저장소다. 원장 커서로 fail-closed 하지 않으므로
        # COUNTERS 밖에서 부른다.
        try:
            out.extend(_capability_blocks(cur))
        except Exception:  # noqa: BLE001
            pass
    finally:
        if own:
            conn.close()
    # 단위가 다른 값을 한 줄로 세우지 않는다 - 종류 안에서 정렬하고
    # 종류끼리는 관측 건수로 편다. 억지 환산은 없는 정밀도를 만든다.
    return sorted(out, key=lambda b: (-b.cost, b.kind, b.key))


# ── 카드 본문 ────────────────────────────────────────────────────────────────

def card_body(items: list[Bottleneck], *, top: int = 3) -> str:
    """자동 조종이 부서에 거는 개선 카드. **없으면 빈 문자열**(카드를 안 만든다)."""
    if not items:
        return ""
    # ▶ **종류마다 하나씩 싣는다** (2026-08-12)
    #   원가 상위 N 건을 그대로 실으면 단위가 큰 종류가 카드를 독점한다.
    #   실측: 시세 미관측이 166,979'봉' 이라 상위 3칸을 혼자 밀어내, 리드
    #   사장(2'건')·환류 누락(21'실험') 같은 다른 종류는 켜져도 안 보였다.
    #   봉과 시도와 건은 같은 자로 잴 수 없다 - 억지로 세우면 없는 정밀도가
    #   생기고, 그 정밀도가 카드의 우선순위 행세를 한다.
    #   그래서 종류로 먼저 가르고, 종류 안에서만 큰 것을 고른다.
    by_kind: dict[str, list[Bottleneck]] = {}
    for b in items:
        by_kind.setdefault(b.kind, []).append(b)
    # 종류끼리는 원가가 아니라 **관측 건수**로 편다 - 여러 자리에서 나는 것이
    # 한 자리에서 크게 나는 것보다 구조적이다.
    order = sorted(by_kind.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    picked = [g[0] for _, g in order]
    # 자리가 남으면 같은 종류의 다음 것으로 채운다(종류를 다 실은 뒤에만)
    for _, g in order:
        picked.extend(g[1:])
    # ▶ **종류를 잘라내지 않는다** (2026-08-12, 두 번째로 같은 것에 데였다)
    #   `top` 이 종류 수보다 작으면 남는 종류는 동점 처리(알파벳순)로 잘린다.
    #   실측: 퀀트에 8종류가 있는데 top=3 이라 `능력 밖 신고` 가 떨어졌다 -
    #   에이전트가 직접 "못 하겠다"고 신고한 것이고, 이 시스템에서 유일하게
    #   **추론이 아니라 진술**인 신호인데 알파벳 때문에 안 보였다.
    #   집중은 "하나를 골라 끝까지 없애라" 는 지시가 만드는 것이지 정보를
    #   숨겨서 만드는 게 아니다. 종류마다 한 줄은 반드시 실린다.
    top = max(int(top), len(order))
    lines = [
        "공장이 지금 어디서 시간을 잃고 있는지 **원장에서 세어** 온 것이다.",
        "추정이 아니라 관측이다. **종류마다 하나씩** 실었다 - 봉·시도·건은",
        f"같은 자로 못 재므로 한 줄로 세우지 않는다(관측된 종류 {len(order)}가지).",
        "",
    ]
    for i, b in enumerate(picked[:top], 1):
        lines += [f"{i}. [{b.kind}] {b.evidence}",
                  f"   낭비: {b.cost:.0f}{b.unit} | 담당: {b.owner}",
                  f"   여는 법: {b.hint}"]
        if b.ids:
            lines.append(f"   대상: {', '.join(str(x)[:14] for x in b.ids[:5])}")
        lines.append("")
    lines += [
        "**하나를 골라 끝까지 없애라.** 여러 개를 조금씩 건드리면 다음 주기에",
        "전부 그대로 남는다. 고른 이유와 안 고른 이유를 카드에 적어라.",
        "",
        "▶ 이 목록은 **원장에서 다시 세어** 만든다. 진짜로 없애면 다음 주기에",
        "  저절로 사라지고, 겉만 덮으면 그대로 남는다. 보고가 아니라 상태다.",
        "",
        "▶ 여는 도구가 없으면 **도구를 만들어라.** 그리고 그것을 스킬로 남겨라",
        "  (skill-authoring). 다음 주기의 네가 같은 자리를 다시 파지 않는다.",
    ]
    return "\n".join(lines)


# ── 자체 점검 ────────────────────────────────────────────────────────────────

def _check_signature_groups_the_accident():
    """**서명이 실제 사고를 하나로 묶는다.** (2026-08-12 실측 원문)

    작업 실패 86건이 실은 8종류였다. id·수·목록이 매번 달라 그대로 세면
    86종류로 보인다. 묶이지 않으면 순위가 무의미하다.
    """
    a = ("가설 파라미터 거부: 실행면이 읽지 않는 파라미터가 있다: "
         "['signal_window_days', 'walk_forward_window_days'] - 무시하고 돌리면")
    b = ("가설 파라미터 거부: 실행면이 읽지 않는 파라미터가 있다: "
         "['observation_refs', 'universe'] - 무시하고 돌리면")
    assert signature(a) == signature(b), (signature(a), signature(b))

    c = "회수 가설 774f3b75 RUNNING 11168분 -> PROPOSED"
    d = "회수 가설 667f0a45 RUNNING 30분 -> PROPOSED"
    assert signature(c) == signature(d), (signature(c), signature(d))

    # **다른 사고는 안 묶여야 한다.** 다 묶이면 순위가 또 무의미하다.
    e = "BadGzipFile: Not a gzipped file (b'PA')"
    assert signature(a) != signature(e)
    assert signature("") == "(사유 없음)"
    print("  실패 서명 군집           OK")


def _check_fixed_bottleneck_is_not_reported_again():
    """**차단기가 잡은 옛 실패를 병목으로 다시 올리지 않는다** (2026-08-14 실측).

    REPEAT_BLOCK=3 이 발주를 막은 뒤 이틀간 한 건도 안 죽었는데, 인구조사가
    누적 42회를 계속 세어 매 주기 브리핑을 차지했다. 거짓 신호가 쌓인 경보는
    사람도 에이전트도 무시하게 된다 - 그러면 진짜 신규 병목이 묻힌다.
    """
    class _Cur:
        def __init__(self, rows): self._rows = rows
        def execute(self, *a, **k): pass
        def fetchall(self): return self._rows

    reason = "verdict=NOT_RUNNABLE; backlog=사상할 데이터셋이 없다"
    # ① 옛 실패만 있는 경우(마지막 실패 2일 전) - 병목이 아니다
    old = [(reason, f"h{i}", 7, 2.0 + i * 0.1) for i in range(6)]
    assert _job_churn(_Cur(old)) == [], "고쳐진 옛 실패를 다시 병목으로 올렸다"

    # ② 지금도 죽고 있으면 병목이다
    fresh = [(reason, f"h{i}", 7, 0.1) for i in range(6)]
    got = _job_churn(_Cur(fresh))
    assert got and got[0].kind == "작업 되풀이 실패", got
    assert "최근" in got[0].evidence and "누적" in got[0].evidence, got[0].evidence

    # ③ 옛것과 새것이 섞이면: 최근 창 기준으로 임계를 넘어야만 올린다
    mixed = [(reason, "h0", 40, 3.0), (reason, "h1", 1, 0.2)]
    assert _job_churn(_Cur(mixed)) == [], "누적 40회에 최근 1회인데 병목으로 올렸다"
    print("  고쳐진 병목 재보고 금지  OK")


def _check_counts_only_what_is_observed():
    """**못 세면 안 적는다.** 계수기는 실패해도 빈 목록이지 추정이 아니다."""
    class _Boom:
        def execute(self, *a, **k): raise RuntimeError("표 없음")
        def fetchall(self): return []
    for fn in COUNTERS:
        assert fn(_Boom()) == [], f"{fn.__name__} 이 못 세고도 무언가를 적었다"
    print("  못 세면 안 적음          OK")


def _check_card_is_empty_when_clean():
    """병목이 없으면 **카드를 안 만든다.** 없는 일을 지어내면 에이전트가 낭비한다."""
    assert card_body([]) == ""
    one = Bottleneck(kind="미사용 데이터셋", key="dataset:x/v1", cost=3.0,
                     unit="일", evidence="x/v1 가 148,931행으로 등재됐는데 실험 0건",
                     owner="quant-backtest-department", hint="probe_dataset.py")
    body = card_body([one])
    assert "148,931행" in body and "probe_dataset.py" in body, body
    # 스스로 사라지는 성질을 카드가 말해야 한다 - 안 그러면 보고서로 읽힌다
    assert "다시 세어" in body, body
    print("  깨끗하면 카드 없음       OK")


def _check_new_kind_needs_no_code():
    """**모르는 종류도 잡힌다.** 병목 종류를 미리 열거하지 않는다는 증거."""
    class _Cur:
        def execute(self, sql, *a):
            self._sql = sql
        def fetchall(self):
            if "experiment_jobs" in self._sql:
                return [("듣도 보도 못한 새 사고: code 42 at /x/y.py", "h1", 5),
                        ("듣도 보도 못한 새 사고: code 77 at /a/b.py", "h2", 4)]
            return []
    got = _job_churn(_Cur())
    assert len(got) == 1, got
    assert got[0].cost == 9.0, got[0].cost
    assert "듣도 보도 못한" in got[0].evidence
    print("  새 종류도 코드 없이 잡음  OK")


def _check_market_quality_needs_no_ledger():
    """**시세 결함은 원장이 아니라 시세 DB 에서 센다.** DSN 이 없으면 안 적는다.

    (2026-08-12) 사람이 손으로 찾은 결함이다 - 거래량 0 봉이 변동성을 깎아
    저변동성 선택을 편향시켰다. 다음 것은 공장이 찾아야 한다.
    """
    import os

    saved = os.environ.pop("TIMESCALE_DATABASE_URL", None)
    try:
        assert _market_data_quality(None) == [], "DSN 없이도 무언가를 적었다"
    finally:
        if saved is not None:
            os.environ["TIMESCALE_DATABASE_URL"] = saved
    # 못 붙어도 죽지 않는다 - 다른 계수기가 계속 돌아야 한다
    os.environ["TIMESCALE_DATABASE_URL"] = "postgresql://x:y@127.0.0.1:1/none"
    try:
        assert _market_data_quality(None) == [], "못 붙었는데 지어냈다"
    finally:
        if saved is not None:
            os.environ["TIMESCALE_DATABASE_URL"] = saved
        else:
            os.environ.pop("TIMESCALE_DATABASE_URL", None)
    print("  시세 결함도 관측만        OK")


def _check_idle_leads_reach_the_builder():
    """**리드가 고이면 만들 수 있는 쪽에 간다.** 담당이 리서치면 아무 일도 안 난다.

    (2026-08-12 실측) 어휘 밖 반려는 0건이었다. 막힌 적이 없어서가 아니라
    브리핑이 깎으라고 시켰고 기획자가 매번 깎았기 때문이다. 자기검열은
    반려 기록을 안 만드므로 **반려를 세는 계수기로는 영원히 안 보인다.**
    그래서 증상(리드가 묵는 것)을 센다.
    """
    from datetime import timedelta

    class _Cur:
        def execute(self, sql, *a):
            assert "methodology_leads" in sql
            assert "not exists" in sql, "이미 기획된 리드까지 세면 안 된다"

        def fetchall(self):
            return [("lead_a", "Maxing Out: Stocks as Lotteries", timedelta(days=9)),
                    ("lead_b", "Overnight versus intraday returns", timedelta(days=4))]

    got = _idle_leads(_Cur())
    assert len(got) == 1 and got[0].cost == 2.0, got
    # 만들 권한이 있는 쪽으로 가야 한다 - 리서치로 가면 되돌아온다
    assert got[0].owner == "quant-backtest-department", got[0].owner
    # 무엇을 하라는지가 실행면 언어여야 한다(기획하라가 아니라)
    assert "TEMPLATES" in got[0].hint and "NOT_IMPLEMENTED" in got[0].hint
    assert "폐기" in got[0].hint, "셋째 길(리드가 나쁘다)이 없으면 다 미룬다"
    assert "9일" in got[0].evidence, got[0].evidence

    class _Empty:
        def execute(self, sql, *a): pass
        def fetchall(self): return []

    assert _idle_leads(_Empty()) == [], "고인 게 없는데 카드를 만들었다"
    print("  고인 리드가 만드는 쪽으로 OK")


def _check_card_shows_distinct_kinds():
    """**단위가 큰 종류가 카드를 독점하지 못한다.** (2026-08-12 실측 값)

    시세 미관측 166,979'봉' 하나가 상위 세 칸을 밀어내면, 리드 사장 2'건' 은
    계수기를 만들어 놔도 영원히 카드에 못 오른다. 봉과 건을 한 자로 재는
    순간 없는 정밀도가 생기고 그게 우선순위 행세를 한다.
    """
    items = [
        Bottleneck(kind="시세 미관측", key="a", cost=166979, unit="봉",
                   evidence="e", owner="q", hint="h"),
        Bottleneck(kind="시세 미관측", key="b", cost=90000, unit="봉",
                   evidence="e", owner="q", hint="h"),
        Bottleneck(kind="작업 되풀이 실패", key="c", cost=35, unit="시도",
                   evidence="e", owner="q", hint="h"),
        Bottleneck(kind="작업 되풀이 실패", key="d", cost=20, unit="시도",
                   evidence="e", owner="q", hint="h"),
        Bottleneck(kind="리드 사장", key="e", cost=2, unit="건",
                   evidence="리드가 고여 있다", owner="q", hint="h"),
    ]
    body = card_body(items, top=3)
    assert "리드 사장" in body, "가장 작은 종류가 밀려났다:\n" + body
    assert body.count("시세 미관측") == 1, "한 종류가 두 칸을 먹었다"
    # 같은 종류 안에서는 큰 것을 고른다
    assert "166979" in body.replace(",", "") or "166979봉" in body

    # 종류가 자리보다 적으면 남은 자리는 같은 종류의 다음 것으로 채운다
    two = card_body(items[:2] + items[2:3], top=3)
    assert two.count("시세 미관측") == 2, two

    # **종류 수가 자리보다 많으면 자리를 늘린다.** 자르면 알파벳순으로 임의로
    # 떨어지고, 실측으로 `능력 밖 신고`(에이전트 본인의 진술)가 그렇게 잘렸다.
    many = items + [
        Bottleneck(kind=f"종류{i}", key=f"k{i}", cost=1, unit="건",
                   evidence="e", owner="q", hint="h") for i in range(6)]
    body2 = card_body(many, top=3)
    for k in {b.kind for b in many}:
        assert k in body2, f"종류 {k} 가 카드에서 잘렸다"
    print("  카드가 종류를 고루 실음  OK")


def _check_soundness_routes_to_who_can_fix():
    """**건전성 위반이 손댈 수 있는 부서로 간다.** (2026-08-12)

    같은 조항 위반이라도 수준이 다르면 갈 곳이 다르다. 어휘에 없는 것은
    실행면(퀀트)이 고치고, 잘못된 키는 기획안(리서치)이 고친다. 한 덩어리로
    보내면 절반은 권한이 없어 되돌아온다 - 그게 오늘의 아킬레스건이었다.
    """
    import attribution as attr
    import soundness as snd

    class _Cur:
        connection = None

    assert _soundness(_Cur()) == [], "연결 없이 무언가를 적었다"

    # 라우팅 자체를 직접 잰다(DB 없이). 두 위반은 수준이 달라야 한다.
    v_impl = snd.option_to_complete({"h1": "'short_term_reversal' 는 어휘에 없다"})
    v_hyp = snd.option_to_complete({"h2": "실행면이 안 읽는 파라미터: universe"})
    a1, a2 = attr.attribute(v_impl[0].why), attr.attribute(v_hyp[0].why)
    assert a1.level == attr.IMPLEMENTATION and a2.level == attr.HYPOTHESIS
    assert a1.owner != a2.owner, "수준이 다른데 같은 부서로 간다"

    # 일시적인 것은 병목이 아니다 - 재시도가 맞는 종류다
    a3 = attr.attribute("시장 DB 연결이 없어 커버리지를 재지 못했다")
    assert a3.level == attr.TRANSIENT and a3.retryable

    # **처방이 하나여야 한다.** 구현 지시 뒤에 리서치 지시를 이어 붙이면
    # 읽는 쪽이 어느 쪽을 믿을지 모른다(실측으로 카드에 그렇게 나갔다).
    v_imp = snd.option_to_complete({"h1": "'x' 는 어휘에 없다"})[0]
    assert "다시 내면 된다" in v_imp.detail, "픽스처 전제가 바뀌었다"
    v_orp = snd.option_to_complete({"h2": "'x' 는 어휘에 없다"},
                                   orphan_ids={"h2"})[0]
    assert "폐기" in v_orp.detail and "고아" in (v_orp.ids or [])
    print("  건전성이 손댈 곳으로     OK")


def _check_capability_report_is_heard():
    """**에이전트가 "못 하겠다"고 하면 그 말이 어딘가에 남는다.** (2026-08-12)

    실측: `t_6067c079` 이 `block_kind=capability` 로 돌아왔는데(실패 아님,
    연속실패 0, 오류 없음) 공장은 그 상태를 출력만 하고 버렸다. 에이전트는
    물었고 아무도 듣지 않았다 - 그래서 매 주기 같은 카드가 다시 걸렸다.
    """
    import factory_autopilot as fa

    saved = fa._board_rows
    try:
        fa._board_rows = lambda sql, params=(): [
            ("t_6067c079", "quant-backtest-department",
             "공장 개선: 관측된 병목 12건", "capability", 2),
            ("t_aaa", "quant-backtest-department",
             "다른 카드", "capability", 0),
        ]
        got = _capability_blocks(None)
        assert len(got) == 1 and got[0].cost == 2.0, got
        # 신고한 쪽에게 돌려준다 - 능력은 자기가 만드는 것이다
        assert got[0].owner == "quant-backtest-department"
        # 실패로 읽으면 안 된다. 신고다.
        assert "신고" in got[0].evidence, got[0].evidence
        assert "재발 2회" in got[0].evidence
        assert "무엇이 없어서" in got[0].hint

        # 보드를 못 읽으면 지어내지 않는다
        def _boom(*a, **k):
            raise RuntimeError("보드 조회 실패")

        fa._board_rows = _boom
        assert _capability_blocks(None) == []
        # 막힌 카드가 없으면 병목도 없다
        fa._board_rows = lambda sql, params=(): []
        assert _capability_blocks(None) == []
    finally:
        fa._board_rows = saved
    print("  능력 밖 신고를 듣는다     OK")


def _selfcheck() -> int:
    print(f"{CENSUS_VERSION} 자체 점검 (DB 없음)")
    _check_market_quality_needs_no_ledger()
    _check_signature_groups_the_accident()
    _check_fixed_bottleneck_is_not_reported_again()
    _check_counts_only_what_is_observed()
    _check_card_is_empty_when_clean()
    _check_new_kind_needs_no_code()
    _check_idle_leads_reach_the_builder()
    _check_card_shows_distinct_kinds()
    _check_soundness_routes_to_who_can_fix()
    _check_capability_report_is_heard()
    print("병목 인구조사 10개 영역 통과.")
    return 0


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="공장 병목 인구조사")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--card", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--top", type=int, default=3)
    a = ap.parse_args(argv)
    if a.check:
        return _selfcheck()

    items = census()
    if a.json:
        print(json.dumps([b.as_dict() for b in items], ensure_ascii=False, indent=2))
        return 0
    if a.card:
        print(card_body(items, top=a.top))
        return 0
    if not items:
        print("관측된 병목이 없다. (없다고 지어내지 않았다 - 원장에 낭비가 안 남았다)")
        return 0
    print(f"{'종류':16} {'낭비':>10}  사실")
    print("-" * 92)
    for b in items:
        print(f"{b.kind:16} {b.cost:>7.0f}{b.unit:3}  {b.evidence[:66]}")
    print(f"\n총 {len(items)}건. 카드 본문은 --card, 기계 판독은 --json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
