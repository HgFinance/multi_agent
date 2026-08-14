#!/usr/bin/env python3
"""데이터 준비도 진단 - **공장이 자기 바닥을 스스로 본다.**

소유: 재일 (퀀트·백테스트본부 QNT)
근거: 2026-08-14 "실제 공장이 돌아가려면 스스로 전부 설계하고 돌아가야 한다"

▶ 왜 이 파일이 있어야 하나
  공장은 가설을 내고 실험을 돌리고 판정까지 스스로 한다. 그런데 **자기가
  딛고 선 데이터가 성한지는 아무도 안 본다.** 2026-08-14 하루에 사람이
  손으로 파서 나온 것들이다:

    · 유니버스에 상장폐지가 10.6년간 3.57%(기대 10.6%) - 생존편향
    · `reference.corporate_actions` 205행이 전부 `ratio`/`ex_date` NULL
      (DART 공시 원문만 있고 조정계수가 없다)
    · 주 데이터셋이 이틀간 안 굳혀짐 - 매니페스트는 여전히 "있다" 고 말함
    · `feature_specs` 1행 · `feature_snapshots` 0행 - 어휘가 코드에 8개로 잠김

  넷 다 **원장을 한 줄 세면 나오는 것**인데 아무도 안 셌다. 못 보는 것은
  설계도 못 한다 - 능동적 자동화의 전제는 자기 진단이다.

▶ 판정과 측정을 가른다
  측정은 DB 가 하고 **판정은 순수 함수가 한다.** 그래야 DB 없이 자체 점검이
  돌고, 임계를 바꿀 때 무엇이 달라지는지 시험으로 고정할 수 있다.

▶ 등급 세 개만 쓴다
  BLOCKS   : 이걸 두고 실험하면 **결과가 증거가 아니다**
  DEGRADES : 돌긴 도는데 값을 깎는다
  OK       : 지금은 문제 없다

  "경고" 같은 중간 등급을 두지 않는다 - 경고는 아무도 안 본다.

자체 점검: python departments/04-quant-backtest/pipeline/data_readiness.py
실행     : python departments/04-quant-backtest/pipeline/data_readiness.py --run
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

MODULE_VERSION = "quant-data-readiness-v1"

BLOCKS, DEGRADES, OK = "BLOCKS", "DEGRADES", "OK"

# 연간 기대 폐지율. `dataset_refinery` 와 같은 값을 쓴다 - 두 곳이 다르면
# 정제소는 통과시키고 진단은 막는 상태가 된다.
DELISTING_PER_YEAR = 0.010
STALE_DAYS_LIMIT = 1        # 하루 한 번 굳히므로 이틀 밀리면 밀린 것이다


@dataclass(frozen=True)
class Finding:
    name: str
    verdict: str
    evidence: str
    blocks: str      # 무엇을 못 하게 하는가
    action: str      # 무엇을 하면 풀리는가

    @property
    def bad(self) -> bool:
        return self.verdict != OK


# ── 판정 (순수 함수) ────────────────────────────────────────────────────────


def judge_survivorship(*, symbols: int, vanished: int, years: float) -> Finding:
    """**사라진 종목이 기대의 절반도 안 되면 표본이 살아남은 것만 담고 있다.**"""
    expected = DELISTING_PER_YEAR * max(years, 0.25)
    ratio = (vanished / symbols) if symbols else 0.0
    if symbols and ratio < expected * 0.5:
        return Finding(
            "유니버스 생존편향", BLOCKS,
            f"{years:.1f}년 동안 사라진 종목 {vanished}/{symbols}({ratio:.2%}) "
            f"· 기대 {expected:.1%}",
            "낙폭·저가주 전략의 성적 전부. 표본에 파산이 없으므로 낙폭이 "
            "실제보다 얕게 나온다",
            "폐지 종목 이력을 유니버스에 채운다(universe_versions.as_of 사용)")
    return Finding("유니버스 생존편향", OK,
                   f"{years:.1f}년 폐지 {vanished}/{symbols}({ratio:.2%})", "", "")


def judge_corporate_actions(*, rows: int, with_ratio: int,
                            with_ex_date: int) -> Finding:
    """**조정계수 없는 자본변동 표는 가격을 못 고친다.**"""
    if rows and (with_ratio == 0 or with_ex_date == 0):
        return Finding(
            "자본변동 조정계수", BLOCKS,
            f"{rows}행 중 ratio {with_ratio}행 · ex_date {with_ex_date}행 "
            f"(공시 원문만 있고 조정계수가 없다)",
            "수정주가 적용. 지금은 미조정 갭 앞을 잘라내고 있어 그만큼 "
            "종목이 조용히 빠진다",
            "상장주식수 시계열이나 수정주가를 원천에서 받아 ratio/ex_date 를 채운다")
    if not rows:
        return Finding("자본변동 조정계수", BLOCKS, "표가 비어 있다",
                       "수정주가 적용", "자본변동 수집을 켠다")
    return Finding("자본변동 조정계수", OK,
                   f"{rows}행 · ratio {with_ratio}행", "", "")


def judge_dataset_freshness(*, name: str, covers_through: str,
                            upstream_through: str, lag_days: int) -> Finding:
    """**매니페스트는 "있다" 고만 말한다.** 낡았다는 사실은 세야 나온다."""
    if lag_days > STALE_DAYS_LIMIT:
        return Finding(
            f"데이터셋 신선도({name})", DEGRADES,
            f"덮는 구간 {covers_through} · 상류 {upstream_through} "
            f"({lag_days}일 밀림)",
            "최근 구간 실험. 어제 시세로 오늘 가설을 검증한다",
            "굳히기를 시계가 아니라 조건으로 돌린다(상류가 앞서면 굳힌다)")
    return Finding(f"데이터셋 신선도({name})", OK,
                   f"{covers_through} (밀림 {lag_days}일)", "", "")


def judge_feature_registry(*, specs: int, snapshots: int,
                           hardcoded: int) -> Finding:
    """**피처가 코드에 있으면 리서치가 어휘를 못 늘린다.**"""
    if specs <= 1 or snapshots == 0:
        return Finding(
            "피처 레지스트리", BLOCKS,
            f"feature_specs {specs}행 · feature_snapshots {snapshots}행 · "
            f"코드에 박힌 템플릿 {hardcoded}종",
            "리서치가 코드 수정 없이 새 피처를 내는 것. 독창성·복잡도 "
            "정규화(AlphaAgent)도 AST 가 없으면 계산 불가",
            "피처 정의를 코드에서 feature_specs 로 옮긴다")
    return Finding("피처 레지스트리", OK,
                   f"specs {specs} · snapshots {snapshots}", "", "")


def judge_unit_declaration(*, declared: bool, sample_note: str = "") -> Finding:
    """**단위가 데이터에 안 붙어 있으면 같은 사고가 또 난다.**"""
    if not declared:
        return Finding(
            "단위 선언", DEGRADES,
            f"데이터셋 매니페스트에 금액·수량 단위 필드가 없다{sample_note}",
            "원천이 바뀌면 조용히 배수가 틀어진다(2026-08-14 notional "
            "백만원↔원 1e6배로 유니버스가 통째로 빔)",
            "매니페스트에 notional_unit/volume_unit 을 넣고 로더 경계에서 환산한다")
    return Finding("단위 선언", OK, "매니페스트가 단위를 선언한다", "", "")


def judge_contract_adoption(*, experiments: int, with_contract: int) -> Finding:
    """**계약을 안 쓰는 실험은 무엇을 걸렀는지 원장이 모른다.**"""
    if experiments and with_contract == 0:
        return Finding(
            "계약 채택", DEGRADES,
            f"실험 {experiments}건 중 계약 지문이 달린 것 {with_contract}건",
            "'이 알파는 정확히 어떤 데이터로 나왔나' 에 답하는 것",
            "정제소를 오케스트레이터에 배선하고 계약 지문을 input_hash 계보에 넣는다")
    return Finding("계약 채택", OK,
                   f"{with_contract}/{experiments} 실험이 계약을 쓴다", "", "")


def judge_serving_parity(*, offline_impls: int, online_impls: int,
                         shared_definition: bool) -> Finding:
    """**모의투자가 백테스트를 재현하려면 정의가 하나여야 한다.**

    ▶ 무엇이 문제인가 (training-serving skew)
      지금 피처는 `strategy_templates` 안에서 **백테스트 실행 중에** 계산된다.
      모의투자를 붙이면 실시간 쪽에 같은 계산을 **다시 구현**해야 하고,
      구현이 둘이면 값이 갈린다. 그러면 성적이 달라졌을 때
      **알파가 죽은 것인지 피처가 다른 것인지 구분할 수 없다** - 그 상태의
      모의투자는 검증이 아니라 소음이다.

      피처 스토어가 존재하는 이유가 정확히 이것이다: 정의는 하나, 실체화는
      둘(과거 배치 / 실시간). `feature_specs.availability_lag` 가 그 접점이다 -
      "이 값이 실제로 손에 들어오는 시점" 을 정의가 들고 있어야 양쪽이 같은
      시점을 본다.
    """
    if offline_impls and online_impls and not shared_definition:
        return Finding(
            "온·오프라인 정합", BLOCKS,
            f"오프라인 구현 {offline_impls}곳 · 실시간 구현 {online_impls}곳 · "
            f"공유 정의 없음",
            "모의투자가 백테스트를 재현하는 것. 성적이 갈려도 알파 문제인지 "
            "구현 문제인지 못 가른다",
            "피처 정의를 feature_specs 하나로 두고 배치·실시간이 그것을 읽게 한다")
    if not online_impls:
        return Finding(
            "온·오프라인 정합", DEGRADES,
            f"실시간 피처 실체화가 없다(오프라인 {offline_impls}곳) - "
            f"모의투자를 붙이면 두 번째 구현이 생긴다",
            "모의투자 준비",
            "실시간 경로를 만들기 **전에** 정의를 레지스트리로 옮긴다 - "
            "나중에 옮기면 구현이 둘이 된 뒤라 이미 늦다")
    return Finding("온·오프라인 정합", OK,
                   f"정의 공유 · 오프라인 {offline_impls} · 실시간 {online_impls}",
                   "", "")


def summarize(findings: list[Finding]) -> dict:
    """**막는 것이 하나라도 있으면 준비 안 된 것이다.**"""
    blocks = [f for f in findings if f.verdict == BLOCKS]
    degrades = [f for f in findings if f.verdict == DEGRADES]
    return {"ready": not blocks,
            "blocks": len(blocks), "degrades": len(degrades),
            "findings": findings}


# ── 측정 (DB) ────────────────────────────────────────────────────────────────


def _measure(conn_sup, conn_ts) -> list[Finding]:  # pragma: no cover - DB 경로
    out: list[Finding] = []
    c = conn_sup.cursor()

    def one(sql, default=0):
        try:
            c.execute(sql)
            r = c.fetchone()
            return r[0] if r and r[0] is not None else default
        except Exception:  # noqa: BLE001
            conn_sup.rollback()
            return default

    rows = one("select count(*) from reference.corporate_actions")
    out.append(judge_corporate_actions(
        rows=rows,
        with_ratio=one("select count(*) from reference.corporate_actions "
                       "where ratio is not null"),
        with_ex_date=one("select count(*) from reference.corporate_actions "
                         "where ex_date is not null")))

    out.append(judge_feature_registry(
        specs=one("select count(*) from quant.feature_specs"),
        snapshots=one("select count(*) from quant.feature_snapshots"),
        hardcoded=_hardcoded_templates()))

    out.append(judge_contract_adoption(
        experiments=one("select count(*) from quant.experiments"),
        with_contract=one("select count(*) from quant.experiments "
                          "where config ? 'contract_fingerprint'")))

    out.append(judge_serving_parity(
        offline_impls=1 if _hardcoded_templates() > 0 else 0,
        online_impls=0,          # 실시간 피처 실체화 경로가 아직 없다
        shared_definition=one("select count(*) from quant.feature_specs") > 1))

    out.append(judge_unit_declaration(declared=bool(one(
        "select count(*) from information_schema.columns "
        "where table_schema='quant' and table_name='dataset_manifests' "
        "and column_name in ('notional_unit','volume_unit')"))))
    return out


def _hardcoded_templates() -> int:
    try:
        from strategy_templates import TEMPLATES  # noqa: PLC0415
        return len(TEMPLATES)
    except Exception:  # noqa: BLE001
        return -1


def _run() -> int:  # pragma: no cover - DB 경로
    import os

    import psycopg2

    sup = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=20)
    try:
        ts = psycopg2.connect(os.environ.get("TIMESCALE_DATABASE_URL", ""),
                              connect_timeout=10)
    except Exception:  # noqa: BLE001
        ts = None
    findings = _measure(sup, ts)
    s = summarize(findings)
    print(f"{MODULE_VERSION} 데이터 준비도")
    print(f"  준비됨: {s['ready']} | 막는 것 {s['blocks']} · 깎는 것 {s['degrades']}")
    print()
    for f in findings:
        mark = {"BLOCKS": "■", "DEGRADES": "▲", "OK": "·"}[f.verdict]
        print(f"{mark} [{f.verdict:8s}] {f.name}")
        print(f"     근거: {f.evidence}")
        if f.bad:
            print(f"     막는 것: {f.blocks}")
            print(f"     처방: {f.action}")
        print()
    return 0 if s["ready"] else 1


# ── 자체 점검 ────────────────────────────────────────────────────────────────


def _check_survivorship_scales_with_span():
    """1년 창의 1% 폐지는 정상, 10년 창의 1% 는 아니다."""
    assert judge_survivorship(symbols=1000, vanished=10, years=1.0).verdict == OK
    assert judge_survivorship(symbols=1000, vanished=10, years=10.6).verdict == BLOCKS
    # 실측값: 3924종목 · 140 폐지 · 10.6년 -> 막아야 한다
    f = judge_survivorship(symbols=3924, vanished=140, years=10.6)
    assert f.verdict == BLOCKS and "3.57%" in f.evidence, f


def _check_corporate_actions_without_ratio_blocks():
    """**행이 있어도 조정계수가 없으면 못 고친다** (2026-08-14 실측 205행)."""
    f = judge_corporate_actions(rows=205, with_ratio=0, with_ex_date=0)
    assert f.verdict == BLOCKS and "205행" in f.evidence, f
    assert judge_corporate_actions(rows=205, with_ratio=205,
                                   with_ex_date=205).verdict == OK
    # 빈 표도 막는다 - "없음" 과 "못 씀" 을 같은 등급으로 둔다
    assert judge_corporate_actions(rows=0, with_ratio=0,
                                   with_ex_date=0).verdict == BLOCKS


def _check_staleness_uses_lag_not_presence():
    """매니페스트가 '있다' 고 해도 낡았으면 낡은 것이다."""
    assert judge_dataset_freshness(name="d", covers_through="2026-08-10",
                                   upstream_through="2026-08-14",
                                   lag_days=2).verdict == DEGRADES
    assert judge_dataset_freshness(name="d", covers_through="2026-08-14",
                                   upstream_through="2026-08-14",
                                   lag_days=0).verdict == OK


def _check_feature_registry_blocks_when_empty():
    """**스모크 1행은 채워진 게 아니다.**"""
    assert judge_feature_registry(specs=1, snapshots=0, hardcoded=8).verdict == BLOCKS
    assert judge_feature_registry(specs=40, snapshots=0, hardcoded=8).verdict == BLOCKS
    assert judge_feature_registry(specs=40, snapshots=12000,
                                  hardcoded=8).verdict == OK


def _check_blocks_beat_degrades_in_summary():
    """**막는 것이 하나라도 있으면 준비 안 된 것이다.**"""
    s = summarize([judge_survivorship(symbols=1000, vanished=200, years=1.0),
                   judge_unit_declaration(declared=False)])
    assert s["ready"] and s["degrades"] == 1, s      # DEGRADES 만 있으면 준비됨
    s2 = summarize([judge_corporate_actions(rows=205, with_ratio=0,
                                            with_ex_date=0)])
    assert not s2["ready"] and s2["blocks"] == 1, s2


def _check_serving_parity_warns_before_the_second_impl_exists():
    """**두 번째 구현이 생기기 전에 말해야 한다** - 생긴 뒤엔 이미 늦다."""
    f = judge_serving_parity(offline_impls=1, online_impls=0,
                             shared_definition=False)
    assert f.verdict == DEGRADES and "전에" in f.action, f

    # 구현이 둘인데 정의가 안 겹치면 막는다
    g = judge_serving_parity(offline_impls=1, online_impls=1,
                             shared_definition=False)
    assert g.verdict == BLOCKS and "재현" in g.blocks, g

    # 정의를 공유하면 통과
    h = judge_serving_parity(offline_impls=1, online_impls=1,
                             shared_definition=True)
    assert h.verdict == OK, h


def _check_no_middle_grade_exists():
    """**경고 등급을 두지 않는다** - 경고는 아무도 안 본다."""
    for fn, kw in ((judge_survivorship, dict(symbols=1, vanished=0, years=1.0)),
                   (judge_corporate_actions,
                    dict(rows=1, with_ratio=1, with_ex_date=1)),
                   (judge_unit_declaration, dict(declared=True))):
        assert fn(**kw).verdict in (BLOCKS, DEGRADES, OK)


def _check_ok_findings_carry_no_prescription():
    """정상인데 처방이 붙으면 목록이 잡음이 된다."""
    f = judge_unit_declaration(declared=True)
    assert f.verdict == OK and not f.blocks and not f.action, f


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        raise SystemExit(_run())

    print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
    _check_survivorship_scales_with_span();   print("  생존편향 임계 비례      OK")
    _check_corporate_actions_without_ratio_blocks()
    print("  조정계수 없으면 막음    OK")
    _check_staleness_uses_lag_not_presence(); print("  신선도는 밀림으로 봄    OK")
    _check_feature_registry_blocks_when_empty()
    print("  빈 레지스트리는 막음    OK")
    _check_blocks_beat_degrades_in_summary(); print("  BLOCKS 가 준비도를 정함  OK")
    _check_serving_parity_warns_before_the_second_impl_exists()
    print("  정합은 구현 전에 경고   OK")
    _check_no_middle_grade_exists();          print("  중간 등급 없음          OK")
    _check_ok_findings_carry_no_prescription()
    print("  정상엔 처방 안 붙임     OK")
    print("데이터 준비도 진단 8개 영역 통과.")
