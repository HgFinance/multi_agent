"use client";

import { useQuery } from "@tanstack/react-query";
import {
  fetchWorkforceCapacity,
  WorkforceCapacityError,
  type CapacityObservationStatus,
  type DepartmentCapacityReport,
  type WorkforceCapacity,
} from "../lib/workforceCapacityClient";

/**
 * HR이 6개 투자본부의 Capacity(용량)를 관측하는 읽기 전용 산출물 카드.
 *
 * `workforce.capacity_snapshots` writer가 없어(P1-2 미착수) DB 기반 Scorecard의
 * capacity는 항상 null이다 - 이 패널은 그 대신 Langfuse 실행 이벤트를 직접
 * 집계한 값을 보여준다(WorkforceIdleAgentsPanel과 같은 원리). 판정/집계는
 * workforce-api가 하고 이 화면은 결과만 표시한다.
 */

const POLL_MS = 60_000;

const STATUS_VIEW: Record<CapacityObservationStatus, { label: string; tone: string; icon: string }> = {
  MEASURED: {
    label: "MEASURED",
    tone: "border-primary/30 bg-secondary-container text-primary",
    icon: "monitoring",
  },
  UNAVAILABLE: {
    label: "UNAVAILABLE",
    tone: "border-error/40 bg-error-container text-on-error-container",
    icon: "cloud_off",
  },
};

function statusView(status: string) {
  return (
    STATUS_VIEW[status as CapacityObservationStatus] ?? {
      label: status,
      tone: "border-outline-variant bg-surface-container text-on-surface-variant",
      icon: "help",
    }
  );
}

function formatMs(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  return value >= 1_000 ? `${(value / 1_000).toFixed(1)}초` : `${Math.round(value)}ms`;
}

function formatRate(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

function formatUtilization(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  // 부서 등록 Worker 전원을 합산한 값이라 여러 Worker가 겹쳐 돌면 100%를
  // 넘을 수 있다(단일 서버 가동률이 아니라 "부서 총 작업시간/관측 시간").
  return `${(value * 100).toFixed(0)}%`;
}

function CapacityArtifactHeader({ samples }: { samples?: number }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-outline-variant bg-surface-container-low px-4 py-2.5">
      <span className="flex min-w-0 items-center gap-2 text-label-md font-label-md text-on-surface-variant">
        <span className="material-symbols-outlined text-[16px]" aria-hidden="true">
          speed
        </span>
        <span className="truncate">workforce.capacity</span>
      </span>
      <div className="flex shrink-0 items-center gap-1.5">
        <span className="inline-flex items-center whitespace-nowrap rounded-full border border-outline-variant bg-surface-container-lowest px-2.5 py-0.5 text-[10px] font-semibold text-on-surface-variant">
          HR 관측 · Langfuse
        </span>
        {samples !== undefined ? (
          <span className="inline-flex items-center whitespace-nowrap rounded-full border border-outline-variant bg-surface-container-lowest px-2.5 py-0.5 text-[10px] font-semibold text-on-surface-variant">
            {samples}개 본부
          </span>
        ) : null}
      </div>
    </div>
  );
}

function CapacityRow({ report }: { report: DepartmentCapacityReport }) {
  const view = statusView(report.status);
  return (
    <tr className="border-t border-outline-variant/60 text-on-surface">
      <td className="px-2.5 py-1.5 font-data-mono">{report.department}</td>
      <td className="px-2.5 py-1.5">
        <span className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2 py-0.5 text-[10px] font-semibold ${view.tone}`}>
          <span className="material-symbols-outlined text-[12px]" aria-hidden="true">
            {view.icon}
          </span>
          {view.label}
        </span>
      </td>
      <td className="px-2.5 py-1.5 font-data-mono">{report.arrivals ?? "—"}</td>
      <td className="px-2.5 py-1.5 font-data-mono">{formatMs(report.duration_p95_ms)}</td>
      <td className="px-2.5 py-1.5 font-data-mono">{formatRate(report.error_rate)}</td>
      <td className="px-2.5 py-1.5 font-data-mono">{formatRate(report.retry_rate)}</td>
      <td className="px-2.5 py-1.5 font-data-mono">{formatUtilization(report.utilization)}</td>
    </tr>
  );
}

export default function WorkforceCapacityPanel() {
  const query = useQuery<WorkforceCapacity, WorkforceCapacityError>({
    queryKey: ["workforce-capacity"],
    queryFn: () => fetchWorkforceCapacity(24),
    refetchInterval: POLL_MS,
    staleTime: 0,
    retry: false,
  });
  const data = query.data ?? null;
  const error = query.error ?? null;
  const loading = query.isPending;
  const reports = data?.capacity ?? [];

  return (
    <section
      className="min-w-0 overflow-hidden rounded-lg border border-outline-variant bg-surface-container-lowest shadow-sm"
      aria-labelledby="workforce-capacity-title"
    >
      <CapacityArtifactHeader samples={data ? reports.length : undefined} />
      <div className="space-y-2 px-4 py-3">
        <div className="min-w-0">
          <h2 id="workforce-capacity-title" className="m-0 text-title-sm font-title-sm font-bold text-primary">
            투자본부 용량(Capacity) 관측
          </h2>
          <p className="mt-0.5 max-w-3xl text-[11px] leading-snug text-on-surface-variant">
            최근 24시간 Langfuse 실행 이벤트를 부서별로 집계한 값입니다(도착 건수·처리시간
            p95·오류율·재시도율·가동률). 정식 Quality+Cost 통합 Scorecard 연동 전까지는
            이 값이 Capacity의 유일한 실측 출처입니다.
          </p>
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
              {error.status === 503 ? "Capacity 관측 연동이 꺼져 있습니다." : "Capacity를 불러오지 못했습니다."}
            </p>
            <p className="m-0 mt-1">{error.message}</p>
          </div>
        ) : null}

        {loading && !data && !error ? (
          <p className="m-0 rounded-lg border border-outline-variant bg-surface-container-low p-3 text-xs text-on-surface-variant">
            Capacity를 확인하는 중입니다…
          </p>
        ) : null}

        {data ? (
          <div className="overflow-x-auto rounded-lg border border-outline-variant">
            <table className="w-full min-w-[640px] text-left text-xs">
              <thead className="bg-surface-container text-label-md text-on-surface-variant">
                <tr>
                  <th className="px-2.5 py-1.5 font-semibold">부서</th>
                  <th className="px-2.5 py-1.5 font-semibold">상태</th>
                  <th className="px-2.5 py-1.5 font-semibold">도착 건수</th>
                  <th className="px-2.5 py-1.5 font-semibold">처리시간 p95</th>
                  <th className="px-2.5 py-1.5 font-semibold">오류율</th>
                  <th className="px-2.5 py-1.5 font-semibold">재시도율</th>
                  <th className="px-2.5 py-1.5 font-semibold">가동률</th>
                </tr>
              </thead>
              <tbody>
                {reports.length > 0 ? (
                  reports.map((report) => <CapacityRow key={report.department} report={report} />)
                ) : (
                  <tr>
                    <td colSpan={7} className="px-3 py-7 text-center text-sm text-on-surface-variant">
                      아직 등록된 투자본부가 없습니다.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        ) : null}

        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 border-t border-outline-variant pt-2 text-[11px] text-on-surface-variant">
          <span>Langfuse 실행 이벤트 집계 기준 · 대기시간(queue)은 계측 대상 아님</span>
          <span>{POLL_MS / 1000}초마다 자동 갱신</span>
        </div>
      </div>
    </section>
  );
}
