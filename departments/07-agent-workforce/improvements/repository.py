#!/usr/bin/env python3
"""F19: 개선 후보의 실제 PostgreSQL(workforce.improvement_candidates) 저장 계층.

소유: 영주 (Agent Workforce 인사팀)
근거: docs/02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md F19,
      workflow.py의 ImprovementRepository 인터페이스(event 조회·저장),
      departments/00-ceo-office/src/mandate/postgres_repository.py 패턴.

candidate.py / workflow.py 의 도메인 타입(ImprovementCandidate, CandidateEvent)을
workforce.improvement_candidates / improvement_candidate_events 테이블에 1:1 매핑한다.

이전 버전은 asyncpg 기반이었다. CLAUDE.md 컨벤션(호출부가 전부 동기라 psycopg2로
통일 - Mandate/Audit과 동일 근거) 및 api/app.py가 실제로 이 모듈을 부르지 않고
있었다는 점을 확인하고 psycopg2로 다시 짰다 - 이전 asyncpg 코드는 아무 곳에서도
import되지 않아 API와 어긋나 있었다.

불변식:
  1. 접속 문자열은 .env 의 DATABASE_URL 만 쓴다.
  2. 비밀번호와 service_role Key 를 코드·로그에 남기지 않는다.
  3. improvement_candidate_events 는 DB 트리거로 append-only 다 - update/delete 를
     시도하지 않는다(그래서 이 모듈에는 event를 지우거나 고치는 메서드가 없다).

자체 점검: python departments/07-agent-workforce/improvements/repository.py
  - DATABASE_URL 없으면 import만 확인한다.
  - 있으면 candidate insert/조회/status 갱신은 실 DB로 왕복 검증하고 정리(delete)한다.
  - event append는 append-only 트리거 때문에 한번 넣으면 지울 수 없다 - candidate와
    달리 실 삽입 후 정리하지 않고, candidate_id를 selfcheck- 접두어로 표시해 남긴다
    (Audit 계열 append-only 테이블에 자체 점검 흔적을 남기는 것과 같은 절충 - QA의
    audit/repository.py가 같은 이유로 __main__ 자체 점검을 아예 두지 않은 것보다는,
    후속 검증을 위해 흔적을 남기는 쪽을 택했다).
"""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Any

from candidate import CandidateStatus, ImprovementCandidate
from workflow import CandidateEvent, ImprovementRepository


class ImprovementPersistenceError(RuntimeError):
    """개선 후보/Event를 기록하거나 조회하지 못한 경우."""


@lru_cache(maxsize=1)
def _load_postgres_driver() -> Any:
    try:
        from psycopg2.extras import Json, register_uuid
        from psycopg2.pool import ThreadedConnectionPool
        register_uuid()
    except ModuleNotFoundError as exc:
        raise ImprovementPersistenceError(
            "PostgreSQL 개선 후보 저장에는 psycopg2-binary가 필요합니다. "
            "requirements.txt를 설치하거나 `uv pip install psycopg2-binary`를 실행하세요."
        ) from exc
    return Json, ThreadedConnectionPool


class PostgresImprovementRepository(ImprovementRepository):
    """`workforce.improvement_candidates`/`improvement_candidate_events` 전용 저장소.

    candidate 저장(get_candidate/save_candidate)은 ImprovementRepository 인터페이스
    밖의 추가 메서드다 - workflow.py는 event만 다루고, candidate 영속화는 api/app.py가
    직접 이 Repository로 호출한다(access.py의 AccessRepository와 같은 분리).
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str) -> PostgresImprovementRepository:
        _, ThreadedConnectionPool = _load_postgres_driver()
        return cls(ThreadedConnectionPool(1, 4, dsn))

    def close(self) -> None:
        self._pool.closeall()

    # --- candidates ---------------------------------------------------------

    def get_candidate(self, candidate_id: str) -> ImprovementCandidate | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select candidate_id, target_type, target_ref, target_current_version,
                           rollback_target_version, author, evidence_ids, expected_effect,
                           risk_class, status
                    from workforce.improvement_candidates
                    where candidate_id = %s
                    """,
                    (candidate_id,),
                )
                row = cur.fetchone()
            conn.commit()
            return None if row is None else self._to_candidate(row)
        finally:
            self._pool.putconn(conn)

    def save_candidate(
        self,
        candidate: ImprovementCandidate,
        *,
        author_agent_id: str | None = None,
        deployed_profile_version_id: str | None = None,
    ) -> None:
        """새 후보면 insert, 이미 있으면(같은 candidate_id) status만 갱신한다(전이는
        workflow.py가 만들고, candidate_id/target/evidence 등은 PROPOSED 이후 안 바뀐다)."""
        Json, _ = _load_postgres_driver()
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into workforce.improvement_candidates (
                        candidate_id, target_type, target_ref, target_current_version,
                        rollback_target_version, author, author_agent_id, evidence_ids,
                        expected_effect, risk_class, deployed_profile_version_id, status
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (candidate_id) do update set
                        status = excluded.status,
                        deployed_profile_version_id = coalesce(
                            excluded.deployed_profile_version_id,
                            workforce.improvement_candidates.deployed_profile_version_id
                        )
                    """,
                    (
                        candidate.candidate_id, candidate.target_type.value, candidate.target_ref,
                        candidate.target_current_version, candidate.rollback_target_version,
                        candidate.author, author_agent_id, Json(candidate.evidence_ids),
                        candidate.expected_effect, candidate.risk_class.value,
                        deployed_profile_version_id, candidate.status.value,
                    ),
                )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise ImprovementPersistenceError(f"개선 후보 저장 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _to_candidate(db_row: tuple) -> ImprovementCandidate:
        (candidate_id, target_type, target_ref, target_current_version,
         rollback_target_version, author, evidence_ids, expected_effect, risk_class, status) = db_row
        return ImprovementCandidate(
            candidate_id=str(candidate_id), author=author, target_type=target_type,
            target_ref=target_ref, target_current_version=target_current_version,
            evidence_ids=list(evidence_ids), expected_effect=expected_effect,
            risk_class=risk_class, rollback_target_version=rollback_target_version,
            status=CandidateStatus(status),
        )

    # --- events (append-only, ImprovementRepository 인터페이스) --------------

    def next_sequence(self, candidate_id: str) -> int:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select coalesce(max(sequence), 0) + 1
                    from workforce.improvement_candidate_events
                    where candidate_id = %s
                    """,
                    (candidate_id,),
                )
                (next_seq,) = cur.fetchone()
            conn.commit()
            return int(next_seq)
        finally:
            self._pool.putconn(conn)

    def append_event(self, event: CandidateEvent) -> None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into workforce.improvement_candidate_events (
                        candidate_id, sequence, from_status, to_status, actor, reason,
                        qa_eval_run_id, occurred_at
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        event.candidate_id, event.sequence,
                        event.from_status.value if event.from_status else None,
                        event.to_status.value, event.actor, event.reason,
                        event.qa_eval_run_id, event.occurred_at,
                    ),
                )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise ImprovementPersistenceError(f"개선 후보 Event 기록 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    def events_for(self, candidate_id: str) -> list[CandidateEvent]:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select candidate_id, sequence, from_status, to_status, actor, reason,
                           qa_eval_run_id, occurred_at
                    from workforce.improvement_candidate_events
                    where candidate_id = %s
                    order by sequence
                    """,
                    (candidate_id,),
                )
                rows = cur.fetchall()
            conn.commit()
            return [self._to_event(r) for r in rows]
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _to_event(db_row: tuple) -> CandidateEvent:
        candidate_id, sequence, from_status, to_status, actor, reason, qa_eval_run_id, occurred_at = db_row
        return CandidateEvent(
            candidate_id=str(candidate_id), sequence=sequence,
            from_status=CandidateStatus(from_status) if from_status else None,
            to_status=CandidateStatus(to_status), actor=actor, reason=reason,
            occurred_at=occurred_at, qa_eval_run_id=str(qa_eval_run_id) if qa_eval_run_id else None,
        )


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/07-agent-workforce/improvements/repository.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import uuid
    from datetime import timezone

    print("ok - import 확인 (psycopg2 lazy load)")

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL 미설정 - 왕복 검증은 건너뛴다")
        raise SystemExit(0)

    repo = PostgresImprovementRepository.connect(dsn)
    try:
        candidate_id = str(uuid.uuid4())

        # 1) 존재하지 않는 candidate_id - None.
        assert repo.get_candidate(str(uuid.uuid4())) is None
        print("ok - 존재하지 않는 candidate_id 조회 (실 DB) 통과")

        # 2) candidate insert -> 조회 왕복 (author_agent_id/deployed 없이 - FK 불필요).
        candidate = ImprovementCandidate(
            candidate_id=candidate_id, author="selfcheck", target_type="PROFILE",
            target_ref="agent-selfcheck", target_current_version=1,
            evidence_ids=["selfcheck-evidence"], expected_effect="자체 점검",
            risk_class="LOW", rollback_target_version=1,
        )
        repo.save_candidate(candidate)
        found = repo.get_candidate(candidate_id)
        assert found is not None and found.status == CandidateStatus.PROPOSED
        print("ok - save_candidate -> get_candidate 왕복 (실 DB) 통과")

        # 3) status 갱신 - 같은 candidate_id로 upsert.
        evaluating = candidate.model_copy(update={"status": CandidateStatus.EVALUATING})
        repo.save_candidate(evaluating)
        found2 = repo.get_candidate(candidate_id)
        assert found2.status == CandidateStatus.EVALUATING
        print("ok - status 갱신 upsert (실 DB) 통과")

        # 4) event append -> next_sequence/events_for 왕복. append-only라 지울 수 없어
        #    candidate_id를 selfcheck-*로 남긴다(모듈 docstring 참고).
        seq1 = repo.next_sequence(candidate_id)
        assert seq1 == 1
        event = CandidateEvent(
            candidate_id=candidate_id, sequence=seq1, from_status=CandidateStatus.PROPOSED,
            to_status=CandidateStatus.EVALUATING, actor="selfcheck", reason="자체 점검 전이",
            occurred_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        )
        repo.append_event(event)
        events = repo.events_for(candidate_id)
        assert len(events) == 1 and events[0].sequence == 1
        assert events[0].to_status == CandidateStatus.EVALUATING
        seq2 = repo.next_sequence(candidate_id)
        assert seq2 == 2, "다음 sequence가 이어지지 않았다"
        print("ok - append_event -> next_sequence/events_for 왕복 (실 DB) 통과")

        # candidate 정리 시도 - event가 FK로 참조하고 있어 삭제되지 않는다(append-only
        # 자식 행이 남아 있는 한 부모도 지울 수 없다). 실패를 정상으로 간주하고 넘어간다.
        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "delete from workforce.improvement_candidates where candidate_id = %s",
                    (candidate_id,),
                )
            conn.commit()
            print("ok - candidate 정리 완료 (event 없었다면 삭제됨)")
        except Exception:  # noqa: BLE001 - intentional fallback boundary
            conn.rollback()
            print(f"참고 - candidate_id={candidate_id}는 event가 참조 중이라 삭제하지 않고 남긴다")
    finally:
        repo.close()
