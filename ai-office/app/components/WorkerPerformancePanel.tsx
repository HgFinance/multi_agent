import type { LlmPerformanceMetric } from "../lib/operationsClient";

function formatLatency(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "측정값 없음";
  return value >= 1_000 ? `${(value / 1_000).toFixed(value >= 10_000 ? 0 : 1)}초` : `${Math.round(value)}ms`;
}

function percentile(values: number[], percentileValue: number): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((left, right) => left - right);
  return sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * percentileValue) - 1)];
}

type StatusCategory = "success" | "error" | "active" | "neutral";

/** 실행 상태 문자열은 부서마다(REJECT/DEGRADED/HOLD/...) 제각각이라 의미를 다 알 수
 *  없다 - 그래서 "정확히 어떤 상태였나"는 라벨 그대로 보여주고, 색은 네 카테고리
 *  키워드 기준으로만 나눈다(과잉 해석하지 않는다). */
function categorizeStatus(status: string): StatusCategory {
  const upper = status.toUpperCase();
  if (/(ERROR|FAIL|REJECT|DENY|DEGRADED|BLOCKED)/.test(upper)) return "error";
  if (/(COMPLETED|APPROVE|ACCEPT|^OK$)/.test(upper)) return "success";
  if (/(RUNNING|QUEUED|WAITING|PENDING)/.test(upper)) return "active";
  return "neutral";
}

const CATEGORY_COLOR: Record<StatusCategory, string> = {
  success: "var(--color-tertiary-fixed-dim)",
  error: "var(--color-error)",
  active: "var(--color-primary)",
  neutral: "var(--color-outline)",
};

const CATEGORY_SWATCH_CLASS: Record<StatusCategory, string> = {
  success: "bg-tertiary-fixed-dim",
  error: "bg-error",
  active: "bg-primary",
  neutral: "bg-outline",
};

type StatusSlice = { label: string; count: number; percent: number; category: StatusCategory };

function statusBreakdown(measured: LlmPerformanceMetric[]): StatusSlice[] {
  const counts = new Map<string, number>();
  for (const item of measured) {
    const label = item.status || "미확인";
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  const total = measured.length;
  return [...counts.entries()]
    .map(([label, count]) => ({ label, count, percent: (count / total) * 100, category: categorizeStatus(label) }))
    .sort((left, right) => right.count - left.count);
}

/** 실행 상태 도넛. completed류는 초록, 오류성 상태는 빨강, 진행 중은 파랑, 그
 *  외는 회색 - 색만으로 구분하지 않도록 범례에 라벨을 항상 같이 둔다. */
function StatusDonutChart({ slices }: { slices: StatusSlice[] }) {
  const total = slices.reduce((sum, slice) => sum + slice.count, 0);

  return (
    <div className="w-full min-w-0 rounded-md border border-outline-variant bg-surface-container-low px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <span className="flex items-center gap-2 text-label-md font-label-md uppercase text-on-surface-variant">
          <span className="material-symbols-outlined text-[16px]" aria-hidden="true">
            donut_small
          </span>
          실행 상태 분포
        </span>
        <span className="text-[11px] text-on-surface-variant">{total}건</span>
      </div>

      {slices.length > 0 ? (
        <div className="mt-3 grid grid-cols-[7rem_minmax(0,1fr)] items-center gap-4">
          <div className="relative h-28 w-28 shrink-0" role="img" aria-label="실행 상태별 비율">
            <svg viewBox="0 0 120 120" className="h-full w-full -rotate-90" aria-hidden="true">
              <circle cx="60" cy="60" r="42" fill="none" stroke="#e0e3e5" strokeWidth="16" />
              {slices.reduce<{ offset: number; elements: React.ReactNode[] }>(
                (result, slice) => {
                  const visiblePercent = Math.max(slice.percent, 0.5);
                  result.elements.push(
                    <circle
                      key={slice.label}
                      cx="60"
                      cy="60"
                      r="42"
                      fill="none"
                      pathLength="100"
                      stroke={CATEGORY_COLOR[slice.category]}
                      strokeDasharray={`${visiblePercent} ${100 - visiblePercent}`}
                      strokeDashoffset={-result.offset}
                      strokeWidth="16"
                    />,
                  );
                  result.offset += visiblePercent;
                  return result;
                },
                { offset: 0, elements: [] },
              ).elements}
            </svg>
            <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center text-center">
              <strong className="font-data-mono text-sm text-primary">{total}건</strong>
            </div>
          </div>

          <ul className="m-0 min-w-0 space-y-2 p-0">
            {slices.map((slice) => (
              <li key={slice.label} className="flex min-w-0 items-center justify-between gap-2 text-xs">
                <span className="flex min-w-0 items-center gap-2">
                  <span
                    className={`h-2.5 w-2.5 shrink-0 rounded-full ${CATEGORY_SWATCH_CLASS[slice.category]}`}
                    aria-hidden="true"
                  />
                  <span className="truncate text-on-surface" title={slice.label}>
                    {slice.label}
                  </span>
                </span>
                <span className="shrink-0 font-data-mono text-on-surface-variant">
                  {slice.count}건 · {slice.percent.toFixed(0)}%
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="m-0 mt-3 flex min-h-[7rem] items-center justify-center rounded border border-outline-variant bg-surface-container-lowest px-4 text-center text-xs text-on-surface-variant">
          실행 상태 데이터를 확인하는 중입니다.
        </p>
      )}
    </div>
  );
}

function WorkerMetricsArtifactHeader({ samples }: { samples?: number }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-outline-variant bg-surface-container-low px-4 py-2.5">
      <span className="flex min-w-0 items-center gap-2 text-label-md font-label-md text-on-surface-variant">
        <span className="material-symbols-outlined text-[16px]" aria-hidden="true">monitoring</span>
        <span className="truncate">workforce.worker_performance</span>
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

/**
 * HR이 전체 Worker의 성능을 관찰하는 읽기 전용 산출물 카드다.
 * 원문 프롬프트·응답은 받지 않고 BFF가 제공한 집계 지표만 표시한다.
 */
export default function WorkerPerformancePanel({ metrics }: { metrics: LlmPerformanceMetric[] }) {
  const measured = metrics.filter((item) => Number.isFinite(item.latency_ms) && item.latency_ms >= 0);
  if (measured.length === 0) {
    return (
      <section className="min-w-0 overflow-hidden rounded-lg border border-outline-variant bg-surface-container-lowest shadow-sm" aria-labelledby="worker-performance-title">
        <WorkerMetricsArtifactHeader />
        <div className="space-y-5 p-4 md:p-6">
          <div className="min-w-0">
            <p className="m-0 text-label-md font-label-md uppercase text-on-surface-variant">Workforce · Worker Observability</p>
            <h2 id="worker-performance-title" className="mt-2 text-headline-md font-headline-md font-bold text-primary">Worker 지연·토큰 현황</h2>
            <p className="mt-2 max-w-3xl text-body-sm font-body-sm text-on-surface-variant">
              포트폴리오 추천 파이프라인이 이 BFF에서 실행되며 남긴 Worker 지연·토큰·실행 상태가 아직 없습니다. 다른
              경로(Hermes/Kanban 배차)로 실행된 Worker는 이 카드에 잡히지 않습니다 - 전체 Worker는{" "}
              <span className="font-semibold text-on-surface">투자본부 Worker 유휴 상태</span> 카드를 보세요.
            </p>
          </div>
        </div>
      </section>
    );
  }

  const latencies = measured.map((item) => item.latency_ms);
  const average = Math.round(latencies.reduce((total, value) => total + value, 0) / latencies.length);
  const p95 = percentile(latencies, 0.95);
  const tokenTotal = measured.reduce(
    (total, item) => total + (item.prompt_tokens ?? 0) + (item.completion_tokens ?? 0),
    0,
  );
  const hasTokenMeasurement = measured.some(
    (item) => item.prompt_tokens != null || item.completion_tokens != null,
  );
  const recent = [...measured].slice(-10).reverse();
  const statusSlices = statusBreakdown(measured);

  return (
    <section className="min-w-0 overflow-hidden rounded-lg border border-outline-variant bg-surface-container-lowest shadow-sm" aria-labelledby="worker-performance-title">
      <WorkerMetricsArtifactHeader samples={measured.length} />
      <div className="space-y-5 p-4 md:p-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="m-0 text-label-md font-label-md uppercase text-on-surface-variant">Workforce · Worker Observability</p>
            <h2 id="worker-performance-title" className="mt-2 text-headline-md font-headline-md font-bold text-primary">Worker 지연·토큰 현황</h2>
            <p className="mt-2 max-w-3xl text-body-sm font-body-sm text-on-surface-variant">
              포트폴리오 추천 파이프라인이 이 BFF에서 실행되며 호출한 Worker만의 지표입니다(재시작 시 초기화, 최근
              100건). 다른 경로(Hermes/Kanban 배차)로 실행된 Worker나 회사 전체 현황은 이 카드에 없습니다. 모델
              입력·출력 원문은 표시하거나 전송하지 않습니다.
            </p>
          </div>
          <div className="shrink-0 rounded-md border border-outline-variant bg-surface-container-low px-4 py-3 text-right">
            <span className="block text-label-md font-label-md uppercase text-on-surface-variant">관측 표본</span>
            <p className="m-0 mt-1 font-data-mono text-body-sm font-semibold text-primary">{measured.length}건</p>
            <span className="text-[11px] text-outline">최근 실행 기준</span>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
          {[
            ["평균 지연", formatLatency(average)],
            ["P95 지연", p95 === null ? "측정값 없음" : formatLatency(p95)],
            ["최대 지연", formatLatency(Math.max(...latencies))],
            ["입·출력 토큰", hasTokenMeasurement ? tokenTotal.toLocaleString("ko-KR") : "미측정"],
          ].map(([label, value]) => (
            <div key={label} className="rounded-md border border-outline-variant bg-surface-container-low px-3 py-2.5">
              <p className="m-0 text-label-md font-label-md text-on-surface-variant">{label}</p>
              <strong className="mt-1 block font-data-mono text-body-md text-on-surface">{value}</strong>
            </div>
          ))}
        </div>

        <div className="min-w-0 max-w-xl">
          <StatusDonutChart slices={statusSlices} />
        </div>

        <div className="overflow-x-auto rounded-lg border border-outline-variant">
          <table className="w-full min-w-[680px] text-left text-xs">
            <thead className="bg-surface-container text-label-md text-on-surface-variant">
              <tr>
                <th className="px-3 py-2 font-semibold">부서</th>
                <th className="px-3 py-2 font-semibold">Worker</th>
                <th className="px-3 py-2 font-semibold">모델</th>
                <th className="px-3 py-2 font-semibold">지연</th>
                <th className="px-3 py-2 font-semibold">입력/출력 토큰</th>
                <th className="px-3 py-2 font-semibold">상태</th>
              </tr>
            </thead>
            <tbody>
              {recent.map((metric, index) => (
                <tr key={`${metric.stage}-${metric.worker_id}-${index}`} className="border-t border-outline-variant/60 text-on-surface">
                  <td className="px-3 py-2">{metric.stage}</td>
                  <td className="px-3 py-2 font-data-mono">{metric.worker_id}</td>
                  <td className="px-3 py-2 font-data-mono">{metric.model_name || "미측정"}</td>
                  <td className="px-3 py-2 font-data-mono">{formatLatency(metric.latency_ms)}</td>
                  <td className="px-3 py-2 font-data-mono">
                    {metric.prompt_tokens == null && metric.completion_tokens == null
                      ? "미측정"
                      : `${metric.prompt_tokens ?? 0} / ${metric.completion_tokens ?? 0}`}
                  </td>
                  <td className="px-3 py-2">{metric.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
