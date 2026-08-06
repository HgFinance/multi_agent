#!/usr/bin/env python3
"""HR-02 P0-3: ACTIVE 전이 증거(QA Eval, CEO 승인) 조회 전용 Repository.

소유: 영주 (Agent Workforce 인사팀)
근거: docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md v2.0 P0-3,
      roster.py verify_activation_evidence()

roster.py는 이 조회 결과(문자열 status/decision)만 받아 판정한다 - 이 모듈 자체는
"통과/거절"을 모른다(approval.py/actor_identity.py와 같은 조회-판정 분리 원칙).

audit.eval_runs와 governance.approvals는 각각 QA/감사본부와 CEO Office 소유 테이블이다 -
이 모듈은 그 스키마를 읽기만 하고 쓰지 않는다(HR은 QA Eval도 CEO 승인도 스스로 못
만든다는 게 애초에 이 검증이 강제하려는 권한 분리다).

자체 점검: python departments/07-agent-workforce/roster/activation_evidence.py
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any


class ActivationEvidenceRepository:
    """조회 인터페이스. 실제 구현은 audit.eval_runs/governance.approvals를 읽는다."""

    def get_eval_run_status(self, eval_run_id: str, profile_version_id: str) -> str | None:
        """eval_run_id가 이 profile_version_id를 candidate로 하는 실제 행을 가리키면
        audit.eval_runs.status를 돌려준다. 없거나 다른 Version을 가리키면 None -
        "잘못된 eval_run_id를 다른 Version 증거로 재사용"을 이 매칭이 막는다."""
        raise NotImplementedError

    def get_ceo_approval_decision(self, approval_id: str, profile_version_id: str) -> str | None:
        """approval_id가 required_role=CEO, object_type=AGENT_PROFILE_VERSION,
        object_id=profile_version_id인 실제 governance.approvals 행을 가리키면
        decision을 돌려준다. 없거나 다른 대상/역할이면 None."""
        raise NotImplementedError


class InMemoryActivationEvidenceRepository(ActivationEvidenceRepository):
    """테스트·개발용. seed_eval_run()/seed_ceo_approval()로 미리 등록해둔 것만 실재로 본다."""

    def __init__(self) -> None:
        self._eval_runs: dict[tuple[str, str], str] = {}
        self._ceo_approvals: dict[tuple[str, str], str] = {}

    def seed_eval_run(self, eval_run_id: str, profile_version_id: str, status: str) -> None:
        self._eval_runs[(eval_run_id, profile_version_id)] = status

    def seed_ceo_approval(self, approval_id: str, profile_version_id: str, decision: str) -> None:
        self._ceo_approvals[(approval_id, profile_version_id)] = decision

    def get_eval_run_status(self, eval_run_id: str, profile_version_id: str) -> str | None:
        return self._eval_runs.get((eval_run_id, profile_version_id))

    def get_ceo_approval_decision(self, approval_id: str, profile_version_id: str) -> str | None:
        return self._ceo_approvals.get((approval_id, profile_version_id))


class ActivationEvidencePersistenceError(RuntimeError):
    """audit.eval_runs/governance.approvals 조회에 실패한 경우."""


@lru_cache(maxsize=1)
def _load_postgres_driver() -> Any:
    try:
        from psycopg2.pool import ThreadedConnectionPool
    except ModuleNotFoundError as exc:
        raise ActivationEvidencePersistenceError(
            "PostgreSQL 조회에는 psycopg2-binary가 필요합니다. "
            "requirements.txt를 설치하거나 `uv pip install psycopg2-binary`를 실행하세요."
        ) from exc
    return ThreadedConnectionPool


class PostgresActivationEvidenceRepository(ActivationEvidenceRepository):
    """`audit.eval_runs`/`governance.approvals`에 대한 읽기 전용 구현."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str) -> PostgresActivationEvidenceRepository:
        ThreadedConnectionPool = _load_postgres_driver()
        return cls(ThreadedConnectionPool(1, 4, dsn))

    def close(self) -> None:
        self._pool.closeall()

    def get_eval_run_status(self, eval_run_id: str, profile_version_id: str) -> str | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select status from audit.eval_runs "
                    "where eval_run_id = %s and candidate_profile_version_id = %s",
                    (eval_run_id, profile_version_id),
                )
                row = cur.fetchone()
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - 형식이 잘못된 uuid 등도 여기서 거절로 번역한다.
            conn.rollback()
            raise ActivationEvidencePersistenceError(f"eval_run_id 조회 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)
        return row[0] if row else None

    def get_ceo_approval_decision(self, approval_id: str, profile_version_id: str) -> str | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select decision from governance.approvals "
                    "where approval_id = %s and object_id = %s "
                    "and object_type = 'AGENT_PROFILE_VERSION' and required_role = 'CEO'",
                    (approval_id, profile_version_id),
                )
                row = cur.fetchone()
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            raise ActivationEvidencePersistenceError(f"ceo_approval_id 조회 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)
        return row[0] if row else None


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/07-agent-workforce/roster/activation_evidence.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    repo = InMemoryActivationEvidenceRepository()

    # 1) seed 안 한 조합은 None - 실재하지 않는 것으로 취급된다.
    assert repo.get_eval_run_status("eval-1", "pv-1") is None
    assert repo.get_ceo_approval_decision("appr-1", "pv-1") is None

    # 2) seed한 조합만 실재 값을 돌려준다.
    repo.seed_eval_run("eval-1", "pv-1", "COMPLETED")
    assert repo.get_eval_run_status("eval-1", "pv-1") == "COMPLETED"

    repo.seed_ceo_approval("appr-1", "pv-1", "APPROVED")
    assert repo.get_ceo_approval_decision("appr-1", "pv-1") == "APPROVED"

    # 3) 다른 profile_version_id로는 매칭되지 않는다 - 증거 재사용을 막는 핵심 동작.
    assert repo.get_eval_run_status("eval-1", "pv-2") is None
    assert repo.get_ceo_approval_decision("appr-1", "pv-2") is None

    print("ok - activation_evidence 자체 점검 6개 시나리오 통과")

    import os

    from dotenv import load_dotenv

    load_dotenv()
    dsn = os.environ.get("GOVERNANCE_WORKFORCE_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL 미설정 - 실 DB 조회 검증은 건너뛴다")
        raise SystemExit(0)

    pg_repo = PostgresActivationEvidenceRepository.connect(dsn)
    try:
        import uuid

        missing = str(uuid.uuid4())
        assert pg_repo.get_eval_run_status(missing, missing) is None
        assert pg_repo.get_ceo_approval_decision(missing, missing) is None
        print("ok - 실 DB 조회 검증 통과 (존재하지 않는 uuid 조합은 None)")
    finally:
        pg_repo.close()
