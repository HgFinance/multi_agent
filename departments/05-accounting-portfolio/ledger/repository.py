#!/usr/bin/env python3
"""원장 저장소 — 프로세스 메모리에서 Supabase `accounting.*`로.

소유: 도현 (회계·포트폴리오본부)
근거: docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 4.4, 8.2
      docs/02-engineering/TECH_STACK_DECISIONS.md (Supabase PostgreSQL = System of Record)
      supabase/migrations/20260729000400_execution_risk_accounting.sql

`ledger.py`의 판정 로직은 한 줄도 옮겨오지 않았다. 여기는 순수 저장 계층이고,
차대균형·멱등·보유초과 매도 차단은 그대로 도메인이 한다. DB도 같은 불변식을
독립적으로 강제하므로(아래) 이중 방어가 된다.

**DB가 이미 강제하는 것** — 우리 코드가 죽어도 남는 방어선:
  - `journals_validate_posting` 트리거: POSTED 전환 시 라인 2개 이상 + 차대 합계 0
  - `journals_protect_posted` / `journal_lines_protect_posted`: POSTED 분개 수정·삭제 거부.
    허용되는 변경은 POSTED -> REVERSED 상태 전환 하나뿐이다(불변식 2와 같은 규칙)
  - `unique (event_type, source_event_id)`: 같은 체결로 분개가 두 번 생기지 않는다(불변식 3)

그래서 Posting은 **DRAFT로 넣고 라인을 붙인 뒤 POSTED로 올리는 3단계**다. 처음부터
POSTED로 넣으면 라인을 붙일 수 없고(immutable), 균형 검증도 안 걸린다.

**여기서 하지 않는 것:**
  - 시세 조회·NAV 확정·Break 종결. 저장만 한다.
  - `execution.*` 쓰기. 체결 사실은 트레이딩본부가 만든다. 우리는 읽기만 한다.

자체 점검: python departments/05-accounting-portfolio/ledger/repository.py
           (DATABASE_URL 없으면 건너뛴다 - 실 DB 왕복 검사다)
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator
from uuid import NAMESPACE_OID, UUID, uuid5

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "portfolio"))

from ledger import (
    ACCOUNT_TYPES,
    CASH,
    PAYABLE,
    RECEIVABLE,
    REALIZED_PNL,
    ZERO,
    Journal,
    JournalLine,
    Ledger,
    decimal_str as _num,
)
from portfolio import PortfolioSnapshot, PositionValuation

SNAPSHOT_SCHEMA_VERSION = 1
# Durable mode is opt-in. In PAPER_DB (and equivalent durable deployments),
# absence of DATABASE_URL is an operational error rather than permission to
# discard accounting state into process memory.
_DURABLE_MODES = {"PAPER_DB", "DURABLE", "PRODUCTION", "LIVE_DB"}
ACCOUNTING_LEDGER_DATABASE_ROLE = "svc_accounting_ledger"

def durable_required_from_env() -> bool:
    mode = os.environ.get("ACCOUNTING_MODE", "").strip().upper()
    # An explicit offline mode is the operator/test contract.  It must win
    # over inherited PAPER_DB or ACCOUNTING_DURABLE_REQUIRED values from a
    # shared .env; otherwise importing another service can silently turn an
    # offline E2E read into a durable-database failure.
    if mode == "OFFLINE":
        return False
    flag = os.environ.get("ACCOUNTING_DURABLE_REQUIRED", "").strip().lower()
    paper_db = os.environ.get("PAPER_DB", "").strip().lower()
    return (
        mode in _DURABLE_MODES
        or flag in {"1", "true", "yes", "on"}
        or paper_db in {"1", "true", "yes", "on"}
    )

# ledger.py의 account_code -> DB `accounting.ledger_accounts.name`.
# 계정과목 자체는 ledger.py가 소유한다. 여기엔 표시 이름만 둔다.
ACCOUNT_NAMES = {
    "1000": "현금", "1100": "유가증권", "1200": "미수금",
    "2000": "미지급금", "2100": "미지급보수", "3000": "자본금",
    "4000": "실현손익", "4100": "평가손익",
    "5000": "수수료비용", "5100": "세금비용",
    "5200": "관리보수비용", "5300": "성과보수비용",
}

# 도메인 status <-> DB status. DB는 대문자 + DRAFT/VOID까지 가진다.
_DB_STATUS = {"posted": "POSTED", "reversed": "REVERSED"}
_DOMAIN_STATUS = {v: k for k, v in _DB_STATUS.items()}


class LedgerPersistenceError(RuntimeError):
    """원장을 저장·조회하지 못한 경우. 조용히 메모리로 되돌아가지 않는다."""


class LedgerConflictError(LedgerPersistenceError):
    """다른 장부가 이미 쓴 원천 이벤트. 재시도해도 달라지지 않는다 - 요청이 틀린 것이다."""


def _load_driver() -> tuple[Any, Any, Any]:
    try:
        import psycopg2
        from psycopg2.extras import Json, register_uuid
        from psycopg2.pool import ThreadedConnectionPool
    except ModuleNotFoundError as exc:  # pragma: no cover - 설치 안내
        raise LedgerPersistenceError(
            "원장 DB 저장에는 psycopg2-binary가 필요합니다. "
            "`uv pip install --python .venv/Scripts/python.exe psycopg2-binary`"
        ) from exc
    register_uuid()
    return psycopg2, Json, ThreadedConnectionPool


def _trace_uuid(trace_id: str, fallback: UUID) -> UUID:
    """`accounting.journals.trace_id`는 uuid 컬럼이고 도메인 trace_id는 문자열이다.

    ponytail: PLAT-01 Event Envelope가 붙으면 진짜 trace uuid가 흘러온다. 그 전까지는
              같은 문자열이 항상 같은 uuid가 되도록 uuid5로 접는다 - Replay 재현성만은
              지킨다. 문자열이 비면 분개 id에서 파생시킨다(추적은 안 되지만 not null이다).
    """
    try:
        return UUID(trace_id)
    except (ValueError, AttributeError, TypeError):
        return uuid5(NAMESPACE_OID, trace_id) if trace_id else uuid5(NAMESPACE_OID, str(fallback))


def _canonical_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class LedgerRepository:
    """`accounting.journals` / `journal_lines` / `positions` / `cash_balances` /
    `portfolio_snapshots` 전용 저장소."""

    def __init__(self, pool: Any, *, database_role: str | None = None) -> None:
        self._pool = pool
        self._database_role = (database_role or "").strip() or None
        if (
            self._database_role is not None
            and self._database_role != ACCOUNTING_LEDGER_DATABASE_ROLE
        ):
            raise LedgerPersistenceError(
                "ACCOUNTING_DATABASE_ROLE must be svc_accounting_ledger"
            )
        self._accounts: dict[UUID, dict[str, UUID]] = {}
        self._currency: dict[UUID, str] = {}

    @classmethod
    def connect(
        cls, dsn: str, *, database_role: str | None = None
    ) -> LedgerRepository:
        _, _, ThreadedConnectionPool = _load_driver()
        # minconn=0 - 유휴 커넥션을 잡지 않는다.  The UI snapshot endpoint
        # can be polled by several browser clients at once; a fixed maxconn=4
        # caused transient PoolError 500s even though the database itself was
        # healthy.  Keep the bound configurable for the deployment's pooler.
        try:
            maxconn = int(os.environ.get("ACCOUNTING_DATABASE_POOL_MAX", "8"))
        except (TypeError, ValueError):
            maxconn = 8
        maxconn = max(4, min(maxconn, 32))
        return cls(
            ThreadedConnectionPool(0, maxconn, dsn), database_role=database_role
        )

    @classmethod
    def from_env(cls, *, required: bool | None = None) -> LedgerRepository | None:
        """DATABASE_URL이 없으면 None (명시적 offline 모드일 때만)."""
        mode = os.environ.get("ACCOUNTING_MODE", "").strip().upper()
        required = durable_required_from_env() if required is None else required
        if mode == "OFFLINE" and not required:
            return None
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            if required:
                raise LedgerPersistenceError(
                    "durable accounting mode requires DATABASE_URL; "
                    "offline memory mode was not selected"
                )
            return None
        return cls.connect(
            dsn,
            database_role=os.environ.get("ACCOUNTING_DATABASE_ROLE"),
        )

    def close(self) -> None:
        self._pool.closeall()

    @contextmanager
    def cursor(self) -> Iterator[Any]:
        """트랜잭션 하나. 같은 부서의 다른 저장 모듈(대사)도 이 Pool을 함께 쓴다."""
        psycopg2, _, _ = _load_driver()
        conn = self._pool.getconn()
        try:
            with conn:  # 정상 종료면 commit, 예외면 rollback
                with conn.cursor() as cur:
                    # This repository owns Journal/projection mutations. A
                    # transaction-pool backend may have been left with a
                    # read-only session default by an unrelated read client;
                    # override it before the first domain statement. A real
                    # replica still rejects READ WRITE, so the writer fails
                    # closed instead of pretending that projection succeeded.
                    cur.execute("set transaction read write")
                    # Reduce the shared operational login before any domain
                    # query. The exact allowlist above keeps this from becoming
                    # a caller-controlled SQL identifier.
                    if self._database_role == ACCOUNTING_LEDGER_DATABASE_ROLE:
                        cur.execute("set local role svc_accounting_ledger")
                    yield cur
        except psycopg2.Error as exc:
            raise LedgerPersistenceError(f"원장 DB 작업 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    # -- 기준정보 -----------------------------------------------------------

    def bootstrap(self, fund_code: str, book_code: str, *,
                  fund_name: str | None = None, book_name: str | None = None,
                  base_currency: str = "KRW") -> tuple[UUID, UUID]:
        """Fund / Book / 계정과목을 만든다. 멱등이다.

        원장을 쓰려면 이 셋이 먼저 있어야 한다(전부 FK). 요청 경로에서는 부르지
        않는다 - Fund를 만드는 건 자본 구조 결정이지 주문 처리 중에 일어날 일이 아니다.
        """
        with self.cursor() as cur:
            cur.execute(
                """
                insert into accounting.funds (fund_code, name, base_currency, inception_date, status)
                values (%s, %s, %s, current_date, 'ACTIVE')
                on conflict (fund_code) do update set name = excluded.name
                returning fund_id
                """,
                (fund_code, fund_name or fund_code, base_currency),
            )
            fund_id = cur.fetchone()[0]
            cur.execute(
                """
                insert into accounting.books (fund_id, book_code, name, book_type)
                values (%s, %s, %s, 'PAPER')
                on conflict (fund_id, book_code) do update set name = excluded.name
                returning book_id
                """,
                (fund_id, book_code, book_name or book_code),
            )
            book_id = cur.fetchone()[0]
            for code, name in ACCOUNT_NAMES.items():
                cur.execute(
                    """
                    insert into accounting.ledger_accounts
                        (fund_id, account_code, name, account_type, currency)
                    values (%s, %s, %s, %s, %s)
                    on conflict (fund_id, account_code) do nothing
                    """,
                    (fund_id, code, name, ACCOUNT_TYPES[code].upper(), base_currency),
                )
        self._accounts.pop(fund_id, None)
        return fund_id, book_id

    def default_book(self) -> tuple[UUID, UUID] | None:
        """파라미터 없이 물었을 때 쓸 (fund_id, book_id). **모르면 안 고른다.**

        `ACCOUNTING_DEFAULT_BOOK_ID`가 있으면 그게 이긴다. 없으면 ACTIVE 장부가
        정확히 하나일 때만 그걸 쓴다 - 둘 이상인데 아무거나 고르면 화면과 보고가
        남의 펀드 수치를 자기 것으로 말한다. 못 고르면 None이고, 호출자는 그때
        수치를 지어내지 말고 "고르지 못했다"로 떨어져야 한다.

        BFF(`/ui/snapshot`)와 마감 스케줄러가 같은 답을 써야 해서 여기 둔다 -
        두 곳이 각자 고르면 화면과 보고서가 다른 장부를 말하게 된다.
        """
        pinned = os.environ.get("ACCOUNTING_DEFAULT_BOOK_ID", "").strip()
        if pinned:
            try:
                book_id = UUID(pinned)
            except ValueError:
                return None
            fund_id = self.fund_of_book(book_id)
            return (fund_id, book_id) if fund_id else None
        with self.cursor() as cur:
            cur.execute("select fund_id, book_id from accounting.books "
                        " where status = 'ACTIVE' order by book_id limit 2")
            rows = cur.fetchall()
        return (rows[0][0], rows[0][1]) if len(rows) == 1 else None

    def book_for_fund(self, fund_id: UUID) -> UUID | None:
        """Resolve one active Book for a selected Fund, failing closed on ambiguity."""
        with self.cursor() as cur:
            cur.execute(
                """
                select book_id
                  from accounting.books
                 where fund_id = %s and status = 'ACTIVE'
                 order by book_id
                 limit 2
                """,
                (fund_id,),
            )
            rows = cur.fetchall()
        return rows[0][0] if len(rows) == 1 else None

    def fund_of_book(self, book_id: UUID) -> UUID | None:
        """book_id 하나로 Fund가 정해진다. 그래서 ledger_id == book_id로 쓴다."""
        with self.cursor() as cur:
            cur.execute("select fund_id from accounting.books where book_id = %s", (book_id,))
            row = cur.fetchone()
        return row[0] if row else None

    def counts(self) -> tuple[int, int]:
        """(장부 수, 분개 수). /health가 "몇 건이 실제로 저장돼 있는지"를 말하게 한다."""
        with self.cursor() as cur:
            cur.execute("select (select count(*) from accounting.books), "
                        "(select count(*) from accounting.journals)")
            return cur.fetchone()

    def account_ids(self, fund_id: UUID) -> dict[str, UUID]:
        cached = self._accounts.get(fund_id)
        if cached:
            return cached
        with self.cursor() as cur:
            cur.execute(
                "select account_code, account_id from accounting.ledger_accounts where fund_id = %s",
                (fund_id,),
            )
            accounts = {code: account_id for code, account_id in cur.fetchall()}
        if not accounts:
            raise LedgerPersistenceError(
                f"Fund {fund_id}에 계정과목이 없습니다. bootstrap()을 먼저 실행하세요"
            )
        self._accounts[fund_id] = accounts
        return accounts

    def instrument_by_symbol(self, symbol: str, *, as_of: datetime | None = None,
                             market: str = "KRX") -> UUID | None:
        """symbol -> instrument_id. **market-api는 symbol을 쓰고 우리 도메인은 UUID를 쓴다.**

        `reference.instrument_symbols`가 그 다리이며 **Point-in-Time 표다**
        (`valid_from`/`valid_to`). KRX는 상장폐지된 종목코드를 나중에 다른 회사에
        재배정하므로 "지금 이 코드의 주인"으로 과거 체결을 해석하면 남의 종목에
        분개가 붙는다. 그래서 as_of를 받아 그 시점의 매핑을 쓴다.

        모르면 None이다. 짐작해서 아무 instrument에 붙이지 않는다.
        """
        as_of = as_of or datetime.now(timezone.utc)
        with self.cursor() as cur:
            cur.execute(
                """
                select instrument_id from reference.instrument_symbols
                 where symbol = %s and market = %s
                   and valid_from <= %s and (valid_to is null or valid_to > %s)
                 order by is_primary desc, valid_from desc
                 limit 1
                """,
                (symbol, market, as_of, as_of),
            )
            row = cur.fetchone()
        return row[0] if row else None

    def symbols_for(self, instrument_ids: list[UUID], *, as_of: datetime | None = None,
                    market: str = "KRX") -> dict[UUID, str]:
        """instrument_id -> symbol. `instrument_by_symbol`의 역방향이다.

        평가 경로가 이 방향을 쓴다 - 우리는 보유 종목을 UUID로 알고 있고 market-api는
        symbol로만 답한다. PIT 규칙은 반대 방향과 같다(`valid_from`/`valid_to`).

        모르는 종목은 **결과에 없다.** 짐작해서 아무 코드나 붙이면 남의 종목 시세로
        NAV가 나온다 - 빠진 채로 두면 그 종목의 Mark가 없어 NAV가 거부된다.
        """
        ids = list(dict.fromkeys(instrument_ids))
        if not ids:
            return {}
        as_of = as_of or datetime.now(timezone.utc)
        with self.cursor() as cur:
            cur.execute(
                """
                select distinct on (instrument_id) instrument_id, symbol
                  from reference.instrument_symbols
                 where instrument_id = any(%s) and market = %s
                   and valid_from <= %s and (valid_to is null or valid_to > %s)
                 order by instrument_id, is_primary desc, valid_from desc
                """,
                (ids, market, as_of, as_of),
            )
            return {instrument_id: symbol for instrument_id, symbol in cur.fetchall()}

    def base_currency(self, fund_id: UUID) -> str:
        cached = self._currency.get(fund_id)
        if cached:
            return cached
        with self.cursor() as cur:
            cur.execute("select base_currency from accounting.funds where fund_id = %s", (fund_id,))
            row = cur.fetchone()
        if row is None:
            raise LedgerPersistenceError(f"그런 fund_id가 없습니다: {fund_id}")
        self._currency[fund_id] = row[0]
        return row[0]

    # -- 분개 ---------------------------------------------------------------

    def insert_journal(self, journal: Journal) -> bool:
        """분개 하나를 기록한다. 이미 있으면 False.

        DRAFT -> 라인 -> POSTED 3단계인 이유는 파일 상단 참고. 이 순서를 지켜야
        `journals_validate_posting` 트리거의 차대균형 검사가 실제로 걸린다.
        """
        accounts = self.account_ids(journal.fund_id)
        currency = self.base_currency(journal.fund_id)
        missing = [l.account_code for l in journal.lines if l.account_code not in accounts]
        if missing:
            raise LedgerPersistenceError(f"등록되지 않은 계정과목입니다: {sorted(set(missing))}")

        _, Json, _ = _load_driver()
        metadata = dict(journal.metadata)
        if journal.reason:
            metadata["reason"] = journal.reason
        with self.cursor() as cur:
            cur.execute(
                """
                insert into accounting.journals (
                    journal_id, fund_id, book_id, event_type, source_event_id,
                    effective_at, accounting_date, base_currency, status,
                    reversal_of_journal_id, created_by_service, trace_id
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, 'DRAFT', %s, %s, %s)
                on conflict (event_type, source_event_id) do nothing
                returning journal_id
                """,
                (journal.journal_id, journal.fund_id, journal.book_id,
                 journal.event_type, journal.source_event_id,
                 journal.effective_at, journal.accounting_date, currency,
                 journal.reversal_of, journal.created_by_service,
                 _trace_uuid(journal.trace_id, journal.journal_id)),
            )
            if cur.fetchone() is None:
                # 같은 원천 이벤트가 이미 기록돼 있다. 재처리에서 정상적으로 일어난다.
                # 단 `unique (event_type, source_event_id)`는 **Fund/Book 전역**이다.
                # 다른 장부가 먼저 쓴 id라면 이 장부에는 분개가 없는데 있다고 착각하게
                # 되므로(현금이 조용히 비는 증상) 조용히 넘어가지 않는다.
                cur.execute(
                    "select fund_id, book_id from accounting.journals "
                    "where event_type = %s and source_event_id = %s",
                    (journal.event_type, journal.source_event_id),
                )
                owner = cur.fetchone()
                if owner != (journal.fund_id, journal.book_id):
                    raise LedgerConflictError(
                        f"source_event_id '{journal.source_event_id}'는 다른 장부"
                        f"(fund={owner[0]}, book={owner[1]})가 이미 사용했습니다"
                    )
                return False
            for line_no, line in enumerate(journal.lines, start=1):
                cur.execute(
                    """
                    insert into accounting.journal_lines (
                        journal_id, account_id, instrument_id, line_no,
                        debit, credit, quantity, unit_price, currency, metadata
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (journal.journal_id, accounts[line.account_code], line.instrument_id,
                     line_no, line.debit, line.credit, line.quantity, line.unit_price,
                     currency, Json(metadata)),
                )
            cur.execute(
                "update accounting.journals set status = %s where journal_id = %s",
                (_DB_STATUS.get(journal.status, "POSTED"), journal.journal_id),
            )
        return True

    def mark_reversed(self, journal_id: UUID) -> None:
        """원본을 REVERSED로 표시한다. **내용은 건드리지 않는다**(불변식 2).

        DB 트리거가 status 외의 컬럼이 함께 바뀌면 거부하므로, 여기서 다른 컬럼을
        섞어 쓰려 해도 저장되지 않는다.
        """
        with self.cursor() as cur:
            cur.execute(
                "update accounting.journals set status = 'REVERSED' "
                "where journal_id = %s and status = 'POSTED'",
                (journal_id,),
            )

    def load(self, fund_id: UUID, book_id: UUID) -> PostgresLedger:
        """DB의 분개로 원장을 복원한다.

        ponytail: 매번 전 분개를 읽는다. Paper 규모(분개 수백 건)에서는 문제가 없고,
                  느려지면 Position/Cash projection 테이블을 기점으로 삼고 그 이후
                  분개만 읽는 방식으로 바꾼다 - projection은 이미 저장하고 있다.
        """
        with self.cursor() as cur:
            cur.execute(
                """
                select journal_id, event_type, source_event_id, effective_at,
                       accounting_date, status, reversal_of_journal_id,
                       created_by_service, trace_id
                  from accounting.journals
                 where fund_id = %s and book_id = %s
                 order by effective_at, created_at, journal_id
                """,
                (fund_id, book_id),
            )
            rows = cur.fetchall()
            journal_ids = [r[0] for r in rows]
            lines: dict[UUID, list[JournalLine]] = {}
            metadata_by_journal: dict[UUID, dict[str, Any]] = {}
            if journal_ids:
                cur.execute(
                    """
                    select l.journal_id, a.account_code, l.debit, l.credit,
                           l.instrument_id, l.quantity, l.unit_price, l.metadata
                      from accounting.journal_lines l
                      join accounting.ledger_accounts a on a.account_id = l.account_id
                     where l.journal_id = any(%s)
                     order by l.journal_id, l.line_no
                    """,
                    (journal_ids,),
                )
                for jid, code, debit, credit, instrument_id, qty, price, meta in cur.fetchall():
                    if meta:
                        metadata_by_journal.setdefault(jid, dict(meta))
                    lines.setdefault(jid, []).append(JournalLine(
                        account_code=code, debit=debit, credit=credit,
                        instrument_id=instrument_id, quantity=qty, unit_price=price,
                    ))

        journals = [
            Journal(
                journal_id=jid, fund_id=fund_id, book_id=book_id,
                event_type=event_type, source_event_id=source_event_id,
                effective_at=effective_at, accounting_date=accounting_date,
                lines=lines.get(jid, []),
                status=_DOMAIN_STATUS.get(status, status.lower()),
                reversal_of=reversal_of, created_by_service=service,
                trace_id=str(trace_id),
                reason=str(metadata_by_journal.get(jid, {}).get("reason", "")),
                metadata=metadata_by_journal.get(jid, {}),
            )
            for (jid, event_type, source_event_id, effective_at, accounting_date,
                 status, reversal_of, service, trace_id) in rows
        ]
        # 마감 기준일. **여기서 붙이지 않으면 게이트가 영원히 안 걸린다** -
        # 인메모리 기본값이 None(전 기간 열림)이라 DB 원장이 조용히 소급 분개를
        # 받아준다. 값의 출처는 `close/nav_close.py::closed_through`(nav_runs)다.
        with self.cursor() as cur:
            cur.execute(
                """
                select max(valuation_date) from accounting.nav_runs
                 where fund_id = %s and run_type = 'OFFICIAL' and status = 'APPROVED'
                """,
                (fund_id,),
            )
            row = cur.fetchone()
        return PostgresLedger(self, fund_id, book_id, journals,
                              closed_through=row[0] if row else None)

    # -- Projection ---------------------------------------------------------

    def save_projection(self, ledger: Ledger) -> None:
        """Position과 Cash를 저장한다. **분개에서 재계산한 값만 쓴다**(불변식 4).

        전량 매도된 종목은 `rebuild()`가 빼버리므로 여기서 수량 0으로 눌러준다 -
        안 그러면 DB에 판 종목이 계속 남는다.
        """
        positions, cash = ledger.rebuild()
        balances = ledger.trial_balance()
        # Receivables increase economically available cash; payables reduce
        # it.  This projection must be durable before Trading releases a fill
        # reservation, otherwise a T+2 BUY can be spent twice before settlement.
        unsettled_cash = balances.get(RECEIVABLE, ZERO) + balances.get(PAYABLE, ZERO)
        realized = _realized_by_instrument(ledger)
        as_of = datetime.now(timezone.utc)
        cash_account = self.account_ids(ledger.fund_id)[CASH]
        currency = self.base_currency(ledger.fund_id)
        last_journal = ledger.journals[-1].journal_id if ledger.journals else None

        with self.cursor() as cur:
            for instrument_id, pos in positions.items():
                cur.execute(
                    """
                    insert into accounting.positions (
                        fund_id, book_id, instrument_id, quantity, average_cost,
                        cost_currency, realized_pnl, last_journal_id, as_of
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (fund_id, book_id, strategy_version_id, instrument_id)
                    do update set quantity = excluded.quantity,
                                  average_cost = excluded.average_cost,
                                  realized_pnl = excluded.realized_pnl,
                                  last_journal_id = excluded.last_journal_id,
                                  as_of = excluded.as_of,
                                  version = accounting.positions.version + 1
                    """,
                    (ledger.fund_id, ledger.book_id, instrument_id, pos.quantity,
                     pos.average_cost, currency, realized.get(instrument_id, ZERO),
                     last_journal, as_of),
                )
            cur.execute(
                """
                update accounting.positions
                   set quantity = 0, as_of = %s, version = version + 1
                 where fund_id = %s and book_id = %s and quantity <> 0
                   and not (instrument_id = any(%s))
                """,
                (as_of, ledger.fund_id, ledger.book_id, list(positions.keys())),
            )
            cur.execute(
                """
                insert into accounting.cash_balances (
                    fund_id, book_id, account_id, currency, settled_amount,
                    unsettled_amount,last_journal_id, as_of
                ) values (%s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (fund_id, book_id, account_id, currency)
                do update set settled_amount = excluded.settled_amount,
                              unsettled_amount = excluded.unsettled_amount,
                              last_journal_id = excluded.last_journal_id,
                              as_of = excluded.as_of,
                              version = accounting.cash_balances.version + 1
                """,
                (ledger.fund_id, ledger.book_id, cash_account, currency, cash,
                 unsettled_cash, last_journal, as_of),
            )

    def save_snapshot(self, snapshot: PortfolioSnapshot) -> UUID:
        """확정된 스냅샷을 남긴다. 같은 내용이면 다시 만들지 않는다.

        `is_official`은 여기에도 없다. NAV 확정은 별도 승인 절차이고 이 행은 그 전
        단계의 계산 결과다.

        `quality_status`를 우리가 정하지 않고 스냅샷에게 묻는다 - 하나라도 미확정
        봉(`MarkPrice.is_final=False`)으로 평가됐으면 WARN이다. 여기에 PASS를 박아
        두면 미확정 가격으로 만든 NAV가 확정 종가 NAV와 구분되지 않는다.
        """
        _, Json, _ = _load_driver()
        currency = self.base_currency(snapshot.fund_id)
        cash = {currency: _num(snapshot.cash)}
        positions = [
            {"instrument_id": str(p.instrument_id), "quantity": _num(p.quantity),
             "average_cost": _num(p.average_cost), "mark_price": _num(p.mark_price),
             "mark_as_of": p.mark_as_of.isoformat(), "mark_is_final": p.mark_is_final,
             "cost_basis": _num(p.cost_basis), "market_value": _num(p.market_value),
             "unrealized_pnl": _num(p.unrealized_pnl)}
            for p in snapshot.positions
        ]
        content_hash = _canonical_hash({
            "cash": cash, "positions": positions, "nav": _num(snapshot.nav),
            "receivable": _num(snapshot.receivable), "payable": _num(snapshot.payable),
            "realized_pnl": _num(snapshot.realized_pnl),
            "fees": _num(snapshot.fees), "taxes": _num(snapshot.taxes),
            "quality_status": snapshot.quality_status,
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
        })
        with self.cursor() as cur:
            cur.execute(
                """
                insert into accounting.portfolio_snapshots (
                    fund_id, book_id, as_of, cash, positions, gross_exposure,
                    net_exposure, nav, currency, quality_status, content_hash, schema_version
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (fund_id, book_id, as_of, content_hash) do nothing
                returning portfolio_snapshot_id
                """,
                (snapshot.fund_id, snapshot.book_id, snapshot.as_of, Json(cash),
                 Json(positions), snapshot.gross_exposure, snapshot.net_exposure,
                 snapshot.nav, currency, snapshot.quality_status, content_hash,
                 SNAPSHOT_SCHEMA_VERSION),
            )
            row = cur.fetchone()
            if row is not None:
                return row[0]
            cur.execute(
                """
                select portfolio_snapshot_id from accounting.portfolio_snapshots
                 where fund_id = %s and book_id = %s and as_of = %s and content_hash = %s
                """,
                (snapshot.fund_id, snapshot.book_id, snapshot.as_of, content_hash),
            )
            return cur.fetchone()[0]

    def load_snapshots(
        self,
        fund_id: UUID,
        book_id: UUID,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        collapse_unchanged: bool = False,
    ) -> list[PortfolioSnapshot]:
        """저장된 스냅샷을 도메인 객체로 되살린다.

        기간 필터는 SQL에서 먼저 적용한다. ``collapse_unchanged``는 연속된 동일
        ``content_hash``만 접으므로 상태가 바뀌었다가 돌아온 경로와 Drawdown은
        보존한다. 기본값은 기존 전체-history 계약을 그대로 유지한다.
        """
        with self.cursor() as cur:
            cur.execute(
                """
                with bounded as (
                    select as_of, cash, positions, content_hash, created_at,
                           portfolio_snapshot_id
                      from accounting.portfolio_snapshots
                     where fund_id = %s and book_id = %s
                       and (%s::timestamptz is null or as_of >= %s)
                       and (%s::timestamptz is null or as_of < %s)
                ), sequenced as (
                    select *, lag(content_hash) over (
                        order by as_of, created_at, portfolio_snapshot_id
                    ) as previous_content_hash
                      from bounded
                )
                select as_of, cash, positions
                  from sequenced
                 where not %s
                    or content_hash is distinct from previous_content_hash
                 order by as_of, created_at, portfolio_snapshot_id
                """,
                (fund_id, book_id, start_at, start_at, end_at, end_at,
                 collapse_unchanged),
            )
            rows = cur.fetchall()

        snapshots = []
        for as_of, cash, positions in rows:
            snapshots.append(PortfolioSnapshot(
                fund_id=fund_id, book_id=book_id, as_of=as_of,
                cash=Decimal(next(iter(cash.values()), "0")),
                # ponytail: 미수금·미지급금·실현손익·비용은 스냅샷 jsonb에 자리가 없다.
                # 일일 보고서는 이 넷을 원장에서 다시 읽으므로 지금은 0으로 둔다.
                # NAV 항등식에 미수/미지급이 들어가면 그때 스키마 델타로 넘긴다.
                receivable=ZERO, payable=ZERO, realized_pnl=ZERO, fees=ZERO, taxes=ZERO,
                positions=tuple(
                    PositionValuation(
                        instrument_id=UUID(p["instrument_id"]),
                        quantity=Decimal(p["quantity"]),
                        average_cost=Decimal(p["average_cost"]),
                        mark_price=Decimal(p["mark_price"]),
                        mark_as_of=datetime.fromisoformat(p["mark_as_of"]),
                        # 옛 스냅샷에는 이 키가 없다. 없으면 미확정으로 읽는다 -
                        # 모르는 것을 확정으로 승격시키지 않는다.
                        mark_is_final=bool(p.get("mark_is_final", False)),
                    )
                    for p in positions
                ),
            ))
        return snapshots


def _realized_by_instrument(ledger: Ledger) -> dict[UUID, Decimal]:
    """종목별 실현손익. 대변이 이익이라 부호를 뒤집는다."""
    realized: dict[UUID, Decimal] = {}
    for journal in ledger.journals:
        for line in journal.lines:
            if line.account_code == REALIZED_PNL and line.instrument_id is not None:
                realized[line.instrument_id] = (
                    realized.get(line.instrument_id, ZERO) + line.credit - line.debit
                )
    return realized


class PostgresLedger(Ledger):
    """Ledger에 저장소만 붙인 것. 회계 규칙은 상속받아 그대로 쓴다.

    `post()` 하나만 가로채면 모든 분개가 저장된다 - 자본 납입·체결·기업행위·반대분개가
    전부 `post()`를 거치기 때문이다. 규칙이 두 군데로 갈라지지 않는다.

    ponytail: load -> 계산 -> insert 사이에 락이 없다. 같은 장부에 두 프로세스가 동시에
              분개하면 이중 분개는 DB의 unique (event_type, source_event_id)가 막지만,
              평균원가가 상대의 체결을 못 본 채 계산될 수 있다. 지금은 accounting-api
              단일 인스턴스라 발생하지 않는다(compose.yaml에 복제 금지로 적어둠).
              해소는 `select ... for update`로 장부 행을 잡거나 체결을 큐로 직렬화하는
              것이고, 그건 PLAT-02(Redis Queue)가 붙은 뒤에 한다.
    """

    def __init__(self, repo: LedgerRepository, fund_id: UUID, book_id: UUID,
                 journals: list[Journal], *, closed_through=None) -> None:
        super().__init__(
            fund_id=fund_id, book_id=book_id, journals=journals,
            closed_through=closed_through,
            _posted_sources={(j.event_type, j.source_event_id) for j in journals},
        )
        self._repo = repo

    def post(self, journal: Journal) -> Journal:
        posted = super().post(journal)
        if posted is journal:  # 새로 붙은 것만 저장한다. 중복이면 기존 분개가 돌아온다
            try:
                inserted = self._repo.insert_journal(journal)
            except Exception:
                # A failed DB transaction must not leave a speculative in-memory
                # Journal that a caller could mistake for durable evidence.
                self.journals.remove(journal)
                self._posted_sources.discard((journal.event_type, journal.source_event_id))
                raise
            if not inserted:
                # Another worker won the source-event unique race. Do not keep
                # the speculative local Journal in projections; reload the
                # committed row and converge on its immutable evidence.
                key = (journal.event_type, journal.source_event_id)
                self.journals.remove(journal)
                self._posted_sources.discard(key)
                loaded = self._repo.load(self.fund_id, self.book_id)
                existing = next(
                    (j for j in loaded.journals
                     if (j.event_type, j.source_event_id) == key
                     and j.reversal_of is None),
                    None,
                )
                if existing is None:
                    raise LedgerPersistenceError(
                        f"원천 이벤트 저장 경합 후 분개를 복원하지 못했습니다: {key}"
                    )
                self.journals.append(existing)
                self._posted_sources.add(key)
                return existing
        return posted

    def reverse(self, journal_id: UUID, reason: str) -> Journal:
        rev = super().reverse(journal_id, reason)  # 내부 post()가 반대분개를 저장한다
        self._repo.mark_reversed(journal_id)
        return rev


if __name__ == "__main__":
    # 실 DB에 붙는 점검이다. 여러 번 돌려도 같은 상태가 되도록 모든 원천 이벤트 id를
    # 고정했다 - 자체 점검이 실행할 때마다 장부를 늘리면 그건 점검이 아니라 오염이다.
    sys.path.insert(0, str(_HERE.parent.parent / "02-trading" / "contracts"))
    from contracts import Side

    from ledger import CAPITAL, Position
    from portfolio import MarkPrice, value_portfolio

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
    FIXED = datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc)
    # ACC-01 Fixture 장부(MAIN)와 분리한다. 같은 장부를 쓰면 이 점검이 남긴
    # 포지션이 ACC-01의 NAV에 섞여 들어간다.
    fund_id, book_id = repo.bootstrap(
        "ACC01-PAPER", "REPO-SELFCHECK",
        fund_name="Paper Fund (ACC-01 Fixture)", book_name="Repository Self-check Book")

    with repo.cursor() as cur:
        cur.execute("select instrument_id from reference.instruments order by instrument_id limit 1")
        instrument_id = cur.fetchone()[0]

    def journal_count() -> int:
        with repo.cursor() as cur:
            cur.execute("select count(*) from accounting.journals where fund_id=%s and book_id=%s",
                        (fund_id, book_id))
            return cur.fetchone()[0]

    class Fill:
        def __init__(self, qty, price, fee, tax, bfid):
            self.quantity, self.price = D(qty), D(price)
            self.fee, self.tax = D(fee), D(tax)
            self.event_time, self.broker_fill_id = FIXED, bfid
            self.fill_id = uuid5(NAMESPACE_OID, bfid)

    # 1. 계정과목 9개와 기준통화가 준비된다
    assert set(repo.account_ids(fund_id)) == set(ACCOUNT_NAMES)
    assert repo.base_currency(fund_id) == "KRW"

    # 2. book_id 하나로 Fund가 정해진다 (ledger_id == book_id 규약의 근거)
    assert repo.fund_of_book(book_id) == fund_id
    assert repo.fund_of_book(uuid5(NAMESPACE_OID, "없는-book")) is None

    # 3. 자본 납입이 DB에 남고, 복원한 원장이 그걸 본다
    repo.load(fund_id, book_id).post_capital(D("1000000000"), FIXED, "repo-selfcheck-capital")
    _, cash = repo.load(fund_id, book_id).rebuild()
    assert cash > 0, cash

    # 4. 멱등 - 같은 source_event_id로 다시 부르면 분개가 늘지 않는다
    before = journal_count()
    repo.load(fund_id, book_id).post_capital(D("1000000000"), FIXED, "repo-selfcheck-capital")
    assert journal_count() == before, "같은 원천 이벤트로 분개가 두 번 생겼다"

    # 5. 체결 분개와 Position projection
    led = repo.load(fund_id, book_id)
    led.post_fill(Fill("100", "70000", "1050", "0", "repo-selfcheck-buy"),
                  Side.BUY, instrument_id, Position(instrument_id))
    repo.save_projection(led)
    with repo.cursor() as cur:
        cur.execute("select quantity, average_cost from accounting.positions "
                    "where fund_id=%s and book_id=%s and instrument_id=%s",
                    (fund_id, book_id, instrument_id))
        assert cur.fetchone() == (D("100"), D("70000")), "Position projection이 틀렸다"

    # 6. DB가 차대균형을 독립적으로 강제한다 - 도메인 검증을 우회해도 막힌다.
    #    균형 잡힌 분개를 만든 뒤 리스트를 직접 흔들어 __post_init__을 지나친다.
    before_unbalanced = journal_count()
    unbalanced = Journal(
        journal_id=uuid5(NAMESPACE_OID, "repo-selfcheck-unbalanced"),
        fund_id=fund_id, book_id=book_id, event_type="selfcheck",
        source_event_id="repo-selfcheck-unbalanced", effective_at=FIXED,
        accounting_date=FIXED.date(),
        lines=[JournalLine(CASH, debit=D("100")), JournalLine(CAPITAL, credit=D("100"))],
    )
    unbalanced.lines[1] = JournalLine(CAPITAL, credit=D("99"))
    try:
        repo.insert_journal(unbalanced)
        raise AssertionError("불균형 분개가 DB에 POSTED로 들어갔다")
    except LedgerPersistenceError as exc:
        assert "balanced" in str(exc), exc
    assert journal_count() == before_unbalanced, "실패한 분개가 DRAFT로 남았다"

    # 7. Reversal - 원본은 남고 REVERSED로만 바뀐다.
    #    자본금 원본을 뒤집으면 재실행마다 현금이 달라지므로 전용 분개를 쓴다.
    led = repo.load(fund_id, book_id)
    led.post_capital(D("1"), FIXED, "repo-selfcheck-reversal-target")
    target = next(j for j in led.journals if j.source_event_id == "repo-selfcheck-reversal-target")
    if not any(j.reversal_of == target.journal_id for j in led.journals):
        led.reverse(target.journal_id, "자체 점검 정정")
    with repo.cursor() as cur:
        cur.execute("select status from accounting.journals where journal_id=%s", (target.journal_id,))
        assert cur.fetchone()[0] == "REVERSED"
        cur.execute("select count(*) from accounting.journals where reversal_of_journal_id=%s",
                    (target.journal_id,))
        assert cur.fetchone()[0] == 1, "반대 분개가 중복 생성됐다"
        cur.execute("select l.metadata->>'reason' from accounting.journal_lines l "
                    "join accounting.journals j on j.journal_id = l.journal_id "
                    "where j.reversal_of_journal_id=%s limit 1", (target.journal_id,))
        assert cur.fetchone()[0] == "자체 점검 정정", "정정 사유가 저장되지 않았다"

    # 8. POSTED 분개는 DB가 수정을 거부한다
    try:
        with repo.cursor() as cur:
            cur.execute("update accounting.journals set event_type='변조' where journal_id=%s",
                        (target.journal_id,))
        raise AssertionError("POSTED 분개가 수정됐다")
    except LedgerPersistenceError as exc:
        assert "immutable" in str(exc), exc

    # 9. 복원한 원장의 차대는 항상 0이다
    led = repo.load(fund_id, book_id)
    assert sum(led.trial_balance().values()) == ZERO, "복원한 원장의 차대가 안 맞는다"

    # 10. 스냅샷 저장 -> 복원. 같은 내용이면 행이 늘지 않는다
    snap = value_portfolio(
        led, {instrument_id: MarkPrice(instrument_id, D("75000"), FIXED, is_final=True)}, FIXED)
    assert snap.quality_status == "PASS", snap.quality_status
    snapshot_id = repo.save_snapshot(snap)
    assert repo.save_snapshot(snap) == snapshot_id, "같은 내용의 스냅샷이 두 건 생겼다"
    restored = repo.load_snapshots(fund_id, book_id)
    assert any(s.as_of == snap.as_of and s.nav == snap.nav for s in restored), \
        "저장한 스냅샷이 복원되지 않는다"

    print("ok - 원장 저장소 10개 영역 점검 통과 (실 DB 왕복, 차대균형·불변성은 DB가 강제)")
