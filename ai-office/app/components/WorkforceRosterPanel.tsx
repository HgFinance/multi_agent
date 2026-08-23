"use client";

import { useQuery } from "@tanstack/react-query";
import {
  fetchWorkforceRoster,
  WorkforceRosterError,
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

function WorkforceRosterArtifactHeader({ samples }: { samples?: number }) {
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
        {samples !== undefined ? (
          <span className="inline-flex items-center whitespace-nowrap rounded-full border border-outline-variant bg-surface-container-lowest px-2.5 py-0.5 text-[10px] font-semibold text-on-surface-variant">
            등록 {samples}명
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

function RosterRow({ agent }: { agent: RosterAgent }) {
  const view = employmentView(agent.employment_status);
  const profile = agent.current_profile_version;
  return (
    <tr className="border-t border-outline-variant/60 text-on-surface">
      <td className="px-3 py-2">{agent.department_code}</td>
      <td className="px-3 py-2">
        <div className="min-w-0">
          <span className="block truncate font-data-mono" title={agent.employee_code}>
            {agent.display_name}
          </span>
          <span className="block truncate text-[11px] text-on-surface-variant">{agent.employee_code}</span>
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
  );
}

export default function WorkforceRosterPanel() {
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

  return (
    <section
      className="min-w-0 overflow-hidden rounded-lg border border-outline-variant bg-surface-container-lowest shadow-sm"
      aria-labelledby="workforce-roster-title"
    >
      <WorkforceRosterArtifactHeader samples={data ? agents.length : undefined} />
      <div className="space-y-5 p-4 md:p-6">
        <div className="min-w-0">
          <p className="m-0 text-label-md font-label-md uppercase text-on-surface-variant">Workforce · Roster</p>
          <h2 id="workforce-roster-title" className="mt-2 text-headline-md font-headline-md font-bold text-primary">
            등록 Agent 인력 현황
          </h2>
          <p className="mt-2 max-w-3xl text-body-sm font-body-sm text-on-surface-variant">
            workforce.agent_profiles에 등록된 Agent 전원의 고용 상태와 현재 Profile Version·모델 좌표입니다. 부서장은
            이 목록에 없습니다(직원만).
          </p>
        </div>

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
                  {agents.length > 0 ? (
                    agents.map((agent) => <RosterRow key={agent.agent_id} agent={agent} />)
                  ) : (
                    <tr>
                      <td colSpan={6} className="px-3 py-7 text-center text-sm text-on-surface-variant">
                        아직 등록된 Agent가 없습니다.
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
