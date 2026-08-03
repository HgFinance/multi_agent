#!/usr/bin/env python3
"""HR-02: roster.py의 RosterRepository 계약에 대한 실제 PostgreSQL 구현.

소유: 영주 (Agent Workforce 인사팀)
근거: docs/02-engineering/GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md 3.1절,
      supabase/migrations/20260729000200_governance_workforce.sql
      (workforce.agent_profiles/agent_profile_versions/role_templates/departments/models)

roster.py의 도메인 규칙(불변식 1·2, compute_artifact_hash)은 여기서 재구현하지 않고
그대로 가져다 쓴다 - 이 모듈은 오직 SQL 왕복만 담당한다.

불변식:
  1. `submit_profile`은 다음 Version 번호를
     `coalesce(max(version), 0) + 1`로 같은 insert 문 안에서 계산한다 - Read-then-Write
     로 번호를 매기지 않는 이유는 그 사이 경합을 줄이기 위해서다(완전한 동시성 보장은
     아니다 - 이 저장소의 다른 Repository들과 동일하게 낙관적 수준).
  2. `list_roster`/`get_agent`는 agent_profiles.current_version과 정확히 일치하는
     agent_profile_versions 행을 current_profile_version으로 조인한다 - 여러 Version이
     있어도 "최신 제출"만 대표로 노출한다.
  3. `change_status`는 employment_status만 결정론적으로 전이한다. profile_version_id로
     지정된 Version의 status는, EmploymentStatus와 ProfileVersionStatus 양쪽에 이름이
     동일하게 존재하는 경우(ACTIVE/SUSPENDED/RETIRED)에만 같이 갱신한다 - PROBATION은
     ProfileVersionStatus에 대응 값이 없어 추측하지 않는다.

자체 점검: python departments/07-agent-workforce/roster/postgres_roster_repository.py
  - DATABASE_URL 없으면 import만 확인한다.
  - 있으면 실제 workforce.agent_profiles 행(HR-04, CANDIDATE 상태)을 찾아 Profile Version
    제출 -> Roster 재조회 -> employment_status 전이까지 왕복 검증한 뒤 삽입한 Version 행만
    정리(delete)한다. agent_profiles.current_version/employment_status는 자체 점검이
    끝나면 원래 값으로 되돌린다(공유 개발 DB에 흔적을 남기지 않기 위해).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from roster import (
    AgentNotFoundError,
    AgentSummary,
    EmploymentStatus,
    ModelRef,
    ProfileVersionRow,
    ProfileVersionStatus,
    ProfileVersionSubmission,
    ProfileVersionSummary,
    RosterRepository,
    compute_artifact_hash,
    validate_status_change,
)


class RosterQueryError(RuntimeError):
    """Roster 조회/갱신에 실패한 경우."""


# EmploymentStatus <-> ProfileVersionStatus 이름이 동일한 값만 연동한다 (불변식 3).
_SHARED_STATUS_NAMES = {EmploymentStatus.ACTIVE, EmploymentStatus.SUSPENDED, EmploymentStatus.RETIRED}

_ROSTER_SELECT = """
    select
      ap.agent_id, ap.employee_code, ap.display_name,
      d.department_code, rt.role_code,
      ap.employment_status, ap.current_version,
      ap.owner_user_id, ap.backup_owner_user_id,
      pv.profile_version_id, pv.version, pv.memory_namespace, pv.status,
      m.provider, m.model_name, m.model_version
    from workforce.agent_profiles ap
    join workforce.departments d on d.department_id = ap.department_id
    join workforce.role_templates rt on rt.role_id = ap.role_id
    left join workforce.agent_profile_versions pv
      on pv.agent_id = ap.agent_id and pv.version = ap.current_version
    left join workforce.models m on m.model_id = pv.model_id
"""


@lru_cache(maxsize=1)
def _load_postgres_driver() -> Any:
    try:
        from psycopg2.pool import ThreadedConnectionPool
    except ModuleNotFoundError as exc:
        raise RosterQueryError(
            "PostgreSQL Roster 조회에는 psycopg2-binary가 필요합니다. "
            "requirements.txt를 설치하거나 `uv pip install psycopg2-binary`를 실행하세요."
        ) from exc
    return ThreadedConnectionPool


class PostgresRosterRepository(RosterRepository):
    """`workforce.agent_profiles`/`agent_profile_versions` 등에 대한 실제 구현."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, dsn: str) -> PostgresRosterRepository:
        ThreadedConnectionPool = _load_postgres_driver()
        return cls(ThreadedConnectionPool(1, 4, dsn))

    def close(self) -> None:
        self._pool.closeall()

    def list_roster(self) -> list[AgentSummary]:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(_ROSTER_SELECT + " order by ap.employee_code")
                rows = cur.fetchall()
            conn.commit()
            return [self._to_agent_summary(r) for r in rows]
        finally:
            self._pool.putconn(conn)

    def get_agent(self, agent_id: str) -> AgentSummary | None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(_ROSTER_SELECT + " where ap.agent_id = %s", (agent_id,))
                row = cur.fetchone()
            conn.commit()
            return None if row is None else self._to_agent_summary(row)
        finally:
            self._pool.putconn(conn)

    @staticmethod
    def _to_agent_summary(db_row: tuple) -> AgentSummary:
        (agent_id, employee_code, display_name, department_code, role_code,
         employment_status, current_version, owner_user_id, backup_owner_user_id,
         profile_version_id, version, memory_namespace, pv_status,
         provider, model_name, model_version) = db_row

        current_profile_version = None
        if profile_version_id is not None:
            current_profile_version = ProfileVersionSummary(
                profile_version_id=str(profile_version_id), version=version,
                model=ModelRef(provider=provider, model_name=model_name, model_version=model_version),
                memory_namespace=memory_namespace, status=ProfileVersionStatus(pv_status),
            )

        return AgentSummary(
            agent_id=str(agent_id), employee_code=employee_code, display_name=display_name,
            department_code=department_code, role_code=role_code,
            employment_status=EmploymentStatus(employment_status), current_version=current_version,
            current_profile_version=current_profile_version,
            owner_user_id=str(owner_user_id) if owner_user_id else None,
            backup_owner_user_id=str(backup_owner_user_id) if backup_owner_user_id else None,
        )

    def submit_profile(self, agent_id: str, submission: ProfileVersionSubmission) -> ProfileVersionRow:
        artifact_hash = compute_artifact_hash(submission)
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("select 1 from workforce.agent_profiles where agent_id = %s", (agent_id,))
                if cur.fetchone() is None:
                    raise AgentNotFoundError(f"agent_id={agent_id}를 찾을 수 없다")

                cur.execute(
                    """
                    insert into workforce.agent_profile_versions
                      (agent_id, version, model_id, prompt_artifact_path, skill_manifest,
                       tool_allowlist, data_scopes, memory_namespace, token_budget, sla,
                       eval_requirements, forbidden_actions, artifact_hash, effective_from,
                       effective_to, status)
                    values (
                      %(agent_id)s,
                      coalesce(
                        (select max(version) from workforce.agent_profile_versions where agent_id = %(agent_id)s),
                        0
                      ) + 1,
                      %(model_id)s, %(prompt_artifact_path)s, %(skill_manifest)s::jsonb,
                      %(tool_allowlist)s::jsonb, %(data_scopes)s::jsonb, %(memory_namespace)s,
                      %(token_budget)s::jsonb, %(sla)s::jsonb, %(eval_requirements)s::jsonb,
                      %(forbidden_actions)s::jsonb, %(artifact_hash)s, %(effective_from)s,
                      %(effective_to)s, 'DRAFT'
                    )
                    returning profile_version_id, version
                    """,
                    _submission_params(agent_id, submission, artifact_hash),
                )
                profile_version_id, version = cur.fetchone()

                cur.execute(
                    "update workforce.agent_profiles set current_version = %s, updated_at = now() "
                    "where agent_id = %s",
                    (version, agent_id),
                )
            conn.commit()
            return ProfileVersionRow(
                profile_version_id=str(profile_version_id), agent_id=agent_id, version=version,
                submission=submission, artifact_hash=artifact_hash, status=ProfileVersionStatus.DRAFT,
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def change_status(self, agent_id: str, *, to_status: EmploymentStatus, at) -> None:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select 1 from workforce.agent_profiles where agent_id = %s", (agent_id,),
                )
                if cur.fetchone() is None:
                    raise AgentNotFoundError(f"agent_id={agent_id}를 찾을 수 없다")

                cur.execute(
                    "update workforce.agent_profiles set employment_status = %s, updated_at = now() "
                    "where agent_id = %s",
                    (to_status.value, agent_id),
                )
                if to_status in _SHARED_STATUS_NAMES:
                    cur.execute(
                        "update workforce.agent_profile_versions set status = %s "
                        "where agent_id = %s and version = "
                        "(select current_version from workforce.agent_profiles where agent_id = %s)",
                        (to_status.value, agent_id, agent_id),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)


def _submission_params(agent_id: str, submission: ProfileVersionSubmission, artifact_hash: str) -> dict:
    import json

    return {
        "agent_id": agent_id,
        "model_id": submission.model_id,
        "prompt_artifact_path": submission.prompt_artifact_path,
        "skill_manifest": json.dumps(submission.skill_manifest),
        "tool_allowlist": json.dumps(submission.tool_allowlist),
        "data_scopes": json.dumps(submission.data_scopes),
        "memory_namespace": submission.memory_namespace,
        "token_budget": json.dumps(submission.token_budget),
        "sla": json.dumps(submission.sla),
        "eval_requirements": json.dumps(submission.eval_requirements),
        "forbidden_actions": json.dumps(submission.forbidden_actions),
        "artifact_hash": artifact_hash,
        "effective_from": submission.effective_from,
        "effective_to": submission.effective_to,
    }


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/07-agent-workforce/roster/postgres_roster_repository.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    from datetime import datetime, timezone

    print("ok - import 확인 (psycopg2 lazy load)")

    dsn = os.environ.get("GOVERNANCE_WORKFORCE_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL 미설정 - 왕복 검증은 건너뛴다")
        raise SystemExit(0)

    repo = PostgresRosterRepository.connect(dsn)
    try:
        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select agent_id, employment_status, current_version from workforce.agent_profiles "
                    "where employee_code = 'HR-04' limit 1"
                )
                row = cur.fetchone()
        finally:
            repo._pool.putconn(conn)

        if row is None:
            print("SKIP - workforce.agent_profiles에 HR-04가 없어 왕복 검증을 건너뛴다")
            raise SystemExit(0)

        agent_id, original_status, original_version = str(row[0]), row[1], row[2]

        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "select coalesce(max(version), 0) from workforce.agent_profile_versions "
                    "where agent_id = %s", (agent_id,),
                )
                existing_max_version = cur.fetchone()[0]
        finally:
            repo._pool.putconn(conn)

        # 1) list_roster/get_agent - 실 DB 조회.
        agent = repo.get_agent(agent_id)
        assert agent is not None and agent.employee_code == "HR-04"
        roster = repo.list_roster()
        assert any(a.agent_id == agent_id for a in roster)
        print("ok - list_roster/get_agent (실 DB) 통과")

        # 2) 존재하지 않는 agent_id.
        assert repo.get_agent("00000000-0000-0000-0000-000000000000") is None
        print("ok - 존재하지 않는 agent_id는 None 통과")

        # 3) Profile Version 제출 - 실 DB.
        conn = repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("select model_id from workforce.models limit 1")
                model_row = cur.fetchone()
        finally:
            repo._pool.putconn(conn)
        if model_row is None:
            print("SKIP - workforce.models에 행이 없어 Profile Version 제출 검증을 건너뛴다")
            raise SystemExit(0)
        model_id = str(model_row[0])

        submission = ProfileVersionSubmission(
            model_id=model_id,
            prompt_artifact_path="departments/07-agent-workforce/hermes/config.yaml#lifecycle-coordinator",
            skill_manifest={"required": ["HR-04"]}, tool_allowlist={"read": ["agent_profiles"]},
            data_scopes={"workforce": "read"}, memory_namespace="workforce/hr-04-selfcheck",
            token_budget={"per_case_tokens": 100000, "daily_tokens": 1000000},
            sla={"decision_latency_hours": 24}, eval_requirements={"status": "PENDING_QA"},
            forbidden_actions=["investment_decision"],
            effective_from=datetime(2026, 8, 3, tzinfo=timezone.utc),
        )
        row_out = None
        try:
            row_out = repo.submit_profile(agent_id, submission)
            assert row_out.version == existing_max_version + 1
            print(f"ok - submit_profile (실 DB) 통과 (new version={row_out.version})")

            updated_agent = repo.get_agent(agent_id)
            assert updated_agent.current_version == row_out.version
            assert updated_agent.current_profile_version.status == ProfileVersionStatus.DRAFT
            print("ok - submit_profile 후 get_agent가 새 Version을 반영함")

            # 4) 존재하지 않는 agent_id로 제출 - 거부.
            try:
                repo.submit_profile("00000000-0000-0000-0000-000000000000", submission)
                raise AssertionError("존재하지 않는 agent_id로 제출이 통과함")
            except AgentNotFoundError:
                pass
            print("ok - 존재하지 않는 agent_id로 제출 거부 통과")

            # 5) change_status - ACTIVE는 증거 없이 애플리케이션 계층에서 막혀야 한다
            # (validate_status_change는 여기서 별도 호출 - Repository 자체는 이 규칙을
            # 모르고 그대로 실행하므로, 실제로는 API 계층이 먼저 검증해야 한다).
            from roster import MissingActivationEvidenceError, StatusChangeRequest

            try:
                validate_status_change(StatusChangeRequest(
                    to_status=EmploymentStatus.ACTIVE, profile_version_id=row_out.profile_version_id,
                    reason="", idempotency_key="selfcheck-1",
                ))
                raise AssertionError("증거 없는 ACTIVE 전이가 통과함")
            except MissingActivationEvidenceError:
                pass
            print("ok - validate_status_change가 API 계층에서 ACTIVE를 막는 것을 재확인")

            # 6) 실제 employment_status 전이 (SUSPENDED - 증거 불필요) 후 원복.
            repo.change_status(agent_id, to_status=EmploymentStatus.SUSPENDED, at=datetime.now(timezone.utc))
            after = repo.get_agent(agent_id)
            assert after.employment_status == EmploymentStatus.SUSPENDED
            assert after.current_profile_version.status == ProfileVersionStatus.SUSPENDED
            print("ok - change_status (실 DB) 통과 - employment_status/profile_version.status 동시 갱신 확인")
        finally:
            # 정리 - 자체 점검으로 만든 Version 행과 상태 변경을 원복한다.
            conn = repo._pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "update workforce.agent_profiles set employment_status = %s, current_version = %s, "
                        "updated_at = now() where agent_id = %s",
                        (original_status, original_version, agent_id),
                    )
                    if row_out is not None:
                        cur.execute(
                            "delete from workforce.agent_profile_versions where profile_version_id = %s",
                            (row_out.profile_version_id,),
                        )
                conn.commit()
            finally:
                repo._pool.putconn(conn)
            print("ok - 자체 점검 흔적 정리 완료 (employment_status/current_version 원복, Version 행 삭제)")
    finally:
        repo.close()
