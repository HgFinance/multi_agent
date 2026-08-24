"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  fetchAgentAccess,
  fetchWorkforceRoster,
  WorkforceRosterError,
  type AccessAssignment,
  type EmploymentStatus,
  type RosterAgent,
  type WorkforceRoster,
} from "../lib/workforceRosterClient";

/**
 * HR이 등록된 Agent 전원의 고용 상태·프로필 버전·모델 좌표를 관측하는 읽기
 * 전용 산출물 카드. 고용상태는 시간 단위로 자주 바뀌는 값이 아니라
 * WorkforceIdleAgentsPanel(60초)보다 느슨하게 폴링한다.
 */

const POLL_MS = 120_000;

/** "현재 일하는 직원"으로 보는 고용 상태 — CANDIDATE(입사 전)·SUSPENDED(정지)·
 * RETIRED(퇴사)는 기본 목록에서 숨긴다. 필터를 끄면 전체 상태를 보여준다. */
const CURRENTLY_WORKING_STATUSES: ReadonlySet<EmploymentStatus> = new Set(["ACTIVE", "PROBATION"]);

const EMPLOYMENT_VIEW: Record<EmploymentStatus, { label: string; tone: string; icon: string }> = {
  CANDIDATE: {
    label: "CANDIDATE",
    tone: "border-outline-variant bg-surface-container text-on-surface-variant",
    icon: "person_add",
  },
  PROBATION: {
    label: "PROBATION",
    tone: "border-outline-variant bg-surface-container-high text-on-surface-variant",
    icon: "hourglass_top",
  },
  ACTIVE: {
    label: "ACTIVE",
    tone: "border-primary/30 bg-secondary-container text-primary",
    icon: "bolt",
  },
  SUSPENDED: {
    label: "SUSPENDED",
    tone: "border-error/40 bg-error-container text-on-error-container",
    icon: "block",
  },
  RETIRED: {
    label: "RETIRED",
    tone: "border-outline-variant bg-surface-container text-on-surface-variant",
    icon: "history",
  },
};

function employmentView(status: string) {
  return (
    EMPLOYMENT_VIEW[status as EmploymentStatus] ?? {
      label: status,
      tone: "border-outline-variant bg-surface-container text-on-surface-variant",
      icon: "help",
    }
  );
}

function WorkforceRosterArtifactHeader({ visible, total }: { visible?: number; total?: number }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-outline-variant bg-surface-container-low px-4 py-2.5">
      <span className="flex min-w-0 items-center gap-2 text-label-md font-label-md text-on-surface-variant">
        <span className="material-symbols-outlined text-[16px]" aria-hidden="true">
          badge
        </span>
        <span className="truncate">workforce.roster</span>
      </span>
      <div className="flex shrink-0 items-center gap-1.5">
        <span className="inline-flex items-center whitespace-nowrap rounded-full border border-outline-variant bg-surface-container-lowest px-2.5 py-0.5 text-[10px] font-semibold text-on-surface-variant">
          HR 관측
        </span>
        {total !== undefined ? (
          <span className="inline-flex items-center whitespace-nowrap rounded-full border border-outline-variant bg-surface-container-lowest px-2.5 py-0.5 text-[10px] font-semibold text-on-surface-variant">
            {visible !== undefined && visible !== total ? `근무 중 ${visible} / 등록 ${total}명` : `등록 ${total}명`}
          </span>
        ) : null}
      </div>
    </div>
  );
}

function EmploymentCountTiles({ agents }: { agents: RosterAgent[] }) {
  const counts = new Map<string, number>();
  for (const agent of agents) counts.set(agent.employment_status, (counts.get(agent.employment_status) ?? 0) + 1);
  const statuses: EmploymentStatus[] = ["ACTIVE", "PROBATION", "CANDIDATE", "SUSPENDED", "RETIRED"];
  const present = statuses.filter((status) => (counts.get(status) ?? 0) > 0);

  return (
    <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
      {(present.length > 0 ? present : statuses.slice(0, 4)).map((status) => {
        const view = employmentView(status);
        return (
          <div key={status} className="rounded-md border border-outline-variant bg-surface-container-low px-3 py-2.5">
            <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-semibold ${view.tone}`}>
              <span className="material-symbols-outlined text-[12px]" aria-hidden="true">
                {view.icon}
              </span>
              {view.label}
            </span>
            <strong className="mt-1.5 block font-data-mono text-body-md text-on-surface">{counts.get(status) ?? 0}</strong>
          </div>
        );
      })}
    </div>
  );
}

/** Roster 행을 펼쳤을 때만 마운트된다 - 그때 처음 Access를 불러온다(N+1 방지). */
function AccessAssignmentsDetail({ agentId }: { agentId: string }) {
  const query = useQuery<{ assignments: AccessAssignment[] }, WorkforceRosterError>({
    queryKey: ["workforce-agent-access", agentId],
    queryFn: () => fetchAgentAccess(agentId),
    staleTime: 60_000,
    retry: false,
  });

  if (query.isPending) {
    return <p className="m-0 px-3 py-2 text-xs text-on-surface-variant">Access를 불러오는 중입니다…</p>;
  }
  if (query.error) {
    return (
      <p
        className={`m-0 px-3 py-2 text-xs ${
          query.error.status === 501 ? "text-on-surface-variant" : "text-error"
        }`}
      >
        {query.error.status === 501 ? "Access 저장소가 연결돼 있지 않습니다." : query.error.message}
      </p>
    );
  }
  const assignments = query.data?.assignments ?? [];
  if (assignments.length === 0) {
    return <p className="m-0 px-3 py-2 text-xs text-on-surface-variant">부여된 Access가 없습니다.</p>;
  }
  return (
    <div className="overflow-x-auto px-3 py-2">
      <table className="w-full min-w-[560px] text-left text-[11px]">
        <thead className="text-on-surface-variant">
          <tr>
            <th className="px-2 py-1 font-semibold">종류</th>
            <th className="px-2 py-1 font-semibold">대상</th>
            <th className="px-2 py-1 font-semibold">상태</th>
            <th className="px-2 py-1 font-semibold">유효기간</th>
          </tr>
        </thead>
        <tbody>
          {assignments.map((assignment) => (
            <tr key={assignment.assignment_id} className="border-t border-outline-variant/40">
              <td className="px-2 py-1 font-data-mono">{assignment.resource_kind}</td>
              <td className="px-2 py-1 font-data-mono">{assignment.resource_ref}</td>
              <td className="px-2 py-1">{assignment.status}</td>
              <td className="px-2 py-1 font-data-mono text-on-surface-variant">
                {new Date(assignment.effective_from).toLocaleDateString("ko-KR")} ~{" "}
                {new Date(assignment.effective_to).toLocaleDateString("ko-KR")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RosterRow({
  agent,
  expanded,
  onToggle,
}: {
  agent: RosterAgent;
  expanded: boolean;
  onToggle: () => void;
}) {
  const view = employmentView(agent.employment_status);
  const profile = agent.current_profile_version;
  return (
    <>
      <tr
        className="cursor-pointer border-t border-outline-variant/60 text-on-surface hover:bg-surface-container-low"
        onClick={onToggle}
        aria-expanded={expanded}
      >
        <td className="px-3 py-2">{agent.department_code}</td>
        <td className="px-3 py-2">
          <div className="flex min-w-0 items-center gap-1.5">
            <span className="material-symbols-outlined shrink-0 text-[14px] text-on-surface-variant" aria-hidden="true">
              {expanded ? "expand_more" : "chevron_right"}
            </span>
            <div className="min-w-0">
              <span className="block truncate font-data-mono" title={agent.employee_code}>
                {agent.display_name}
              </span>
              <span className="block truncate text-[11px] text-on-surface-variant">{agent.employee_code}</span>
            </div>
          </div>
        </td>
        <td className="px-3 py-2 font-data-mono text-on-surface-variant">{agent.role_code}</td>
        <td className="px-3 py-2">
          <span className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2 py-0.5 text-[10px] font-semibold ${view.tone}`}>
            <span className="material-symbols-outlined text-[12px]" aria-hidden="true">
              {view.icon}
            </span>
            {view.label}
          </span>
        </td>
        <td className="px-3 py-2 font-data-mono">
          {profile ? `${profile.model.provider} / ${profile.model.model_name}` : "미배정"}
        </td>
        <td className="px-3 py-2 font-data-mono text-on-surface-variant">
          {profile ? `v${profile.version} · ${profile.status}` : "—"}
        </td>
      </tr>
      {expanded ? (
        <tr className="bg-surface-container-lowest">
          <td colSpan={6} className="p-0">
            <div className="border-b border-outline-variant/60">
              <p className="m-0 px-3 pt-2 text-[10px] font-semibold uppercase text-on-surface-variant">
                Access (더보기)
              </p>
              <AccessAssignmentsDetail agentId={agent.agent_id} />
            </div>
          </td>
        </tr>
      ) : null}
    </>
  );
}

export default function WorkforceRosterPanel() {
  const [expandedAgentId, setExpandedAgentId] = useState<string | null>(null);
  const [showAllStatuses, setShowAllStatuses] = useState(false);
  const query = useQuery<WorkforceRoster, WorkforceRosterError>({
    queryKey: ["workforce-roster"],
    queryFn: () => fetchWorkforceRoster(),
    refetchInterval: POLL_MS,
    staleTime: 0,
    retry: false,
  });
  const data = query.data ?? null;
  const error = query.error ?? null;
  const loading = query.isPending;
  const agents = data?.agents ?? [];
  const visibleAgents = showAllStatuses
    ? agents
    : agents.filter((agent) => CURRENTLY_WORKING_STATUSES.has(agent.employment_status as EmploymentStatus));
  const hiddenCount = agents.length - visibleAgents.length;

  return (
    <section
      className="min-w-0 overflow-hidden rounded-lg border border-outline-variant bg-surface-container-lowest shadow-sm"
      aria-labelledby="workforce-roster-title"
    >
      <WorkforceRosterArtifactHeader
        total={data ? agents.length : undefined}
        visible={data ? visibleAgents.length : undefined}
      />
      <div className="space-y-5 p-4 md:p-6">
        <div className="min-w-0">
          <p className="m-0 text-label-md font-label-md uppercase text-on-surface-variant">Workforce · Roster</p>
          <h2 id="workforce-roster-title" className="mt-2 text-headline-md font-headline-md font-bold text-primary">
            등록 Agent 인력 현황
          </h2>
          <p className="mt-2 max-w-3xl text-body-sm font-body-sm text-on-surface-variant">
            workforce.agent_profiles에 등록된 Agent 전원의 고용 상태와 현재 Profile Version·모델 좌표입니다. 부서장은 이 목록에 없습니다(직원만).
          </p>
        </div>

        {data ? (
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => setShowAllStatuses((current) => !current)}
              aria-pressed={!showAllStatuses}
              className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-[11px] font-semibold transition-colors ${
                showAllStatuses
                  ? "border-outline-variant bg-surface-container-low text-on-surface-variant"
                  : "border-primary/30 bg-secondary-container text-primary"
              }`}
            >
              <span className="material-symbols-outlined text-[14px]" aria-hidden="true">
                {showAllStatuses ? "visibility" : "filter_alt"}
              </span>
              {showAllStatuses ? "전체 보기 (CANDIDATE·SUSPENDED·RETIRED 포함)" : "현재 근무 중인 Agent만 보기"}
            </button>
            {!showAllStatuses && hiddenCount > 0 ? (
              <span className="text-[11px] text-on-surface-variant">퇴사·후보·정지 {hiddenCount}명 숨김</span>
            ) : null}
          </div>
        ) : null}

        {error ? (
          <div
            className={`rounded-lg border p-4 text-sm ${
              error.status === 501
                ? "border-outline-variant bg-surface-container-low text-on-surface-variant"
                : "border-error/40 bg-error-container text-on-error-container"
            }`}
            role={error.status === 501 ? "status" : "alert"}
          >
            <p className="m-0 font-semibold">
              {error.status === 501 ? "Roster 저장소가 연결돼 있지 않습니다." : "Roster를 불러오지 못했습니다."}
            </p>
            <p className="m-0 mt-1">{error.message}</p>
          </div>
        ) : null}

        {loading && !data && !error ? (
          <p className="m-0 rounded-lg border border-outline-variant bg-surface-container-low p-5 text-sm text-on-surface-variant">
            Roster를 확인하는 중입니다…
          </p>
        ) : null}

        {data ? (
          <>
            <EmploymentCountTiles agents={agents} />

            <div className="overflow-x-auto rounded-lg border border-outline-variant">
              <table className="w-full min-w-[720px] text-left text-xs">
                <thead className="bg-surface-container text-label-md text-on-surface-variant">
                  <tr>
                    <th className="px-3 py-2 font-semibold">부서</th>
                    <th className="px-3 py-2 font-semibold">Agent</th>
                    <th className="px-3 py-2 font-semibold">역할</th>
                    <th className="px-3 py-2 font-semibold">고용 상태</th>
                    <th className="px-3 py-2 font-semibold">모델</th>
                    <th className="px-3 py-2 font-semibold">프로필 버전</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleAgents.length > 0 ? (
                    visibleAgents.map((agent) => (
                      <RosterRow
                        key={agent.agent_id}
                        agent={agent}
                        expanded={expandedAgentId === agent.agent_id}
                        onToggle={() =>
                          setExpandedAgentId((current) => (current === agent.agent_id ? null : agent.agent_id))
                        }
                      />
                    ))
                  ) : (
                    <tr>
                      <td colSpan={6} className="px-3 py-7 text-center text-sm text-on-surface-variant">
                        {agents.length > 0
                          ? "현재 근무 중인 Agent가 없습니다 (필터를 해제하면 전체를 볼 수 있습니다)."
                          : "아직 등록된 Agent가 없습니다."}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </>
        ) : null}

        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-t border-outline-variant pt-3 text-xs text-on-surface-variant">
          <span>workforce.agent_profiles 기준</span>
          <span>{POLL_MS / 1000}초마다 자동 갱신</span>
        </div>
      </div>
    </section>
  );
}
