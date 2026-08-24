"use client";

import { useQuery } from "@tanstack/react-query";
import { fetchQaLangsmithTraces, type LangsmithQaTraces } from "../lib/langsmithClient";

/**
 * QA 부서 카드의 LangSmith 관측 패널.
 *
 * `stage:qa` 태그가 붙은 redacted trace만 집계한 값이다(BFF `langsmith_traces.py`
 * 머리말) - prompt/output은 이 화면에 절대 나타나지 않는다. LangSmith는 선택적
 * 추적 어댑터라(TECH_STACK_DECISIONS.md) 자격증명이 없거나 호출이 실패해도
 * "연결됨"으로 보이면 안 된다 - 상태 세 가지(READY/NOT_CONFIGURED/ERROR)를 그대로
 * 보여준다(AI Office CLAUDE.md: 실패를 성공으로 표시하지 않는다).
 */

const POLL_MS = 60_000;
const DAYS = 7;

const SUCCESS_COLOR = "var(--color-on-tertiary-container)";
const ERROR_COLOR = "var(--color-error)";
const P50_COLOR = "var(--color-primary)";
const P99_COLOR = "var(--color-outline)";

type SeriesPoint = number | null;
type Series = { id: string; label: string; color: string; dashed?: boolean; values: SeriesPoint[]; unit?: string };

const CHART_WIDTH = 640;
const CHART_HEIGHT = 200;
const PAD_LEFT = 34;
const PAD_RIGHT = 46;
const PAD_TOP = 12;
const PAD_BOTTOM = 24;

function niceMax(value: number): number {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const normalized = value / magnitude;
  const step = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return step * magnitude;
}

function formatDayLabel(iso: string): string {
  const [, month, day] = iso.split("-");
  return `${Number(month)}/${Number(day)}`;
}

/** 의존성 추가 없이 순수 SVG로 그리는 시계열 라인 차트. */
function TimeSeriesChart({
  dates,
  series,
  valueFormatter,
}: {
  dates: string[];
  series: Series[];
  valueFormatter: (value: number) => string;
}) {
  const allValues = series.flatMap((s) => s.values.filter((v): v is number => v !== null));
  const maxValue = niceMax(Math.max(0, ...allValues));
  const plotWidth = CHART_WIDTH - PAD_LEFT - PAD_RIGHT;
  const plotHeight = CHART_HEIGHT - PAD_TOP - PAD_BOTTOM;
  const stepX = dates.length > 1 ? plotWidth / (dates.length - 1) : 0;

  const xAt = (index: number) => PAD_LEFT + stepX * index;
  const yAt = (value: number) => PAD_TOP + plotHeight - (value / maxValue) * plotHeight;

  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((fraction) => Math.round(maxValue * fraction * 100) / 100);
  const hasAnyValue = allValues.length > 0;

  return (
    <div className="overflow-x-auto">
      <svg
        viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
        role="img"
        aria-label={`${series.map((s) => s.label).join(", ")} 시계열 차트`}
        className="w-full min-w-[420px]"
      >
        {yTicks.map((tick) => (
          <g key={tick}>
            <line
              x1={PAD_LEFT}
              x2={CHART_WIDTH - PAD_RIGHT}
              y1={yAt(tick)}
              y2={yAt(tick)}
              stroke="var(--color-outline-variant)"
              strokeWidth={1}
            />
            <text
              x={PAD_LEFT - 6}
              y={yAt(tick)}
              textAnchor="end"
              dominantBaseline="middle"
              className="fill-on-surface-variant"
              fontSize={9}
            >
              {tick}
            </text>
          </g>
        ))}

        {dates.map((date, index) =>
          index % Math.ceil(dates.length / 7) === 0 ? (
            <text
              key={date}
              x={xAt(index)}
              y={CHART_HEIGHT - 6}
              textAnchor="middle"
              className="fill-on-surface-variant"
              fontSize={9}
            >
              {formatDayLabel(date)}
            </text>
          ) : null,
        )}

        {!hasAnyValue ? (
          <text
            x={CHART_WIDTH / 2}
            y={CHART_HEIGHT / 2}
            textAnchor="middle"
            className="fill-on-surface-variant"
            fontSize={11}
          >
            표시할 값이 없습니다
          </text>
        ) : null}

        {series.map((line) => {
          let path = "";
          let drawing = false;
          line.values.forEach((value, index) => {
            if (value === null) {
              drawing = false;
              return;
            }
            path += `${drawing ? "L" : "M"}${xAt(index).toFixed(1)},${yAt(value).toFixed(1)} `;
            drawing = true;
          });
          let lastIndex = -1;
          let lastValue: number | null = null;
          line.values.forEach((value, index) => {
            if (value === null) return;
            lastIndex = index;
            lastValue = value;
          });

          return (
            <g key={line.id}>
              <path
                d={path}
                fill="none"
                stroke={line.color}
                strokeWidth={2}
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeDasharray={line.dashed ? "5 3" : undefined}
              />
              {line.values.map((value, index) =>
                value === null ? null : (
                  <circle key={index} cx={xAt(index)} cy={yAt(value)} r={2.5} fill={line.color}>
                    <title>
                      {formatDayLabel(dates[index])} · {line.label} {valueFormatter(value)}
                      {line.unit ?? ""}
                    </title>
                  </circle>
                ),
              )}
              {lastValue !== null ? (
                <text
                  x={xAt(lastIndex) + 5}
                  y={yAt(lastValue)}
                  dominantBaseline="middle"
                  fontSize={9}
                  fontWeight={700}
                  className="fill-on-surface"
                >
                  {valueFormatter(lastValue)}
                  {line.unit ?? ""}
                </text>
              ) : null}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function Legend({ series }: { series: Series[] }) {
  return (
    <div className="flex flex-wrap gap-3 px-1">
      {series.map((line) => (
        <span key={line.id} className="flex items-center gap-1.5 text-[11px] text-on-surface-variant">
          <span
            className="inline-block h-0.5 w-4 shrink-0"
            style={{
              backgroundColor: line.dashed ? "transparent" : line.color,
              borderTop: line.dashed ? `2px dashed ${line.color}` : undefined,
            }}
            aria-hidden="true"
          />
          {line.label}
        </span>
      ))}
    </div>
  );
}

function StatTile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-md border border-outline-variant bg-surface px-3 py-2">
      <span className="block text-[10px] uppercase tracking-wide text-on-surface-variant">{label}</span>
      <span className="block font-data-mono text-body-md font-bold text-on-surface">{value}</span>
      {hint ? <span className="block text-[10px] text-outline">{hint}</span> : null}
    </div>
  );
}

function DataTable({ data }: { data: LangsmithQaTraces }) {
  const latencyByDate = new Map(data.latency.map((row) => [row.date, row]));
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[420px] border-collapse text-left text-body-sm">
        <thead className="bg-surface-container text-label-md text-on-surface-variant">
          <tr>
            <th scope="col" className="px-3 py-2 font-semibold">
              날짜
            </th>
            <th scope="col" className="px-3 py-2 text-right font-semibold">
              성공
            </th>
            <th scope="col" className="px-3 py-2 text-right font-semibold">
              오류
            </th>
            <th scope="col" className="px-3 py-2 text-right font-semibold">
              P50 (초)
            </th>
            <th scope="col" className="px-3 py-2 text-right font-semibold">
              P99 (초)
            </th>
          </tr>
        </thead>
        <tbody>
          {data.daily.map((row) => {
            const latency = latencyByDate.get(row.date);
            return (
              <tr key={row.date} className="border-t border-outline-variant/60">
                <td className="px-3 py-2 font-data-mono text-[11px] text-on-surface-variant">{row.date}</td>
                <td className="px-3 py-2 text-right font-data-mono">{row.success}</td>
                <td className="px-3 py-2 text-right font-data-mono">{row.error}</td>
                <td className="px-3 py-2 text-right font-data-mono">{latency?.p50_seconds ?? "—"}</td>
                <td className="px-3 py-2 text-right font-data-mono">{latency?.p99_seconds ?? "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const STATUS_BANNER: Record<Exclude<LangsmithQaTraces["status"], "READY">, { tone: string; message: string }> = {
  NOT_CONFIGURED: {
    tone: "border-outline-variant bg-surface-container-low text-on-surface-variant",
    message: "이 배포에는 LangSmith 추적이 설정되어 있지 않습니다 (LANGSMITH_TRACING/LANGSMITH_API_KEY).",
  },
  ERROR: {
    tone: "border-error/40 bg-error-container text-on-error-container",
    message: "LangSmith 조회에 실패했습니다. 자격증명 또는 네트워크를 확인해 주세요.",
  },
};

export default function QaLangsmithPanel() {
  const query = useQuery<LangsmithQaTraces, Error>({
    queryKey: ["qa-langsmith-traces", DAYS],
    queryFn: () => fetchQaLangsmithTraces(DAYS),
    refetchInterval: POLL_MS,
    staleTime: 30_000,
    retry: false,
  });

  const data = query.data ?? null;

  const traceSeries: Series[] = data
    ? [
        { id: "success", label: "성공", color: SUCCESS_COLOR, values: data.daily.map((row) => row.success) },
        { id: "error", label: "오류", color: ERROR_COLOR, values: data.daily.map((row) => row.error) },
      ]
    : [];
  const latencySeries: Series[] = data
    ? [
        {
          id: "p50",
          label: "P50",
          color: P50_COLOR,
          unit: "s",
          values: data.latency.map((row) => row.p50_seconds),
        },
        {
          id: "p99",
          label: "P99",
          color: P99_COLOR,
          dashed: true,
          unit: "s",
          values: data.latency.map((row) => row.p99_seconds),
        },
      ]
    : [];

  return (
    <section
      className="min-w-0 overflow-hidden rounded-lg border border-outline-variant bg-surface-container-lowest shadow-sm"
      aria-labelledby="qa-langsmith-title"
    >
      <div className="flex items-center justify-between gap-3 border-b border-outline-variant bg-surface-container-low px-4 py-2.5">
        <span className="flex min-w-0 items-center gap-2 text-label-md font-label-md text-on-surface-variant">
          <span className="material-symbols-outlined text-[16px]" aria-hidden="true">
            monitoring
          </span>
          <span id="qa-langsmith-title" className="truncate">
            qa.langsmith_traces
          </span>
        </span>
        {data?.project ? <span className="shrink-0 text-[11px] text-outline">project: {data.project}</span> : null}
      </div>

      <div className="space-y-4 p-4 md:p-6">
        {query.isPending ? (
          <p className="m-0 rounded-lg border border-outline-variant bg-surface-container-low p-5 text-sm text-on-surface-variant">
            LangSmith 집계를 불러오는 중입니다…
          </p>
        ) : null}

        {query.isError ? (
          <p role="alert" className="m-0 rounded border border-error/40 bg-error-container px-3 py-2 text-xs text-on-error-container">
            {query.error.message}
          </p>
        ) : null}

        {data && data.status !== "READY" ? (
          <p role="status" className={`m-0 rounded-lg border p-3 text-xs ${STATUS_BANNER[data.status].tone}`}>
            {STATUS_BANNER[data.status].message}
          </p>
        ) : null}

        {data ? (
          <>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
              <StatTile label="트레이스 수" value={`${data.trace_count}건`} hint={`최근 ${data.days}일`} />
              <StatTile
                label="에러율"
                value={data.error_rate_pct !== null ? `${data.error_rate_pct}%` : "—"}
              />
              <StatTile
                label="갱신 시각"
                value={new Date(data.generated_at).toLocaleTimeString("ko-KR")}
              />
            </div>

            <div className="space-y-1.5">
              <h3 className="m-0 text-label-md font-label-md uppercase text-on-surface-variant">
                Trace Count · stage:qa
              </h3>
              <Legend series={traceSeries} />
              <TimeSeriesChart dates={data.daily.map((row) => row.date)} series={traceSeries} valueFormatter={(v) => `${v}`} />
            </div>

            <div className="space-y-1.5">
              <h3 className="m-0 text-label-md font-label-md uppercase text-on-surface-variant">Trace Latency</h3>
              <Legend series={latencySeries} />
              <TimeSeriesChart
                dates={data.latency.map((row) => row.date)}
                series={latencySeries}
                valueFormatter={(v) => v.toFixed(2)}
              />
            </div>

            <details className="group min-w-0 overflow-hidden rounded-lg border border-outline-variant">
              <summary className="flex cursor-pointer list-none items-center gap-2 bg-surface-container-low px-3 py-2 text-xs font-semibold text-on-surface-variant marker:content-none">
                <span
                  className="material-symbols-outlined text-[16px] transition-transform group-open:rotate-180"
                  aria-hidden="true"
                >
                  expand_more
                </span>
                표로 보기
              </summary>
              <div className="border-t border-outline-variant p-2">
                <DataTable data={data} />
              </div>
            </details>
          </>
        ) : null}

        <p className="m-0 border-t border-outline-variant pt-3 text-xs text-on-surface-variant">
          `stage:qa` 태그가 붙은 redacted trace 집계 · prompt/output 미포함 · {POLL_MS / 1000}초마다 자동 갱신
        </p>
      </div>
    </section>
  );
}
