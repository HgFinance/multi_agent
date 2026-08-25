"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  fetchWorkforceIdleAgents,
  WorkforceIdleAgentsError,
  type IdleStatus,
  type WorkerIdleReport,
  type WorkforceIdleAgents,
  type WorkforceIdleWindow,
} from "../lib/workforceIdleClient";

/**
 * HR이 6개 투자본부 Worker의 유휴 상태를 관측하는 읽기 전용 산출물 카드.
 *
 * Langfuse 조회는 workforce-api(departments/07-agent-workforce)가 하고, 이 화면은
 * 그 판정 결과만 30초마다 다시 받는다 — 유휴 여부는 시간 단위로 바뀌는 값이라
 * LivePortfolioPanel(3초)만큼 자주 부를 이유가 없다.
 */

const POLL_MS = 60_000;

type WindowKey = "daily" | "weekly";

/** 일간은 오늘 하루(4시간 넘게 안 잡히면 IDLE), 주간은 최근 7일(하루 넘게 안
 *  잡히면 IDLE) - 창이 넓어지면 "유휴"의 기준도 같이 넓어져야 한다. 그렇지
 *  않으면 주간 보기에서 정상 근무 패턴(야간·주말 공백)이 전부 IDLE로 뜬다. */
const WINDOW_OPTIONS: Record<WindowKey, WorkforceIdleWindow & { label: string }> = {
  daily: { label: "일간", lookbackHours: 24, idleThresholdHours: 4 },
  weekly: { label: "주간", lookbackHours: 24 * 7, idleThresholdHours: 24 },
};

const STATUS_ORDER: IdleStatus[] = ["ACTIVE", "IDLE", "UNOBSERVED", "UNAVAILABLE"];

const STATUS_VIEW: Record<IdleStatus, { label: string; tone: string; icon: string; hint: string }> = {
  ACTIVE: {
    label: "ACTIVE",
    tone: "border-primary/30 bg-secondary-container text-primary",
    icon: "bolt",
    hint: "임계시간 안에 실행 관측됨",
  },
  IDLE: {
    label: "IDLE",
    tone: "border-outline-variant bg-surface-container-high text-on-surface-variant",
    icon: "schedule",
    hint: "관측은 됐지만 임계시간보다 오래 전 — 조치 검토 대상",
  },
  UNOBSERVED: {
    label: "UNOBSERVED",
    tone: "border-outline-variant bg-surface-container text-on-surface-variant",
    icon: "visibility_off",
    hint: "조건부 trigger가 아직 안 켜졌을 수 있음 — 유휴로 단정 금지",
  },
  UNAVAILABLE: {
    label: "UNAVAILABLE",
    tone: "border-error/40 bg-error-container text-on-error-container",
    icon: "cloud_off",
    hint: "Langfuse 조회 실패/자격증명 없음 — 쉬는 게 아니라 모르는 상태",
  },
};

function statusView(status: string) {
  return (
    STATUS_VIEW[status as IdleStatus] ?? {
      label: status,
      tone: "border-outline-variant bg-surface-container text-on-surface-variant",
      icon: "help",
      hint: "",
    }
  );
}

function formatLastSeen(value: string | null): string {
  if (!value) return "관측 없음";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("ko-KR", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function formatIdleHours(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  if (value < 1) return `${Math.round(value * 60)}분 전`;
  return `${value.toFixed(1)}시간 전`;
}

function WindowToggle({
  value,
  onChange,
}: {
  value: WindowKey;
  onChange: (next: WindowKey) => void;
}) {
  return (
    <div
      className="flex shrink-0 items-stretch overflow-hidden rounded border border-outline-variant bg-surface-container-lowest"
      role="group"
      aria-label="관측 창"
    >
      {(Object.keys(WINDOW_OPTIONS) as WindowKey[]).map((key) => {
        const on = value === key;
        return (
          <button
            key={key}
            type="button"
            aria-pressed={on}
            onClick={() => onChange(key)}
            className={`px-3 py-1.5 text-label-md font-label-md font-semibold transition-colors ${
              key !== "daily" ? "border-l border-outline-variant" : ""
            } ${on ? "bg-secondary-container text-primary" : "text-on-surface-variant hover:bg-surface-container"}`}
          >
            {WINDOW_OPTIONS[key].label}
          </button>
        );
      })}
    </div>
  );
}

function WorkforceIdleArtifactHeader({ samples }: { samples?: number }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-outline-variant bg-surface-container-low px-4 py-2.5">
      <span className="flex min-w-0 items-center gap-2 text-label-md font-label-md text-on-surface-variant">
        <span className="material-symbols-outlined text-[16px]" aria-hidden="true">
          visibility
        </span>
        <span className="truncate">workforce.idle_agents</span>
      </span>
      <div className="flex shrink-0 items-center gap-1.5">
        <span className="inline-flex items-center whitespace-nowrap rounded-full border border-outline-variant bg-surface-container-lowest px-2.5 py-0.5 text-[10px] font-semibold text-on-surface-variant">
          HR 관측
        </span>
        {samples !== undefined ? (
          <span className="inline-flex items-center whitespace-nowrap rounded-full border border-outline-variant bg-surface-container-lowest px-2.5 py-0.5 text-[10px] font-semibold text-on-surface-variant">
            표본 {samples}건
          </span>
        ) : null}
      </div>
    </div>
  );
}

function StatusCountTiles({ reports }: { reports: WorkerIdleReport[] }) {
  const counts = new Map<string, number>();
  for (const report of reports) counts.set(report.status, (counts.get(report.status) ?? 0) + 1);

  return (
    <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
      {STATUS_ORDER.map((status) => {
        const view = statusView(status);
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

function IdleAgentRow({ report }: { report: WorkerIdleReport }) {
  const view = statusView(report.status);
  return (
    <tr className="border-t border-outline-variant/60 text-on-surface">
      <td className="px-2.5 py-1.5">{report.department}</td>
      <td className="px-2.5 py-1.5 font-data-mono">{report.worker_id}</td>
      <td className="px-3 py-2 font-data-mono text-on-surface-variant">{report.trigger}</td>
      <td className="px-2.5 py-1.5">
        <span
          className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2 py-0.5 text-[10px] font-semibold ${view.tone}`}
          title={view.hint}
        >
          <span className="material-symbols-outlined text-[12px]" aria-hidden="true">
            {view.icon}
          </span>
          {view.label}
        </span>
      </td>
      <td className="px-2.5 py-1.5 font-data-mono">{formatLastSeen(report.last_seen_at)}</td>
      <td className="px-3 py-2 font-data-mono text-on-surface-variant">{formatIdleHours(report.idle_hours)}</td>
    </tr>
  );
}

export default function WorkforceIdleAgentsPanel() {
  const [windowKey, setWindowKey] = useState<WindowKey>("daily");
  const activeWindow = WINDOW_OPTIONS[windowKey];
  const query = useQuery<WorkforceIdleAgents, WorkforceIdleAgentsError>({
    queryKey: ["workforce-idle-agents", windowKey],
    queryFn: () => fetchWorkforceIdleAgents(activeWindow),
    refetchInterval: POLL_MS,
    staleTime: 0,
    retry: false,
  });
  const data = query.data ?? null;
  const error = query.error ?? null;
  const loading = query.isPending;
  const reports = data?.idle_agents ?? [];

  return (
    <section
      className="min-w-0 overflow-hidden rounded-lg border border-outline-variant bg-surface-container-lowest shadow-sm"
      aria-labelledby="workforce-idle-title"
    >
      <WorkforceIdleArtifactHeader samples={data ? reports.length : undefined} />
      <div className="space-y-2 px-4 py-3">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <h2 id="workforce-idle-title" className="m-0 text-title-sm font-title-sm font-bold text-primary">
              투자본부 Worker 유휴 상태
            </h2>
            <p className="mt-0.5 max-w-3xl text-[11px] leading-snug text-on-surface-variant">
              6개 투자본부에 등록된 Worker 전원의 최근 실행 관측 시각을 Langfuse에서 읽어 판정합니다. 원문 프롬프트·응답은
              받지 않고 시각만 비교합니다.
            </p>
          </div>
          <WindowToggle value={windowKey} onChange={setWindowKey} />
        </div>

        {error ? (
          <div
            className={`rounded-lg border p-3 text-xs ${
              error.status === 503
                ? "border-outline-variant bg-surface-container-low text-on-surface-variant"
                : "border-error/40 bg-error-container text-on-error-container"
            }`}
            role={error.status === 503 ? "status" : "alert"}
          >
            <p className="m-0 font-semibold">
              {error.status === 503 ? "유휴 관측 연동이 꺼져 있습니다." : "Worker 유휴 상태를 불러오지 못했습니다."}
            </p>
            <p className="m-0 mt-1">{error.message}</p>
          </div>
        ) : null}

        {loading && !data && !error ? (
          <p className="m-0 rounded-lg border border-outline-variant bg-surface-container-low p-3 text-xs text-on-surface-variant">
            Worker 유휴 상태를 확인하는 중입니다…
          </p>
        ) : null}

        {data ? (
          <>
            <StatusCountTiles reports={reports} />

            {data.head_profiles_unavailable ? (
              <p role="status" className="m-0 rounded border border-outline-variant bg-surface-container px-3 py-2 text-xs text-on-surface-variant">
                부서장 신원을 못 읽어 부서장은 이번 판정에서 빠졌습니다: {data.head_profiles_unavailable}
              </p>
            ) : null}

            <div className="overflow-x-auto rounded-lg border border-outline-variant">
              <table className="w-full min-w-[600px] text-left text-xs">
                <thead className="bg-surface-container text-label-md text-on-surface-variant">
                  <tr>
                    <th className="px-2.5 py-1.5 font-semibold">부서</th>
                    <th className="px-2.5 py-1.5 font-semibold">Worker</th>
                    <th className="px-2.5 py-1.5 font-semibold">trigger</th>
                    <th className="px-2.5 py-1.5 font-semibold">상태</th>
                    <th className="px-2.5 py-1.5 font-semibold">마지막 관측</th>
                    <th className="px-2.5 py-1.5 font-semibold">경과</th>
                  </tr>
                </thead>
                <tbody>
                  {reports.length > 0 ? (
                    reports.map((report) => <IdleAgentRow key={`${report.department}-${report.worker_id}`} report={report} />)
                  ) : (
                    <tr>
                      <td colSpan={6} className="px-3 py-7 text-center text-sm text-on-surface-variant">
                        아직 등록된 투자본부 Worker가 없습니다.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </>
        ) : null}

        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 border-t border-outline-variant pt-2 text-[11px] text-on-surface-variant">
          <span>
            {activeWindow.label} 관측 · 최근 {activeWindow.lookbackHours}시간 · 임계 {activeWindow.idleThresholdHours}시간 ·
            Langfuse 타임스탬프 기준(원문 미포함)
          </span>
          <span>{POLL_MS / 1000}초마다 자동 갱신</span>
        </div>
      </div>
    </section>
  );
}
