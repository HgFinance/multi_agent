#!/usr/bin/env python3
"""Y2 위원회 — committee.py의 CommitteeRepository 계약에 대한 실제 PostgreSQL 구현.

소유: 영주 (CEO Office)
근거: supabase/migrations/20260729000200_governance_workforce.sql
      (governance.committee_sessions/committee_votes/committee_decisions)

committee.py의 Quorum·SoD 판정은 여기서 재구현하지 않는다 - 이 모듈은 SQL 왕복만 담당한다.

불변식:
  1. `save_vote`는 순수 insert다. 부서당 1표 제한(committee.py 불변식 1)은 호출부가
     `list_votes()`로 먼저 조회해 `find_vote_by_department()`로 검사한 뒤 호출한다 -
     DDL의 unique(session_id, department, voter_agent_id)는 voter_agent_id가 NULL이면
     막지 못하므로(위 committee.py 참고) 이 계층이 마지막 방어선이 아니다.
  2. `get_case_owner_department`는 governance.cases.owner_department를 그대로 읽는다 -
     GOV-02 Case Root가 이미 만든 테이블이라 새 스키마가 필요 없다.

자체 점검: python departments/00-ceo-office/src/committee/postgres_committee_repository.py
  - DATABASE_URL(또는 GOVERNANCE_WORKFORCE_DATABASE_URL) 없으면 import만 확인한다.
  - 있으면 실제 accounting.funds 행과, GOV-02 Case Root 자체 점검이 남긴 Case 1건을
    빌려 세션 생성 -> SoD 확인 -> 투표 3건 -> 종료(Decision) 왕복을 검증한 뒤 정리한다.
    committee_sessions/votes/decisions에는 append-only 트리거가 없다(2026-08-04 실측).
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from committee import (
    CommitteeDecision,
    CommitteeDecisionRecord,
    CommitteeRepository,
    CommitteeSession,
    QuorumPolicy,
    SessionStatus,
    Vote,
    VoteDecision,
)


class CommitteePersistenceError(RuntimeError):
    """위원회 세션/투표/결정 저장·조회에 실패한 경우."""


@lru_cache(maxsize=1)
def _load_postgres_driver() -> Any:
    try:
        from psycopg2.extras import register_uuid
        from psycopg2.pool import ThreadedConnectionPool
    except ModuleNotFoundError as exc:
        raise CommitteePersistenceError(
            "PostgreSQL 위원회 저장에는 psycopg2-binary가 필요합니다. "
            "requirements.txt를 설치하거나 `uv pip install psycopg2-binary`를 실행하세요."
        ) from exc
    register_uuid()
    return ThreadedConnectionPool


class PostgresCommitteeRepository(CommitteeRepository):
    """`governance.committee_sessions/committee_votes/committee_decisions`에 대한 실제 구현."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str) -> PostgresCommitteeRepository:
        ThreadedConnectionPool = _load_postgres_driver()
        return cls(ThreadedConnectionPool(1, 4, dsn))

    def close(self) -> None:
        self._pool.closeall()

    def save_session(self, session: CommitteeSession) -> None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into governance.committee_sessions
                      (session_id, fund_id, case_id, committee_type, quorum_policy,
                       opened_at, closed_at, status, trace_id)
                    values (%(session_id)s, %(fund_id)s, %(case_id)s, %(committee_type)s,
                            %(quorum_policy)s::jsonb, %(opened_at)s, %(closed_at)s,
                            %(status)s, %(trace_id)s)
                    on conflict (session_id) do update set
                      status = excluded.status, closed_at = excluded.closed_at
                    """,
                    {
                        "session_id": session.session_id, "fund_id": session.fund_id,
                        "case_id": session.case_id, "committee_type": session.committee_type,
                        "quorum_policy": json.dumps(session.quorum_policy.to_jsonb()),
                        "opened_at": session.opened_at, "closed_at": session.closed_at,
                        "status": session.status.value, "trace_id": session.trace_id,
                    },
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def get_session(self, session_id: str) -> CommitteeSession | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select session_id, fund_id, case_id, committee_type, quorum_policy,
                           opened_at, closed_at, status, trace_id
                    from governance.committee_sessions where session_id = %s
                    """,
                    (session_id,),
                )
                row = cur.fetchone()
            conn.commit()
            return None if row is None else self._to_session(row)
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _to_session(db_row: tuple) -> CommitteeSession:
        (session_id, fund_id, case_id, committee_type, quorum_policy,
         opened_at, closed_at, status, trace_id) = db_row
        return CommitteeSession(
            session_id=str(session_id), fund_id=str(fund_id),
            case_id=str(case_id) if case_id else None, committee_type=committee_type,
            quorum_policy=QuorumPolicy.from_jsonb(quorum_policy),
            status=SessionStatus(status), opened_at=opened_at, closed_at=closed_at,
            trace_id=str(trace_id),
        )

    def save_vote(self, vote: Vote) -> None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into governance.committee_votes
                      (vote_id, session_id, department, voter_agent_id, decision,
                       conditions, artifact_ids, rationale, voted_at)
                    values (%(vote_id)s, %(session_id)s, %(department)s, %(voter_agent_id)s,
                            %(decision)s, %(conditions)s::jsonb, %(artifact_ids)s,
                            %(rationale)s, %(voted_at)s)
                    """,
                    {
                        "vote_id": vote.vote_id, "session_id": vote.session_id,
                        "department": vote.department, "voter_agent_id": vote.voter_agent_id,
                        "decision": vote.decision.value,
                        "conditions": json.dumps(vote.conditions or {}),
                        "artifact_ids": list(vote.artifact_ids), "rationale": vote.rationale,
                        "voted_at": vote.voted_at,
                    },
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def list_votes(self, session_id: str) -> list[Vote]:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select vote_id, session_id, department, voter_agent_id, decision,
                           conditions, artifact_ids, rationale, voted_at
                    from governance.committee_votes where session_id = %s order by voted_at
                    """,
                    (session_id,),
                )
                rows = cur.fetchall()
            conn.commit()
            return [self._to_vote(r) for r in rows]
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _to_vote(db_row: tuple) -> Vote:
        (vote_id, session_id, department, voter_agent_id, decision,
         conditions, artifact_ids, rationale, voted_at) = db_row
        return Vote(
            vote_id=str(vote_id), session_id=str(session_id), department=department,
            voter_agent_id=str(voter_agent_id) if voter_agent_id else None,
            decision=VoteDecision(decision), voted_at=voted_at, conditions=conditions or {},
            artifact_ids=tuple(str(a) for a in (artifact_ids or ())), rationale=rationale,
        )

    def save_decision(self, decision: CommitteeDecisionRecord) -> None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into governance.committee_decisions
                      (committee_decision_id, session_id, decision, scope, conditions,
                       valid_until, dissent, approvals, decided_at)
                    values (%(id)s, %(session_id)s, %(decision)s, %(scope)s::jsonb,
                            %(conditions)s::jsonb, %(valid_until)s, %(dissent)s::jsonb,
                            %(approvals)s::jsonb, %(decided_at)s)
                    """,
                    {
                        "id": decision.committee_decision_id, "session_id": decision.session_id,
                        "decision": decision.decision.value,
                        "scope": json.dumps(decision.scope or {}),
                        "conditions": json.dumps(decision.conditions or {}),
                        "valid_until": decision.valid_until,
                        "dissent": json.dumps(list(decision.dissent)),
                        "approvals": json.dumps(list(decision.approvals)),
                        "decided_at": decision.decided_at,
                    },
                )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise CommitteePersistenceError(f"위원회 결정 기록 실패: {exc}") from exc
        finally:
            self._pool.putconn(conn)

    def get_case_owner_department(self, case_id: str) -> str | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select owner_department from governance.cases where case_id = %s", (case_id,)
                )
                row = cur.fetchone()
            conn.commit()
            return row[0] if row else None
        finally:
            self._pool.putconn(conn)


# ---------------------------------------------------------------------------
# 자체 점검 (python .../src/committee/postgres_committee_repository.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import uuid
    from datetime import datetime, timedelta, timezone

    from committee import cast_vote, close_session, evaluate_quorum, open_session

    print("ok - import 확인 (psycopg2 lazy load)")

    dsn = os.environ.get("GOVERNANCE_WORKFORCE_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL 미설정 - 왕복 검증은 건너뛴다")
        raise SystemExit(0)

    repo = PostgresCommitteeRepository.connect(dsn)
    session_id = str(uuid.uuid4())
    try:
        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("select fund_id from accounting.funds limit 1")
                fund_row = cur.fetchone()
                cur.execute(
                    "select case_id, owner_department from governance.cases "
                    "where created_by = 'selfcheck' order by created_at limit 1"
                )
                case_row = cur.fetchone()
        finally:
            repo._pool.putconn(conn)

        if fund_row is None:
            print("SKIP - accounting.funds가 비어 있어 건너뛴다")
            raise SystemExit(0)
        fund_id = str(fund_row[0])
        case_id = str(case_row[0]) if case_row else None
        case_owner = case_row[1] if case_row else None
        if case_id:
            print(f"ok - Case Root 자체 점검이 남긴 Case 차용 (owner_department={case_owner})")
        else:
            print("SKIP - Case Root 자체 점검 흔적이 없다(먼저 postgres_case_repository.py "
                  "자체 점검을 실행하면 SoD 경로까지 검증된다) - Case 없이 진행한다")

        t0 = datetime(2026, 8, 4, tzinfo=timezone.utc)
        policy = QuorumPolicy(
            required_departments=("ceo-agent", "risk-management", "qa-department"),
            veto_departments=("risk-management",), approval_threshold=2,
        )

        # 1) 세션 열기 -> 저장 -> 조회 왕복.
        session = open_session(
            session_id=session_id, fund_id=fund_id, committee_type="STRATEGY_PLANNING",
            quorum_policy=policy, opened_at=t0, trace_id=str(uuid.uuid4()), case_id=case_id,
        )
        repo.save_session(session)
        loaded = repo.get_session(session_id)
        assert loaded is not None and loaded.status is SessionStatus.OPEN
        assert loaded.quorum_policy == policy
        print("ok - 세션 생성/조회 (실 DB) 통과 - quorum_policy 왕복 확인")

        # 2) SoD - Case owner_department는 투표 불가 (Case가 있을 때만 검증).
        if case_id and case_owner:
            try:
                cast_vote(loaded, [], vote_id=str(uuid.uuid4()), department=case_owner,
                         decision=VoteDecision.APPROVE, voted_at=t0,
                         case_owner_department=repo.get_case_owner_department(case_id))
                raise AssertionError("Case 소유 부서의 자기 투표가 통과했다")
            except AssertionError:
                raise
            except Exception:
                pass
            print(f"ok - SoD 확인 (실 DB) - Case owner_department={case_owner!r} 투표 차단")

        # 3) 투표 3건 적재 (부서당 1표는 호출부 책임 - committee.py cast_vote가 이미 검증).
        votes: list[Vote] = []
        for dept, decision in (
            ("ceo-agent", VoteDecision.APPROVE),
            ("risk-management", VoteDecision.APPROVE),
            ("qa-department", VoteDecision.CONDITIONAL),
        ):
            v = cast_vote(loaded, votes, vote_id=str(uuid.uuid4()), department=dept,
                         decision=decision, voted_at=t0, conditions={"note": "selfcheck"})
            votes.append(v)
            repo.save_vote(v)
        loaded_votes = repo.list_votes(session_id)
        assert len(loaded_votes) == 3
        assert {v.department for v in loaded_votes} == {"ceo-agent", "risk-management", "qa-department"}
        print("ok - 투표 3건 적재/조회 (실 DB) 통과")

        # 4) 종료 - evaluate_quorum 결과를 그대로 기록, 실 DB에서 재조회 확인.
        result = evaluate_quorum(policy, loaded_votes)
        assert result.met is True and result.decision is CommitteeDecision.CONDITIONAL, result
        closed, decision_row = close_session(
            loaded, loaded_votes, committee_decision_id=str(uuid.uuid4()),
            at=t0 + timedelta(hours=1), scope={"selfcheck": True},
        )
        repo.save_session(closed)
        repo.save_decision(decision_row)
        reloaded = repo.get_session(session_id)
        assert reloaded.status is SessionStatus.DECIDED and reloaded.closed_at is not None
        print("ok - 세션 종료 + Decision 기록 (실 DB) 통과 - CONDITIONAL 확인")
    finally:
        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "delete from governance.committee_decisions where session_id = %s", (session_id,)
                )
                cur.execute(
                    "delete from governance.committee_votes where session_id = %s", (session_id,)
                )
                cur.execute(
                    "delete from governance.committee_sessions where session_id = %s", (session_id,)
                )
            conn.commit()
        finally:
            repo._pool.putconn(conn)
        repo.close()
        print("ok - 자체 점검 행 정리 완료")
