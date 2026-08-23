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
              HR이 관찰하는 최근 Worker 지연·토큰·실행 상태가 아직 수집되지 않았습니다.
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

  return (
    <section className="min-w-0 overflow-hidden rounded-lg border border-outline-variant bg-surface-container-lowest shadow-sm" aria-labelledby="worker-performance-title">
      <WorkerMetricsArtifactHeader samples={measured.length} />
      <div className="space-y-5 p-4 md:p-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="m-0 text-label-md font-label-md uppercase text-on-surface-variant">Workforce · Worker Observability</p>
            <h2 id="worker-performance-title" className="mt-2 text-headline-md font-headline-md font-bold text-primary">Worker 지연·토큰 현황</h2>
            <p className="mt-2 max-w-3xl text-body-sm font-body-sm text-on-surface-variant">
              HR이 관찰하는 전체 Worker 실행 지표입니다. 모델 입력·출력 원문은 표시하거나 전송하지 않습니다.
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
