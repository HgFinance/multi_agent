#!/usr/bin/env python3
"""F19: 개선 후보의 asyncpg 실 저장 계층.

소유: 영주 (Agent Workforce 인사팀)
근거: docs/02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md F19,
      docs/database/README.md 7절(적용 방법)

candidate.py / workflow.py 의 도메인 타입(ImprovementCandidate, CandidateEvent)을
workforce.improvement_candidates / improvement_candidate_events 테이블에 1:1 매핑한다.

불변식:
  1. 접속 문자열은 .env 의 DATABASE_URL 만 쓴다.
  2. 비밀번호와 service_role Key 를 코드·로그에 남기지 않는다 (TEAM_YOUNGJU 7.2, 10.3).
  3. 전이 Event 는 append 만 한다 (workforce.improvement_candidate_events 는 DB 트리거로도 강제).

이 모듈은 live DB 를 요구하므로 __main__ 자체 점검이 없다. 검증은 DB 적용 후 통합 테스트로 한다.
"""
from __future__ import annotations

import json
from datetime import datetime

import asyncpg

from candidate import CandidateStatus, ImprovementCandidate
from workflow import CandidateEvent


class PostgresImprovementRepository:
    """asyncpg 기반 F19 Repository. Pool 을 주입받거나 connect() 로 생성한다."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "PostgresImprovementRepository":
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    # --- candidates ---

    async def insert_candidate(
        self,
        candidate: ImprovementCandidate,
        *,
        author_agent_id: str | None = None,
        deployed_profile_version_id: str | None = None,
    ) -> str:
        """개선 후보 insert. candidate_id 는 uuid 여야 한다 (DDL: uuid)."""
        row = await self._pool.fetchrow(
            """
            insert into workforce.improvement_candidates
              (candidate_id, target_type, target_ref, target_current_version,
               rollback_target_version, author, author_agent_id, evidence_ids,
               expected_effect, risk_class, deployed_profile_version_id, status)
            values ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12)
            returning candidate_id
            """,
            candidate.candidate_id,
            candidate.target_type.value,
            candidate.target_ref,
            candidate.target_current_version,
            candidate.rollback_target_version,
            candidate.author,
            author_agent_id,
            json.dumps(candidate.evidence_ids),
            candidate.expected_effect,
            candidate.risk_class.value,
            deployed_profile_version_id,
            candidate.status.value,
        )
        return str(row["candidate_id"])

    async def load_candidate(self, candidate_id: str) -> ImprovementCandidate | None:
        row = await self._pool.fetchrow(
            """
            select candidate_id, target_type, target_ref, target_current_version,
                   rollback_target_version, author, evidence_ids, expected_effect,
                   risk_class, status
            from workforce.improvement_candidates
            where candidate_id = $1
            """,
            candidate_id,
        )
        if row is None:
            return None
        evidence = row["evidence_ids"]
        if isinstance(evidence, str):
            evidence = json.loads(evidence)
        return ImprovementCandidate(
            candidate_id=str(row["candidate_id"]),
            author=row["author"],
            target_type=row["target_type"],
            target_ref=row["target_ref"],
            target_current_version=row["target_current_version"],
            evidence_ids=list(evidence),
            expected_effect=row["expected_effect"],
            risk_class=row["risk_class"],
            rollback_target_version=row["rollback_target_version"],
            status=CandidateStatus(row["status"]),
        )

    async def set_status(self, candidate_id: str, status: CandidateStatus) -> None:
        await self._pool.execute(
            "update workforce.improvement_candidates set status = $2 where candidate_id = $1",
            candidate_id,
            status.value,
        )

    # --- events (append-only) ---

    async def next_sequence(self, candidate_id: str) -> int:
        val = await self._pool.fetchval(
            """
            select coalesce(max(sequence), 0) + 1
            from workforce.improvement_candidate_events
            where candidate_id = $1
            """,
            candidate_id,
        )
        return int(val)

    async def append_event(self, event: CandidateEvent) -> str:
        row = await self._pool.fetchrow(
            """
            insert into workforce.improvement_candidate_events
              (candidate_id, sequence, from_status, to_status, actor, reason,
               qa_eval_run_id, occurred_at)
            values ($1, $2, $3, $4, $5, $6, $7, $8)
            returning event_id
            """,
            event.candidate_id,
            event.sequence,
            event.from_status.value if event.from_status else None,
            event.to_status.value,
            event.actor,
            event.reason,
            event.qa_eval_run_id,
            event.occurred_at,
        )
        return str(row["event_id"])

    async def events_for(self, candidate_id: str) -> list[dict]:
        rows = await self._pool.fetch(
            """
            select sequence, from_status, to_status, actor, reason,
                   qa_eval_run_id, occurred_at
            from workforce.improvement_candidate_events
            where candidate_id = $1
            order by sequence
            """,
            candidate_id,
        )
        return [dict(r) for r in rows]
