#!/usr/bin/env python3
"""Risk Decision을 Canonical `risk.risk_decisions`에 기록하는 저장 계층.

Risk Engine은 순수 결정론 계산기로 유지한다. 이 모듈은 API 경계에서만
`RiskAssessment`를 영속화하며, 저장 실패를 조용히 삼키지 않는다.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from typing import Any
from uuid import UUID

from engine.risk_engine import RiskAssessment


class RiskDecisionPersistenceError(RuntimeError):
    """Canonical Risk Decision을 기록하지 못한 경우."""


@lru_cache(maxsize=1)
def _load_postgres_driver() -> tuple[Any, Any]:
    """PostgreSQL 저장을 실제로 사용할 때만 psycopg2를 로드한다."""
    try:
        from psycopg2.extras import Json
        from psycopg2.pool import ThreadedConnectionPool
    except ModuleNotFoundError as exc:
        raise RiskDecisionPersistenceError(
            "PostgreSQL Risk 저장에는 psycopg2-binary가 필요합니다. "
            "requirements.txt를 설치하거나 `uv pip install psycopg2-binary`를 실행하세요."
        ) from exc
    return Json, ThreadedConnectionPool


def _json_safe(value: Any) -> Any:
    """psycopg2 Json에 넣을 수 있도록 결정론적 JSON 값으로 변환한다."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return _json_safe(asdict(value))
    return value


class RiskDecisionRepository:
    """`risk.risk_decisions` 전용 저장소.

    `risk_request_id`가 이미 존재하는 정식 Risk 요청만 기록한다. 요청을
    여기서 임의로 생성하면 execution/governance 경계를 우회하게 되므로
    FK 오류를 그대로 영속화 실패로 처리한다.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str) -> RiskDecisionRepository:
        _, ThreadedConnectionPool = _load_postgres_driver()
        # minconn=0 - 유휴 커넥션을 잡지 않는다
        return cls(ThreadedConnectionPool(0, 4, dsn))

    def close(self) -> None:
        self._pool.closeall()

    def save(self, assessment: RiskAssessment) -> UUID:
        Json, _ = _load_postgres_driver()
        decision = assessment.decision
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into risk.risk_decisions (
                        risk_request_id, decision, approved_quantity, max_price,
                        approved_legs, aggregate_exposure, valid_until,
                        reason_codes, check_results, calculation_version,
                        input_hash, created_by_service
                    ) values (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    on conflict (risk_request_id, calculation_version) do nothing
                    returning risk_decision_id
                    """,
                    (
                        assessment.risk_request_id,
                        decision.verdict.value,
                        decision.approved_quantity,
                        decision.max_price,
                        Json(_json_safe(assessment.approved_legs)),
                        Json(_json_safe(assessment.aggregate_exposure)),
                        decision.expires_at,
                        [reason.value for reason in assessment.reason_codes],
                        Json(_json_safe(assessment.check_results)),
                        assessment.calculation_version,
                        assessment.input_hash,
                        decision.decided_by,
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        """
                        select risk_decision_id
                        from risk.risk_decisions
                        where risk_request_id = %s and calculation_version = %s
                        """,
                        (assessment.risk_request_id, assessment.calculation_version),
                    )
                    row = cur.fetchone()
                if row is None:
                    raise RiskDecisionPersistenceError(
                        "Risk Decision INSERT 후 조회할 수 없습니다"
                    )
            conn.commit()
            return row[0]
        except RiskDecisionPersistenceError:
            conn.rollback()
            raise
        except Exception as exc:  # psycopg2 예외를 API 경계에서 통일한다.
            conn.rollback()
            raise RiskDecisionPersistenceError(
                f"Canonical Risk Decision 기록 실패: {exc}"
            ) from exc
        finally:
            self._pool.putconn(conn)


if __name__ == "__main__":
    # 실제 DB 연결은 API/Worker가 담당한다. 비밀값을 자체 점검에서 읽지 않는다.
    print("ok RiskDecisionRepository import")
