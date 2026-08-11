#!/usr/bin/env python3
"""공식 NAV 확정과 회계기간 잠금. **우리는 승인하지 않는다.**

소유: 도현 (회계·포트폴리오본부)
근거: docs/HEDGE_FUND_MASTER_PLAN.md 12.4 10번("독립 NAV Check 후 Official NAV 확정"),
      21.16(Preliminary/Reviewed/Official 상태 분리, "NAV 수정은 원본 덮어쓰기 금지,
      Version과 사유 기록"), 팀 가이드 "Official NAV 확정 금지"(회계 직원 권한)
      저장소: `accounting.nav_runs`(run_type/status/approval_id/input_hash)

세 가지가 서로 다른 일이다. 하나로 뭉치면 회계본부가 자기 숫자를 자기가 확정하게 된다.

  1. **계산**  `record_run()`      - Preliminary NAV 한 판을 증거와 함께 남긴다
  2. **검증**  `independent_check()` - 결정론 게이트. 사람도 LLM도 개입하지 않는다
  3. **승인**  `approve_official()` - **외부가 발급한 승인만 인용한다.** 여기서 만들지 않는다

`approve_official()`은 `governance.approvals`에 이미 있는 행을 찾아 인용할 뿐이고,
없으면 거부한다. 승인 행을 만드는 코드는 이 파일에 없다(만들면 자가 승인이다).

**기간 잠금은 승인의 결과다.** 공식 NAV가 승인된 날짜까지는 원장이 닫힌다
(`Ledger.closed_through` -> `PeriodClosedError`). 별도 잠금 테이블을 만들지 않는 이유가
그거다 - 잠금 상태가 두 군데 있으면 "승인했는데 안 잠긴" 기간이 생긴다.

자체 점검: python departments/05-accounting-portfolio/close/nav_close.py
           (DATABASE_URL 있으면 실 DB 왕복까지. 전부 트랜잭션 안에서 롤백한다)
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import UUID

import yaml

_HERE = Path(__file__).resolve().parent
for _p in (_HERE.parent / "ledger", _HERE.parent / "portfolio",
           _HERE.parent / "reconciliation", _HERE.parent / "treasury"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ledger import decimal_str  # noqa: E402
from portfolio import PortfolioSnapshot  # noqa: E402

OPS_PATH = _HERE.parent / "accounting_ops.yaml"

# NAV 계산 로직의 버전. **수치가 달라질 수 있는 변경이면 올린다** - 같은 입력에서
# 다른 NAV가 나왔는데 버전이 같으면 재현 검증이 거짓으로 통과한다.
CALCULATION_VERSION = "nav-close-v1"

# governance.approvals 를 찾을 때 쓰는 키. object_id 는 nav_run_id 다.
APPROVAL_OBJECT_TYPE = "accounting.nav_run"

ZERO = Decimal(0)


class NavCloseError(Exception):
    """공식 NAV를 확정할 수 없는 경우. 조용히 Preliminary를 공식이라고 부르지 않는다."""


@dataclass(frozen=True)
class CheckResult:
    """독립 검증 결과. **판정이지 승인이 아니다.**"""

    passed: bool
    blockers: tuple[str, ...]
    checked: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "blockers": list(self.blockers),
                "checked": list(self.checked), "decided_by": "deterministic"}


def _residual_tolerance(ops: Mapping[str, Any] | None = None) -> Decimal:
    """미설명 손익 허용치. 코드가 아니라 accounting_ops.yaml이 정한다."""
    if ops is None:
        doc = yaml.safe_load(OPS_PATH.read_text(encoding="utf-8")) or {}
        ops = doc.get("pnl_exception") or {}
    return Decimal(str(ops.get("residual_tolerance", 0)))


def input_hash(snapshot: PortfolioSnapshot, *, calculation_version: str = CALCULATION_VERSION) -> str:
    """이 NAV를 만든 입력의 지문.

    같은 입력에 같은 버전이면 같은 NAV가 나와야 한다. 이 값이 `accounting.nav_runs`에
    남으므로 나중에 재현 검증이 가능하다 - NAV만 저장하면 "그때 무엇으로 계산했는지"가
    사라져서 감사에서 아무것도 증명하지 못한다.

    **NAV 자체는 지문에 넣지 않는다.** 결과를 입력 지문에 섞으면 계산이 틀려도
    지문은 항상 일치한다.
    """
    payload = {
        "fund_id": str(snapshot.fund_id),
        "book_id": str(snapshot.book_id),
        "as_of": snapshot.as_of.isoformat(),
        "cash": decimal_str(snapshot.cash),
        "receivable": decimal_str(snapshot.receivable),
        "payable": decimal_str(snapshot.payable),
        "calculation_version": calculation_version,
        "positions": sorted(
            [
                {
                    "instrument_id": str(p.instrument_id),
                    "quantity": decimal_str(p.quantity),
                    "mark_price": decimal_str(p.mark_price),
                    "mark_as_of": p.mark_as_of.isoformat(),
                    "mark_is_final": p.mark_is_final,
                }
                for p in snapshot.positions
            ],
            key=lambda row: row["instrument_id"],
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def independent_check(
    snapshot: PortfolioSnapshot,
    *,
    report=None,
    open_breaks: Iterable[Any] = (),
    trial_balance_sum: Decimal = ZERO,
    overdue_settlements: int = 0,
    tolerance: Decimal | None = None,
) -> CheckResult:
    """마스터플랜 12.4 10번의 "독립 NAV Check". **전부 결정론이다.**

    통과 기준을 여기서 완화하지 않는다. 하나라도 걸리면 Preliminary로 남고, 그
    사실이 blockers에 남아 조사 대상이 된다 - 통과시키고 각주를 다는 것보다
    막고 이유를 남기는 쪽이 개발 원칙 9다.
    """
    tolerance = _residual_tolerance() if tolerance is None else tolerance
    blockers: list[str] = []
    checked = (
        "mark_quality", "trial_balance", "material_breaks",
        "unexplained_pnl", "settlement_overdue",
    )

    # 1. 미확정 봉으로 평가한 NAV는 공식이 될 수 없다(장중 체결가 포함).
    if snapshot.quality_status != "PASS":
        blockers.append(f"mark_quality={snapshot.quality_status} (확정 종가가 아니다)")

    # 2. 차대가 안 맞는 원장에서 나온 NAV는 숫자가 아니다.
    if trial_balance_sum != ZERO:
        blockers.append(f"trial_balance_sum={decimal_str(trial_balance_sum)}")

    # 3. Material Break은 "거래 사실이 안 맞는다"는 뜻이다. 그 위의 NAV는 확정 불가.
    material = [b for b in open_breaks if getattr(b, "escalates", False)]
    if material:
        blockers.append(f"material_breaks={len(material)}")

    # 4. 미설명 손익. 0으로 반올림하지 않는다(SOUL) - 허용치를 넘으면 막는다.
    if report is not None:
        residual = abs(report.unexplained_pnl)
        if residual > tolerance:
            blockers.append(
                f"unexplained_pnl={decimal_str(report.unexplained_pnl)} "
                f"(허용 {decimal_str(tolerance)})"
            )
    else:
        blockers.append("daily_report 없음 - 미설명 손익을 확인하지 못했다")

    # 5. 결제일이 지났는데 결제되지 않은 체결이 있으면 현금이 사실인지 알 수 없다.
    if overdue_settlements:
        blockers.append(f"settlement_overdue={overdue_settlements}")

    return CheckResult(passed=not blockers, blockers=tuple(blockers), checked=checked)


# ── 저장소 ────────────────────────────────────────────────────────────────────
# `accounting.nav_runs`는 fund 단위다(book이 아니다). NAV는 Fund의 순자산이고
# Book은 그 안의 칸이라, Book마다 공식 NAV를 따로 확정하면 합이 Fund NAV가 아니게 된다.


_CLOSED_THROUGH_SQL = """
    select max(valuation_date) from accounting.nav_runs
     where fund_id = %s and run_type = 'OFFICIAL' and status = 'APPROVED'
"""


def closed_through(repo, fund_id: UUID, *, cur=None) -> date | None:
    """공식 NAV가 승인된 마지막 회계일. 그날까지 원장이 잠긴다.

    승인이 없으면 None이고, 그때는 전 기간이 열려 있다 - 잠금은 승인의 결과이지
    기본값이 아니다.
    """
    if cur is not None:
        cur.execute(_CLOSED_THROUGH_SQL, (fund_id,))
        row = cur.fetchone()
        return row[0] if row else None
    with repo.cursor() as own:
        own.execute(_CLOSED_THROUGH_SQL, (fund_id,))
        row = own.fetchone()
    return row[0] if row else None


def record_run(
    repo,
    snapshot: PortfolioSnapshot,
    *,
    valuation_date: date,
    trace_id: UUID,
    run_type: str = "PRELIMINARY",
    calculation_version: str = CALCULATION_VERSION,
    currency: str = "KRW",
    cur=None,
) -> UUID:
    """NAV 한 판을 증거와 함께 남긴다. **확정이 아니다** - status는 CALCULATED다.

    같은 (fund, 날짜, 종류, 버전, 입력지문)이면 같은 행이다. 재실행이 행을 늘리지
    않는다 - 같은 입력에서 두 번 계산한 것은 두 번의 NAV가 아니다.
    """
    if run_type not in ("INTRADAY", "PRELIMINARY", "OFFICIAL"):
        raise NavCloseError(f"알 수 없는 run_type: {run_type}")
    digest = input_hash(snapshot, calculation_version=calculation_version)

    def _insert(c) -> UUID:
        c.execute(
            """
            insert into accounting.nav_runs (
                fund_id, valuation_date, as_of, run_type, status, total_nav,
                base_currency, input_hash, calculation_version, trace_id
            ) values (%s, %s, %s, %s, 'CALCULATED', %s, %s, %s, %s, %s)
            on conflict (fund_id, valuation_date, run_type, calculation_version, input_hash)
            do nothing
            returning nav_run_id
            """,
            (snapshot.fund_id, valuation_date, snapshot.as_of, run_type,
             snapshot.nav, currency, digest, calculation_version, trace_id),
        )
        row = c.fetchone()
        if row is not None:
            return row[0]
        # 같은 입력으로 이미 계산돼 있다. 그 행을 돌려준다(새 판을 만들지 않는다).
        c.execute(
            """
            select nav_run_id from accounting.nav_runs
             where fund_id = %s and valuation_date = %s and run_type = %s
               and calculation_version = %s and input_hash = %s
            """,
            (snapshot.fund_id, valuation_date, run_type, calculation_version, digest),
        )
        return c.fetchone()[0]

    if cur is not None:
        return _insert(cur)
    with repo.cursor() as own:
        return _insert(own)


def approve_official(
    repo,
    *,
    nav_run_id: UUID,
    approval_id: UUID,
    check: CheckResult,
    cur=None,
) -> dict[str, Any]:
    """외부 승인을 인용해 공식 NAV로 올린다. **승인을 만들지 않는다.**

    거부 조건 넷. 하나라도 걸리면 Preliminary로 남는다:
      1. 독립 검증을 통과하지 못했다
      2. `governance.approvals`에 그 승인이 없거나 APPROVED가 아니다
      3. 그 승인이 **이 NAV Run을 가리키지 않는다**(object_id 불일치)
      4. 승인이 만료됐다

    3번이 요지다. 다른 대상의 승인을 가져다 붙이면 승인 절차가 장식이 된다.
    """
    if not check.passed:
        raise NavCloseError(
            f"독립 검증을 통과하지 못했습니다: {', '.join(check.blockers)}"
        )

    def _promote(c) -> dict[str, Any]:
        c.execute(
            """
            select fund_id, valuation_date, run_type, status
              from accounting.nav_runs where nav_run_id = %s
            """,
            (nav_run_id,),
        )
        run = c.fetchone()
        if run is None:
            raise NavCloseError(f"없는 NAV Run입니다: {nav_run_id}")
        fund_id, valuation_date, run_type, status = run
        if status == "APPROVED" and run_type == "OFFICIAL":
            raise NavCloseError(f"이미 확정된 NAV입니다: {nav_run_id}")
        if status in ("REJECTED", "SUPERSEDED"):
            raise NavCloseError(f"{status} 상태의 NAV는 확정할 수 없습니다")

        c.execute(
            """
            select decision, object_type, object_id, expires_at, required_role
              from governance.approvals where approval_id = %s
            """,
            (approval_id,),
        )
        approval = c.fetchone()
        if approval is None:
            raise NavCloseError(
                f"승인 {approval_id}이 governance.approvals에 없습니다. "
                "회계본부는 승인을 만들지 않습니다"
            )
        decision, object_type, object_id, expires_at, required_role = approval
        if decision != "APPROVED":
            raise NavCloseError(f"승인 상태가 {decision}입니다")
        if object_type != APPROVAL_OBJECT_TYPE or object_id != nav_run_id:
            raise NavCloseError(
                f"승인 대상이 다릅니다: {object_type}/{object_id} "
                f"!= {APPROVAL_OBJECT_TYPE}/{nav_run_id}"
            )
        if expires_at is not None and expires_at <= datetime.now(timezone.utc):
            raise NavCloseError(f"만료된 승인입니다 (expires_at={expires_at})")

        # 같은 날짜의 이전 공식 NAV는 덮어쓰지 않고 SUPERSEDED로 남긴다(21.16).
        c.execute(
            """
            update accounting.nav_runs set status = 'SUPERSEDED'
             where fund_id = %s and valuation_date = %s and run_type = 'OFFICIAL'
               and status = 'APPROVED' and nav_run_id <> %s
            """,
            (fund_id, valuation_date, nav_run_id),
        )
        superseded = c.rowcount
        c.execute(
            """
            update accounting.nav_runs
               set run_type = 'OFFICIAL', status = 'APPROVED', approval_id = %s
             where nav_run_id = %s
            """,
            (approval_id, nav_run_id),
        )
        return {
            "nav_run_id": str(nav_run_id),
            "fund_id": str(fund_id),
            "valuation_date": valuation_date.isoformat(),
            "approval_id": str(approval_id),
            "required_role": required_role,
            "superseded": superseded,
            "closed_through": valuation_date.isoformat(),
            "is_official": True,
            "decided_by": "governance.approvals",
        }

    if cur is not None:
        return _promote(cur)
    with repo.cursor() as own:
        return _promote(own)


if __name__ == "__main__":
    import os
    from types import SimpleNamespace
    from uuid import uuid4

    try:
        from dotenv import load_dotenv
        load_dotenv(Path.cwd() / ".env")
    except ModuleNotFoundError:
        pass

    sys.path.insert(0, str(_HERE.parent.parent / "02-trading" / "contracts"))
    from ledger import Ledger, PeriodClosedError, Position  # noqa: E402
    from portfolio import MarkPrice, PositionValuation, value_portfolio  # noqa: E402
    from reconciliation import Break, Severity  # noqa: E402

    D = Decimal
    ok = lambda label: print(f"  {label:28} OK")  # noqa: E731
    now = datetime.now(timezone.utc)
    fund, book, inst = uuid4(), uuid4(), uuid4()

    def snap(*, quality_final=True, nav_cash=D("1000")) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            fund_id=fund, book_id=book, as_of=now, cash=nav_cash,
            receivable=ZERO, payable=ZERO, realized_pnl=ZERO, fees=ZERO, taxes=ZERO,
            positions=(PositionValuation(
                instrument_id=inst, quantity=D("10"), average_cost=D("100"),
                mark_price=D("110"), mark_as_of=now, mark_is_final=quality_final),),
        )

    def raises(fn, why, exc=NavCloseError):
        try:
            fn()
        except exc:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    report_ok = SimpleNamespace(unexplained_pnl=ZERO)
    report_bad = SimpleNamespace(unexplained_pnl=D("500"))

    # 1. 입력 지문은 입력이 같으면 같고, 하나만 달라도 다르다
    assert input_hash(snap()) == input_hash(snap())
    assert input_hash(snap()) != input_hash(snap(nav_cash=D("1001")))
    assert input_hash(snap()) != input_hash(snap(), calculation_version="nav-close-v2")
    ok("입력 지문 재현")

    # 2. 독립 검증 - 전부 통과해야 passed
    good = independent_check(snap(), report=report_ok)
    assert good.passed and good.blockers == (), good
    ok("독립 검증 통과")

    # 3. 게이트 하나하나가 실제로 막는다
    assert not independent_check(snap(quality_final=False), report=report_ok).passed
    assert not independent_check(snap(), report=report_bad).passed
    assert not independent_check(snap(), report=None).passed
    assert not independent_check(snap(), report=report_ok,
                                 trial_balance_sum=D("1")).passed
    assert not independent_check(snap(), report=report_ok,
                                 overdue_settlements=1).passed
    material = Break(break_id=uuid4(), kind="internal_only_fill",
                     severity=Severity.MATERIAL, detail="내부에만 있는 체결")
    blocked = independent_check(snap(), report=report_ok, open_breaks=[material])
    assert not blocked.passed and "material_breaks=1" in blocked.blockers[0]
    ok("게이트 5종 fail-closed")

    # 4. 허용치 안의 잔차는 막지 않는다 (0으로 반올림하는 것과 다르다)
    small = SimpleNamespace(unexplained_pnl=D("0.5"))
    assert independent_check(snap(), report=small, tolerance=D("1")).passed
    assert not independent_check(snap(), report=small, tolerance=D("0.1")).passed
    ok("잔차 허용치")

    # 5. 검증을 통과하지 못하면 승인을 인용조차 하지 않는다
    raises(lambda: approve_official(None, nav_run_id=uuid4(), approval_id=uuid4(),
                                    check=blocked, cur=object()),
           "검증 실패 상태에서 확정")
    ok("검증 실패 = 확정 불가")

    # 6. 기간 잠금은 원장이 집행한다 - 승인된 날짜 이하로는 분개 거부
    led = Ledger(fund_id=fund, book_id=book, closed_through=date(2026, 8, 10))
    raises(lambda: led.post_capital(D("100"), datetime(2026, 8, 10, 1, tzinfo=timezone.utc),
                                    "late"), "마감 기간 분개", PeriodClosedError)
    fresh = led.post_capital(D("100"), datetime(2026, 8, 11, 1, tzinfo=timezone.utc), "today")
    assert fresh.accounting_date == date(2026, 8, 11)
    ok("마감 기간 분개 차단")

    # 7. 마감된 기간의 분개는 역분개도 당기로 간다(원본 날짜로 되돌아가지 않는다)
    open_led = Ledger(fund_id=fund, book_id=book)
    old = open_led.post_capital(D("100"), datetime(2026, 8, 9, 1, tzinfo=timezone.utc), "old")
    open_led.closed_through = date(2026, 8, 10)
    rev = open_led.reverse(old.journal_id, "마감 후 정정")
    assert rev.accounting_date > date(2026, 8, 10), rev.accounting_date
    assert old.accounting_date == date(2026, 8, 9), "원본 회계일이 바뀌었다"
    ok("역분개는 당기로")

    # 8. 실 DB 왕복 - 전부 롤백한다
    sys.path.insert(0, str(_HERE.parent / "ledger"))
    from repository import LedgerRepository  # noqa: E402

    repo = LedgerRepository.from_env()
    if repo is None or not os.environ.get("DATABASE_URL"):
        print("  실 DB 왕복                   skip - DATABASE_URL 없음")
    else:
        real_fund, real_book = repo.bootstrap("ACC01-PAPER", "MAIN")
        with repo.cursor() as cur:
            db_snap = PortfolioSnapshot(
                fund_id=real_fund, book_id=real_book, as_of=now, cash=D("1000"),
                receivable=ZERO, payable=ZERO, realized_pnl=ZERO, fees=ZERO, taxes=ZERO,
            )
            vd = date(2026, 1, 2)
            run_id = record_run(repo, db_snap, valuation_date=vd, trace_id=uuid4(), cur=cur)
            again = record_run(repo, db_snap, valuation_date=vd, trace_id=uuid4(), cur=cur)
            assert run_id == again, "같은 입력이 NAV Run을 두 개 만들었다"

            # 승인 없이는 확정되지 않는다
            passed = independent_check(db_snap, report=report_ok)
            raises(lambda: approve_official(repo, nav_run_id=run_id,
                                            approval_id=uuid4(), check=passed, cur=cur),
                   "없는 승인으로 확정")

            # 다른 대상의 승인은 가져다 쓸 수 없다
            cur.execute(
                "insert into governance.approvals (fund_id, object_type, object_id, "
                "required_role, decision, decided_at) values (%s, %s, %s, %s, "
                "'APPROVED', now()) returning approval_id",
                (real_fund, APPROVAL_OBJECT_TYPE, uuid4(), "fund_administrator"))
            other = cur.fetchone()[0]
            raises(lambda: approve_official(repo, nav_run_id=run_id, approval_id=other,
                                            check=passed, cur=cur),
                   "다른 대상의 승인 전용")

            # 제 대상 승인이면 확정되고, 그 날짜까지 기간이 잠긴다
            cur.execute(
                "insert into governance.approvals (fund_id, object_type, object_id, "
                "required_role, decision, decided_at) values (%s, %s, %s, %s, "
                "'APPROVED', now()) returning approval_id",
                (real_fund, APPROVAL_OBJECT_TYPE, run_id, "fund_administrator"))
            mine = cur.fetchone()[0]
            result = approve_official(repo, nav_run_id=run_id, approval_id=mine,
                                      check=passed, cur=cur)
            assert result["is_official"] is True and result["superseded"] == 0
            cur.execute("select max(valuation_date) from accounting.nav_runs where "
                        "fund_id = %s and run_type = 'OFFICIAL' and status = 'APPROVED'",
                        (real_fund,))
            assert cur.fetchone()[0] == vd, "잠금 날짜가 승인 날짜와 다르다"

            # 두 번 확정하지 않는다
            raises(lambda: approve_official(repo, nav_run_id=run_id, approval_id=mine,
                                            check=passed, cur=cur), "이중 확정")

            # 정정은 원본을 덮어쓰지 않는다(21.16) - 새 Run이 서고 옛 Run이 SUPERSEDED다
            corrected = PortfolioSnapshot(
                fund_id=real_fund, book_id=real_book, as_of=now, cash=D("1100"),
                receivable=ZERO, payable=ZERO, realized_pnl=ZERO, fees=ZERO, taxes=ZERO)
            fix_id = record_run(repo, corrected, valuation_date=vd,
                                trace_id=uuid4(), cur=cur)
            assert fix_id != run_id, "정정이 원본 Run을 덮어썼다"
            cur.execute(
                "insert into governance.approvals (fund_id, object_type, object_id, "
                "required_role, decision, decided_at) values (%s, %s, %s, %s, "
                "'APPROVED', now()) returning approval_id",
                (real_fund, APPROVAL_OBJECT_TYPE, fix_id, "fund_administrator"))
            fixed = approve_official(
                repo, nav_run_id=fix_id, approval_id=cur.fetchone()[0],
                check=independent_check(corrected, report=report_ok), cur=cur)
            assert fixed["superseded"] == 1, fixed
            cur.execute("select status, total_nav from accounting.nav_runs "
                        "where nav_run_id = %s", (run_id,))
            old_status, old_nav = cur.fetchone()
            assert old_status == "SUPERSEDED", "이전 공식 NAV가 그대로 남았다"
            assert Decimal(str(old_nav)) == db_snap.nav, "원본 NAV 수치가 덮어써졌다"
            # 공용 DB에 공식 NAV와 승인을 남기지 않는다. 예외로 되감으면 그 예외가
            # 자체 점검 밖으로 새어나가므로(실제로 KeyboardInterrupt가 그랬다)
            # 커서를 닫기 전에 직접 되감는다.
            cur.connection.rollback()
        print("  실 DB 왕복                   OK")

    print("ok - NAV Close 8개 영역 점검 통과 "
          "(계산·검증·승인 분리, 승인은 외부 발급만 인용)")
