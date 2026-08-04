#!/usr/bin/env python3
"""대사 결과와 Break를 Supabase `accounting.*`에 남긴다.

소유: 도현 (회계·포트폴리오본부)
근거: docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 4.5, 8.4, 11(DoD 7번)
      docs/HEDGE_FUND_MASTER_PLAN.md 11.2, 12.4
      supabase/migrations/20260729000400_execution_risk_accounting.sql

대사는 "맞다"를 증명하는 작업이 아니라 "다르다"를 숨기지 않는 작업이다. 그래서
Break는 **응답에만 있으면 안 된다** - 프로세스가 죽으면 사라지는 불일치는 없었던
것과 같다. 여기서 canonical 4단 사슬에 남긴다:

    external_statements -> reconciliations -> reconciliation_items -> breaks

**여기서 하지 않는 것:**
  - Break를 종결하지 않는다. `status`는 항상 OPEN으로 만들고 RESOLVED/WAIVED/CLOSED로
    옮기는 경로가 이 파일에 없다. 종결 권한은 AI QA/감사본부다(CLAUDE.md).
  - 판정하지 않는다. severity·kind는 `reconciliation.py`가 정한 값을 옮길 뿐이다.
  - 이벤트를 전송하지 않는다. 리스크·QA는 지금 이 표를 읽는다. Redis 전송로는
    PLAT-02 대기이며, 붙으면 이 함수 뒤에 publisher 한 겹을 더한다.

**외부 명세서 원문은 저장하지 않는다.** `object_path` + `content_hash`만 남긴다
(규약: Event Payload에 전체 Statement를 넣지 않는다). 원문 보관은 Storage 몫이다.

파일 이름이 `repository.py`가 아닌 이유: `ledger/repository.py`와 같은 모듈 이름이
되어 sys.path 순서에 따라 하나가 다른 하나를 가린다. 부서 폴더들이 아직 패키지가
아니라(`__init__.py` 없음) 모듈 이름이 전역이다.

자체 점검: python departments/05-accounting-portfolio/reconciliation/recon_repository.py
           (DATABASE_URL 필요 - 실 DB 왕복 검사다)
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "ledger"))

from reconciliation import (
    Break,
    MatchMethod,
    ReconItem,
    ReconResult,
    Severity,
    reconcile_cash,
    reconcile_positions,
)
from repository import LedgerPersistenceError, LedgerRepository, _load_driver

# 도메인 Severity -> DB check 제약. DB에는 MATERIAL이 없고 CRITICAL이 최상위다.
_DB_SEVERITY = {
    Severity.LOW: "LOW", Severity.MEDIUM: "MEDIUM",
    Severity.HIGH: "HIGH", Severity.MATERIAL: "CRITICAL",
}

# 대사 결과 -> `accounting.reconciliations.status`
_DB_RUN_STATUS = {"matched": "MATCHED", "partial": "BREAKS_FOUND", "break": "BREAKS_FOUND"}


@dataclass(frozen=True)
class ReconRun:
    """저장된 대사 한 건."""

    reconciliation_id: UUID
    statement_id: UUID
    break_ids: tuple[UUID, ...]
    created: bool  # False면 같은 입력의 기존 대사를 재사용했다


def _json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    return value


def _hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(_json_safe(payload), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _item_status(item: ReconItem) -> str:
    """`accounting.reconciliation_items.status` 네 값에 대응시킨다.

    TOLERANCE는 "짝은 맞고 차이는 있는데 Break를 내지 않은 것"이다 - 현금 대사의
    반올림 허용 오차가 여기 들어간다. 이걸 MATCHED로 뭉개면 허용 오차가 실제로
    얼마였는지 나중에 못 본다.
    """
    if item.is_confirmed:
        return "MATCHED"
    if item.match_method is MatchMethod.UNMATCHED:
        return "UNMATCHED"
    if item.match_method is MatchMethod.FUZZY_CANDIDATE or item.has_discrepancy:
        return "REVIEW"
    return "TOLERANCE"


def _item_for_break(result: ReconResult, brk: Break) -> int:
    """Break가 어느 항목에서 나왔는지 찾는다. `breaks.reconciliation_item_id`가 not null이다.

    참조로 정확히 짚고, 못 짚으면 **추측하지 않고 예외를 낸다.** 엉뚱한 항목에 붙은
    증거는 없느니만 못하다. 지금 도메인이 만드는 Break는 전부 참조를 갖거나(체결·포지션)
    항목이 하나뿐이다(현금).
    """
    for index, item in enumerate(result.items):
        if item.internal_ref == brk.internal_ref and item.external_ref == brk.external_ref:
            return index
    for index, item in enumerate(result.items):
        if brk.internal_ref is not None and item.internal_ref == brk.internal_ref:
            return index
        if brk.external_ref is not None and item.external_ref == brk.external_ref:
            return index
    if len(result.items) == 1:
        return 0
    raise LedgerPersistenceError(
        f"Break {brk.kind}를 대사 항목에 연결할 수 없습니다 "
        f"(internal_ref={brk.internal_ref}, external_ref={brk.external_ref})"
    )


def save_reconciliation(
    repo: LedgerRepository,
    fund_id: UUID,
    result: ReconResult,
    *,
    provider: str,
    account_ref: str,
    external_payload: Any,
    internal_payload: Any = None,
    object_path: str | None = None,
    trace_id: UUID | None = None,
    started_at: datetime | None = None,
) -> ReconRun:
    """대사 한 건을 canonical 표에 기록한다.

    **같은 입력이면 같은 대사다.** (외부 명세서, 내부 상태, 규칙 버전)이 모두 같으면
    기존 행을 돌려주고 Break를 다시 만들지 않는다 - 대사를 두 번 돌렸다고 불일치가
    두 배가 되지는 않는다. 내부 상태가 바뀌면 input_hash가 달라져 새 대사가 된다.

    저장된 Break의 id를 도메인 객체에 되돌려 쓴다(`brk.break_id`). 호출자가 응답에
    싣는 id와 DB에 남은 id가 갈라지면 화면에서 본 Break를 DB에서 못 찾는다.
    """
    _, Json, _ = _load_driver()
    # started_at은 **대사를 돌린 시각**이지 대사 대상 시점이 아니다. as_of를 넣으면
    # 과거·미래 시점을 대사할 때 completed_at >= started_at 제약에 걸린다.
    # 대상 시점은 internal_snapshot_ref.as_of와 statement_date에 남는다.
    started_at = started_at or datetime.now(timezone.utc)
    external_hash = _hash(external_payload)
    internal_ref_payload = internal_payload if internal_payload is not None else [
        {"ref": i.internal_ref, "value": i.internal_value} for i in result.items
    ]
    input_hash = _hash({
        "recon_type": result.recon_type, "rule_version": result.rule_version,
        "external": external_hash, "internal": _hash(internal_ref_payload),
    })
    trace_id = trace_id or uuid5(NAMESPACE_URL, f"recon:{input_hash}")

    with repo.cursor() as cur:
        # 1. 외부 명세서. 원문이 아니라 포인터와 해시만 남긴다.
        cur.execute(
            """
            insert into accounting.external_statements
                (fund_id, provider, account_ref, statement_date, statement_type,
                 object_path, content_hash, status)
            values (%s, %s, %s, %s, %s, %s, %s, 'PARSED')
            on conflict (provider, account_ref, statement_date, statement_type, content_hash)
            do nothing
            returning statement_id
            """,
            (fund_id, provider, account_ref, result.as_of.date(), result.recon_type,
             object_path or f"inline://recon/{result.recon_type}/{external_hash[:16]}",
             external_hash),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                """
                select statement_id from accounting.external_statements
                 where provider=%s and account_ref=%s and statement_date=%s
                   and statement_type=%s and content_hash=%s
                """,
                (provider, account_ref, result.as_of.date(), result.recon_type, external_hash),
            )
            statement_id = cur.fetchone()[0]
        else:
            statement_id = row[0]

        # 2. 같은 입력의 대사가 이미 있으면 그걸 쓴다.
        cur.execute(
            """
            select reconciliation_id from accounting.reconciliations
             where statement_id = %s and reconciliation_type = %s
               and summary->>'input_hash' = %s
             limit 1
            """,
            (statement_id, result.recon_type, input_hash),
        )
        existing = cur.fetchone()
        if existing is not None:
            reconciliation_id = existing[0]
            return ReconRun(reconciliation_id, statement_id,
                            _relabel(result, reconciliation_id), created=False)

        # 3. 대사 본체.
        summary = {
            "input_hash": input_hash,
            "result": result.result,
            "item_count": len(result.items),
            "break_count": len(result.breaks),
            "material_break_count": len(result.material_breaks),
        }
        cur.execute(
            """
            insert into accounting.reconciliations
                (fund_id, statement_id, reconciliation_type, internal_snapshot_ref,
                 external_snapshot_ref, rule_version, status, summary, trace_id,
                 started_at, completed_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning reconciliation_id
            """,
            (fund_id, statement_id, result.recon_type,
             Json({"source": "accounting.journals", "as_of": result.as_of.isoformat(),
                   "content_hash": _hash(internal_ref_payload)}),
             Json({"statement_id": str(statement_id), "provider": provider,
                   "content_hash": external_hash}),
             result.rule_version, _DB_RUN_STATUS[result.result], Json(summary),
             trace_id, started_at, max(started_at, datetime.now(timezone.utc))),
        )
        reconciliation_id = cur.fetchone()[0]

        # 4. 항목.
        item_ids: list[UUID] = []
        for item in result.items:
            cur.execute(
                """
                insert into accounting.reconciliation_items
                    (reconciliation_id, item_type, internal_ref, external_ref,
                     match_method, internal_value, external_value, difference, status)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning reconciliation_item_id
                """,
                (reconciliation_id, result.recon_type, item.internal_ref, item.external_ref,
                 str(item.match_method),
                 Json(_json_safe({"amount": item.internal_value, "detail": item.detail})),
                 Json(_json_safe({"amount": item.external_value})),
                 Json(_json_safe({"amount": item.difference})),
                 _item_status(item)),
            )
            item_ids.append(cur.fetchone()[0])

        # 5. Break. status는 항상 OPEN이다 - 종결 권한이 우리에게 없다.
        break_ids = _relabel(result, reconciliation_id)
        for brk in result.breaks:
            cur.execute(
                """
                insert into accounting.breaks
                    (break_id, reconciliation_item_id, severity, status, evidence)
                values (%s, %s, %s, 'OPEN', %s)
                on conflict (break_id) do nothing
                """,
                (brk.break_id, item_ids[_item_for_break(result, brk)],
                 _DB_SEVERITY[brk.severity],
                 Json({"kind": brk.kind, "detail": brk.detail,
                       "internal_ref": brk.internal_ref, "external_ref": brk.external_ref,
                       "escalates": brk.escalates})),
            )

        cur.execute(
            "update accounting.external_statements set status='RECONCILED' where statement_id=%s",
            (statement_id,),
        )

    return ReconRun(reconciliation_id, statement_id, break_ids, created=True)


def _relabel(result: ReconResult, reconciliation_id: UUID) -> tuple[UUID, ...]:
    """도메인 Break의 id를 canonical id로 바꾸고 그 목록을 돌려준다.

    도메인은 대사할 때마다 `uuid4`를 새로 만든다 - 같은 불일치라도 돌릴 때마다 id가
    달라져서 저장 키로 쓸 수 없다. 그래서 (대사 id + 종류 + 양쪽 참조)로 접어 만든다.
    같은 대사의 같은 불일치는 몇 번을 돌려도 같은 id다.

    도메인 객체를 제자리에서 고치는 것은 의도다. 화면에 나간 id와 DB에 남은 id가
    갈라지면 본 Break를 찾을 수 없다.
    """
    ids = []
    for brk in result.breaks:
        brk.break_id = uuid5(
            NAMESPACE_URL,
            f"break:{reconciliation_id}:{brk.kind}:{brk.internal_ref}:{brk.external_ref}",
        )
        ids.append(brk.break_id)
    return tuple(ids)


def open_breaks(repo: LedgerRepository, fund_id: UUID) -> list[dict]:
    """미종결 Break. 리스크·QA가 지금 읽는 자리다(전송로가 붙기 전까지)."""
    with repo.cursor() as cur:
        cur.execute(
            """
            select b.break_id, b.severity, b.status, b.evidence, b.created_at,
                   r.reconciliation_type, r.reconciliation_id
              from accounting.breaks b
              join accounting.reconciliation_items i
                on i.reconciliation_item_id = b.reconciliation_item_id
              join accounting.reconciliations r
                on r.reconciliation_id = i.reconciliation_id
             where r.fund_id = %s and b.status in ('OPEN', 'INVESTIGATING')
             order by b.created_at desc
            """,
            (fund_id,),
        )
        rows = cur.fetchall()
    return [
        {"break_id": str(break_id), "severity": severity, "status": status,
         "kind": evidence.get("kind"), "detail": evidence.get("detail"),
         "escalates": bool(evidence.get("escalates")),
         "recon_type": recon_type, "reconciliation_id": str(reconciliation_id),
         "created_at": created_at.isoformat()}
        for (break_id, severity, status, evidence, created_at,
             recon_type, reconciliation_id) in rows
    ]


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(Path.cwd() / ".env")
    except ModuleNotFoundError:
        pass

    repo = LedgerRepository.from_env()
    if repo is None:
        print("skip - DATABASE_URL이 없다. 실 DB 왕복 검사라 건너뛴다")
        raise SystemExit(0)

    D = Decimal
    FIXED = datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc)
    fund_id, book_id = repo.bootstrap("ACC01-PAPER", "MAIN")
    with repo.cursor() as cur:
        cur.execute("select instrument_id from reference.instruments order by instrument_id limit 1")
        instrument_id = cur.fetchone()[0]

    def break_rows(reconciliation_id: UUID) -> list[tuple]:
        with repo.cursor() as cur:
            cur.execute(
                """
                select b.severity, b.status, b.evidence->>'kind', i.status
                  from accounting.breaks b
                  join accounting.reconciliation_items i
                    on i.reconciliation_item_id = b.reconciliation_item_id
                 where i.reconciliation_id = %s order by 3
                """,
                (reconciliation_id,),
            )
            return cur.fetchall()

    save = lambda result, **kw: save_reconciliation(  # noqa: E731
        repo, fund_id, result, provider="paper-broker", account_ref="ACC01",
        external_payload=kw.pop("external"), **kw)

    # 1. 일치하는 대사 - Break 없이 MATCHED로 남는다
    matched = reconcile_positions({instrument_id: D("10")}, {instrument_id: D("10")}, as_of=FIXED)
    run = save(matched, external={str(instrument_id): "10"})
    with repo.cursor() as cur:
        cur.execute("select status, summary->>'break_count' from accounting.reconciliations "
                    "where reconciliation_id=%s", (run.reconciliation_id,))
        assert cur.fetchone() == ("MATCHED", "0"), "일치 대사가 MATCHED로 안 남았다"
    assert run.break_ids == ()

    # 2. 포지션 불일치는 항상 material -> DB CRITICAL, 항목은 UNMATCHED
    mismatch = reconcile_positions({instrument_id: D("10")}, {instrument_id: D("9")}, as_of=FIXED)
    run2 = save(mismatch, external={str(instrument_id): "9"})
    assert break_rows(run2.reconciliation_id) == [("CRITICAL", "OPEN", "position_mismatch", "UNMATCHED")], \
        break_rows(run2.reconciliation_id)

    # 3. 저장된 id가 도메인 객체에 되돌아온다 - 화면에서 본 Break를 DB에서 찾을 수 있다
    assert str(mismatch.breaks[0].break_id) == str(run2.break_ids[0]), "Break id가 갈라졌다"

    # 4. 같은 입력이면 같은 대사다. 두 번 돌려도 Break가 두 배가 되지 않는다
    again = reconcile_positions({instrument_id: D("10")}, {instrument_id: D("9")}, as_of=FIXED)
    run3 = save(again, external={str(instrument_id): "9"})
    assert run3.reconciliation_id == run2.reconciliation_id and not run3.created
    assert run3.break_ids == run2.break_ids
    assert len(break_rows(run2.reconciliation_id)) == 1, "재실행으로 Break가 늘었다"

    # 5. 내부 상태가 달라지면 새 대사다 (같은 명세서라도)
    moved = reconcile_positions({instrument_id: D("11")}, {instrument_id: D("9")}, as_of=FIXED)
    run4 = save(moved, external={str(instrument_id): "9"})
    assert run4.reconciliation_id != run2.reconciliation_id, "내부가 바뀌었는데 옛 대사를 재사용했다"

    # 6. 현금 허용 오차 - Break는 없지만 항목은 TOLERANCE로 남는다.
    #    MATCHED로 뭉개면 실제 차이가 얼마였는지 사라진다
    tolerated = reconcile_cash(D("1000000"), D("999999.5"), as_of=FIXED)
    assert not tolerated.breaks
    run5 = save(tolerated, external={"cash": "999999.5"})
    with repo.cursor() as cur:
        cur.execute("select status, external_value->>'amount' from accounting.reconciliation_items "
                    "where reconciliation_id=%s", (run5.reconciliation_id,))
        assert cur.fetchone() == ("TOLERANCE", "999999.5"), "허용 오차가 MATCHED로 뭉개졌다"

    # 7. 큰 현금 차이는 Break. 참조가 없는 Break도 항목에 연결된다
    big = reconcile_cash(D("1000000"), D("900000"), as_of=FIXED)
    run6 = save(big, external={"cash": "900000"})
    assert break_rows(run6.reconciliation_id)[0][:3] == ("CRITICAL", "OPEN", "cash_mismatch")

    # 8. 종결 경로가 없다. Break는 OPEN으로만 만들어지고 여기서 닫히지 않는다.
    #    문자열 하나를 찾는 대신 "breaks를 수정하는 SQL이 있는가"를 본다 - 나중에
    #    누가 종결 경로를 추가하면 이름을 뭐라 짓든 여기서 걸린다.
    #    바늘을 쪼개 쓰는 이유: 한 조각으로 두면 이 검사문 자신이 걸린다.
    source = Path(__file__).read_text(encoding="utf-8")
    assert ("update " + "accounting.breaks") not in source, \
        "이 모듈에 Break 수정 경로가 생겼다 - 종결 권한은 AI QA/감사본부다"

    # 9. 미종결 Break 조회 - 리스크·QA가 읽을 자리
    opened = open_breaks(repo, fund_id)
    assert any(b["break_id"] == str(run2.break_ids[0]) for b in opened), opened
    assert all(b["status"] in ("OPEN", "INVESTIGATING") for b in opened)
    assert any(b["escalates"] for b in opened), "material Break가 escalates=false로 남았다"

    print(f"ok - 대사 저장 9개 영역 점검 통과 (실 DB, 미종결 Break {len(opened)}건, "
          f"재실행 멱등, 종결 경로 없음)")
