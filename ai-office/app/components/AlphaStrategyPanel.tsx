"use client";

import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  fetchStrategyRuntime,
  setStrategyPower,
  StrategyRuntimeError,
  type StrategyRuntimeSnapshot,
} from "../lib/strategyRuntimeClient";

/**
 * 채택된 페이퍼 전략(mlpipe-paper) 패널.
 *
 * 회계 패널(`AccountingLedgerPanel`)과 같은 이유로 여기 있다 - 퀀트본부도
 * 자기 축(지금 무슨 전략이 실제로 도는가)으로 봐야 한다. 다만 원천이 다르다:
 * `strategy.strategies` 레지스트리는 아직 아무도 안 써서(호출처 0개,
 * `apps/api/strategy_runtime.py` 머리말) "n개의 등록된 전략 중 채택"을 실제
 * 숫자로 말할 방법이 없다. 대신 실제로 떠 있는 컨테이너 1개와 그 컨테이너가
 * 남기는 원장·팩 파일을 그대로 읽는다 - 없는 숫자를 지어내지 않는다
 * (개발 원칙 9).
 */

const POLL_MS = 30_000;

/**
 * 전략 설명·백테스트 결과는 하드코딩이다 - LLM이 매 요청마다 다시 요약할
 * 이유가 없는, 이미 확정된 서술이다(2026-08-23 결정). 도현님이 직접 쓰고
 * 검증한 문구·수치를 그대로 옮긴다.
 */
const STRATEGY_EXPLANATION =
  "한국 시장에서 장중 급등 종목의 다음 1시간은 평균적으로 하락한다. 원인은 두 겹: " +
  "복권처럼 급등주에 몰리는 개인투자자의 과잉반응(행동재무학의 MAX 효과), 그리고 " +
  "변동성완화장치(VI) 단일가 경매가 되돌림의 약 40%를 기계적으로 수행하는 시장 구조. " +
  "즉 심리적 엔진과 제도적 엔진을 동시에 가진 엣지.";

type BacktestRow = { metric: string; value: string; unit: "bp" | "pct" | "count" };

const BACKTEST_RESULTS: BacktestRow[] = [
  { metric: "거래당 평균", value: "+98.96", unit: "bp" },
  { metric: "일평균", value: "+58.57", unit: "bp" },
  { metric: "90% 신뢰하한", value: "+34.5", unit: "bp" },
  { metric: "승률 / 양수 세션", value: "60.3% / 73", unit: "pct" },
  { metric: "거래 수", value: "219", unit: "count" },
];
const BACKTEST_WINDOW_LABEL = "개발기록 · 37거래일";

/**
 * 백테스트 결과 표. 터미널 창처럼 꾸민다 - 이 지표들은 매 렌더마다 계산하는
 * 값이 아니라 한 번 확정된 기록이라, 실시간 대시보드보다는 "로그를 그대로
 * 출력한다"는 인상이 더 정확하다. 막대·정규화 같은 파생 로직은 넣지 않는다 -
 * 다섯 개짜리 고정 표에 굳이 계산을 들일 이유가 없다.
 */
function BacktestResultTable({ rows, windowLabel }: { rows: BacktestRow[]; windowLabel: string }) {
  return (
    <div className="overflow-hidden rounded-lg border border-outline-variant bg-surface font-data-mono shadow-sm">
      <div className="flex items-center gap-2 border-b border-outline-variant bg-surface-container-high px-3 py-2">
        <span className="flex gap-1" aria-hidden="true">
          <span className="h-2.5 w-2.5 rounded-full bg-outline-variant" />
          <span className="h-2.5 w-2.5 rounded-full bg-outline-variant" />
          <span className="h-2.5 w-2.5 rounded-full bg-outline-variant" />
        </span>
        <span className="text-[11px] text-on-surface-variant">
          <span className="text-[color:var(--color-tertiary-container)]">$</span> backtest --window {windowLabel}
        </span>
      </div>
      <table className="w-full border-collapse text-left text-body-sm">
        <thead>
          <tr className="border-b border-outline-variant text-[10px] uppercase tracking-wider text-on-surface-variant">
            <th scope="col" className="px-3.5 py-2 font-semibold">지표</th>
            <th scope="col" className="px-3.5 py-2 text-right font-semibold">값</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.metric} className="border-b border-outline-variant/60 last:border-b-0 hover:bg-surface-container-low/60">
              <th scope="row" className="px-3.5 py-2.5 font-normal text-on-surface-variant">
                {row.metric}
              </th>
              <td className="px-3.5 py-2.5 text-right">
                <span className="font-bold tabular-nums text-[color:var(--color-tertiary-container)]">{row.value}</span>
                {row.unit === "bp" ? <span className="ml-0.5 text-[11px] text-on-surface-variant">bp</span> : null}
                {row.unit === "pct" ? <span className="ml-0.5 text-[11px] text-on-surface-variant">%</span> : null}
                {row.unit === "count" ? <span className="ml-0.5 text-[11px] text-on-surface-variant">건</span> : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Badge({ children, tone }: { children: React.ReactNode; tone?: string }) {
  return (
    <span
      className={`inline-flex items-center whitespace-nowrap rounded-full border px-2.5 py-0.5 text-[10px] font-semibold ${
        tone ?? "border-outline-variant bg-surface-container-lowest text-on-surface-variant"
      }`}
    >
      {children}
    </span>
  );
}

/** bps 값. null은 "아직 체결 없음"과 다른 말이라 대시가 아니라 문장으로 말한다. */
function formatBps(value: number | null | undefined, whenNull: string): string {
  if (value === null || value === undefined) return whenNull;
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}bp`;
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

type VariableRow = { name: string; value: string; note: string };

function VariableTable({ title, rows }: { title: string; rows: VariableRow[] }) {
  if (rows.length === 0) return null;
  return (
    <div className="min-w-0 overflow-hidden rounded-lg border border-outline-variant">
      <div className="border-b border-outline-variant bg-surface-container-low px-3 py-2 text-label-md font-label-md text-on-surface-variant">
        {title}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[420px] border-collapse text-left text-body-sm">
          <thead className="bg-surface-container text-label-md text-on-surface-variant">
            <tr>
              <th scope="col" className="px-3 py-2 font-semibold">변수</th>
              <th scope="col" className="px-3 py-2 font-semibold">값</th>
              <th scope="col" className="px-3 py-2 font-semibold">설명</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.name} className="border-t border-outline-variant/60">
                <td className="px-3 py-2 font-data-mono text-[11px] text-on-surface-variant">{row.name}</td>
                <td className="px-3 py-2 font-data-mono font-semibold text-on-surface">{row.value}</td>
                <td className="px-3 py-2 text-xs text-on-surface-variant">{row.note}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ModelTable({ models }: { models: StrategyRuntimeSnapshot["models"] }) {
  const sides = (["long", "short"] as const).filter((side) => models[side]);
  if (sides.length === 0) return null;
  return (
    <div className="min-w-0 overflow-hidden rounded-lg border border-outline-variant">
      <div className="border-b border-outline-variant bg-surface-container-low px-3 py-2 text-label-md font-label-md text-on-surface-variant">
        모델 구성 (XGBoost)
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[420px] border-collapse text-left text-body-sm">
          <thead className="bg-surface-container text-label-md text-on-surface-variant">
            <tr>
              <th scope="col" className="px-3 py-2 font-semibold">방향</th>
              <th scope="col" className="px-3 py-2 font-semibold">목적함수</th>
              <th scope="col" className="px-3 py-2 text-right font-semibold">트리 수</th>
              <th scope="col" className="px-3 py-2 text-right font-semibold">피처 수</th>
              <th scope="col" className="px-3 py-2 text-right font-semibold">base_score</th>
            </tr>
          </thead>
          <tbody>
            {sides.map((side) => {
              const model = models[side]!;
              return (
                <tr key={side} className="border-t border-outline-variant/60">
                  <td className="px-3 py-2 font-semibold text-on-surface">{side === "long" ? "Long" : "Short"}</td>
                  <td className="px-3 py-2 font-data-mono text-[11px] text-on-surface-variant">{model.objective ?? "—"}</td>
                  <td className="px-3 py-2 text-right font-data-mono">{model.num_trees ?? "—"}</td>
                  <td className="px-3 py-2 text-right font-data-mono">{model.num_feature ?? "—"}</td>
                  <td className="px-3 py-2 text-right font-data-mono text-[11px]">{model.base_score ?? "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function AlphaStrategyPanel() {
  const queryClient = useQueryClient();
  const confirmDialogRef = useRef<HTMLDialogElement>(null);
  const [pendingAction, setPendingAction] = useState<"start" | "stop" | null>(null);

  const query = useQuery<StrategyRuntimeSnapshot, StrategyRuntimeError>({
    queryKey: ["strategy-runtime", "spike-fade"],
    queryFn: fetchStrategyRuntime,
    refetchInterval: POLL_MS,
    staleTime: 0,
    retry: false,
  });
  const data = query.data ?? null;
  const error = query.error ?? null;

  const powerMutation = useMutation({
    mutationFn: setStrategyPower,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["strategy-runtime", "spike-fade"] });
    },
  });

  function requestPower(action: "start" | "stop") {
    setPendingAction(action);
    confirmDialogRef.current?.showModal();
  }

  function confirmPower() {
    if (pendingAction) powerMutation.mutate(pendingAction);
    setPendingAction(null);
  }

  const running = data?.container.running ?? false;
  const controlEnabled = data?.control_enabled ?? false;
  const candidateName = data?.ledger?.candidate || data?.container_name || "spike-fade";
  const pack = data?.pack;
  const ledger = data?.ledger;
  const settings = data?.settings ?? {};

  const subtitle = pack
    ? `학습 세션 ${pack.training_sessions}일 · 피처 ${pack.features.length}개로 게이트(상위 ${Math.round(
        pack.gate_percentile * 100,
      )}%)를 통과해 채택된 최종 알파 전략입니다.`
    : "채택 근거가 되는 학습 팩 정보를 아직 찾지 못했습니다.";

  const variableRows: VariableRow[] = [];
  if (pack) {
    variableRows.push(
      { name: "lambda1_gate_threshold", value: pack.lambda1_gate_threshold.toFixed(6), note: "이 값을 넘는 신호만 진입을 허용하는 게이트" },
      { name: "gate_percentile", value: `${Math.round(pack.gate_percentile * 100)}%`, note: "게이트 임계값을 정할 때 쓴 상위 분위" },
      { name: "training_sessions", value: `${pack.training_sessions}일`, note: "이 팩 학습에 쓰인 거래일 수" },
      { name: "target_session / previous_session", value: `${pack.target_session} / ${pack.previous_session}`, note: "이 팩이 대상으로 하는 세션과 직전 세션" },
      { name: "sealed_final_sessions_loaded", value: pack.sealed_final_sessions_loaded ? "true" : "false", note: pack.sealed_final_sessions_loaded ? "최종 확정 데이터로 학습됨" : "아직 최종 확정 전 데이터 포함 - 참고용" },
    );
  }
  if (settings.PAPER_NOTIONAL_KRW) {
    variableRows.push({ name: "PAPER_NOTIONAL_KRW", value: `${Number(settings.PAPER_NOTIONAL_KRW).toLocaleString("ko-KR")}원`, note: "이 페이퍼 세션의 명목 거래 금액" });
  }
  if (settings.BROKER) {
    variableRows.push({ name: "BROKER", value: settings.BROKER.toUpperCase(), note: "체결을 시뮬레이션하는 브로커 어댑터" });
  }

  return (
    <section
      className="min-w-0 overflow-hidden rounded-lg border border-outline-variant bg-surface-container-lowest shadow-sm"
      aria-labelledby="alpha-strategy-title"
    >
      <div className="flex items-center justify-between gap-3 border-b border-outline-variant bg-surface-container-low px-4 py-2.5">
        <span className="flex min-w-0 items-center gap-2 text-label-md font-label-md text-on-surface-variant">
          <span className="material-symbols-outlined text-[16px]" aria-hidden="true">experiment</span>
          <span className="truncate">quant.adopted_strategy</span>
        </span>
        <div className="flex shrink-0 items-center gap-1.5">
          {ledger ? <Badge>{ledger.mode}</Badge> : null}
        </div>
      </div>

      <div className="space-y-5 p-4 md:p-6">
        {error ? (
          <div
            className={`rounded-lg border p-4 text-sm ${
              error.status === 503
                ? "border-outline-variant bg-surface-container-low text-on-surface-variant"
                : "border-error/40 bg-error-container text-on-error-container"
            }`}
            role={error.status === 503 ? "status" : "alert"}
          >
            <p className="m-0 font-semibold">
              {error.status === 503 ? "전략 컨테이너에 닿지 못했습니다." : "전략 정보를 불러오지 못했습니다."}
            </p>
            <p className="m-0 mt-1">{error.message}</p>
          </div>
        ) : null}

        {query.isPending && !data && !error ? (
          <p className="m-0 rounded-lg border border-outline-variant bg-surface-container-low p-5 text-sm text-on-surface-variant">
            전략 상태를 불러오는 중입니다…
          </p>
        ) : null}

        {data ? (
          <>
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="min-w-0">
                <p className="m-0 text-label-md font-label-md uppercase text-on-surface-variant">
                  Quant · Adopted Alpha Strategy
                </p>
                <h2 id="alpha-strategy-title" className="mt-2 break-words text-headline-md font-headline-md font-bold text-primary">
                  {candidateName}
                </h2>
                <p className="mt-2 max-w-3xl text-body-sm font-body-sm text-on-surface-variant">{subtitle}</p>
              </div>

              <div className="flex shrink-0 flex-col items-end gap-1.5">
                <button
                  type="button"
                  role="switch"
                  aria-checked={running}
                  aria-label={`${data.container_name} 전원`}
                  disabled={!controlEnabled || powerMutation.isPending || !data.container.found}
                  onClick={() => requestPower(running ? "stop" : "start")}
                  className={`flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                    running
                      ? "border-primary bg-secondary-container text-primary"
                      : "border-outline-variant bg-surface text-on-surface-variant"
                  }`}
                >
                  <span className={`h-2 w-2 rounded-full ${running ? "bg-primary" : "bg-outline"}`} aria-hidden="true" />
                  {powerMutation.isPending ? "처리 중…" : running ? "ON" : "OFF"}
                </button>
                <span className="text-[10px] text-outline">
                  {data.container.found
                    ? controlEnabled
                      ? "클릭해서 전원을 바꿀 수 있어요"
                      : "이 배포에서는 조회만 가능합니다"
                    : "컨테이너를 찾을 수 없습니다"}
                </span>
              </div>
            </div>

            {powerMutation.isError ? (
              <p role="alert" className="m-0 rounded border border-error/40 bg-error-container px-3 py-2 text-xs text-on-error-container">
                전원 조작 실패: {(powerMutation.error as StrategyRuntimeError).message}
              </p>
            ) : null}

            <div className="relative overflow-hidden rounded-lg border border-outline-variant bg-surface-container-low pl-5 pr-4 py-4">
              <span className="absolute inset-y-0 left-0 w-1 bg-primary" aria-hidden="true" />
              <div className="flex items-start gap-2.5">
                <span className="material-symbols-outlined mt-0.5 text-[18px] text-primary" aria-hidden="true">insights</span>
                <p className="m-0 text-body-sm font-body-sm leading-relaxed text-on-surface">{STRATEGY_EXPLANATION}</p>
              </div>
            </div>

            <BacktestResultTable rows={BACKTEST_RESULTS} windowLabel={BACKTEST_WINDOW_LABEL} />

            <details className="group min-w-0 overflow-hidden rounded-lg border border-outline-variant bg-surface-container-lowest">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-3 bg-surface-container-low px-4 py-3 marker:content-none">
                <span className="flex min-w-0 items-center gap-2">
                  <span
                    className="material-symbols-outlined text-[18px] text-on-surface-variant transition-transform group-open:rotate-180"
                    aria-hidden="true"
                  >
                    expand_more
                  </span>
                  <span className="min-w-0">
                    <span className="block text-title-md font-title-md font-semibold text-primary">자세히 보기</span>
                    <span className="block text-[11px] text-outline">백테스트 성과 · 모델 구성 · 설정 변수</span>
                  </span>
                </span>
                <span className="shrink-0 text-xs text-on-surface-variant">
                  {ledger ? `세션 ${ledger.session}` : "—"}
                </span>
              </summary>

              <div className="space-y-4 border-t border-outline-variant p-4">
                {ledger ? (
                  <div>
                    <h3 className="m-0 mb-2 text-label-md font-label-md uppercase text-on-surface-variant">
                      실시간 페이퍼 성과 (오늘 세션)
                    </h3>
                    <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                      <StatTile label="체결" value={`${ledger.summary.trades_closed}건`} />
                      <StatTile label="미체결" value={`${ledger.summary.trades_open}건`} />
                      <StatTile
                        label="평균 체결 성과"
                        value={formatBps(ledger.summary.mean_trade_bps, "체결 없음")}
                      />
                      <StatTile label="슬롯당 성과" value={formatBps(ledger.summary.session_bps_per_slot, "0.00bp")} />
                    </div>
                    <p className="m-0 mt-2 text-[11px] text-outline">
                      {ledger._source_file} · {new Date(ledger.generated_at).toLocaleString("ko-KR")} 기준 · PAPER
                      전용, 실제 주문이 아닙니다.
                    </p>
                  </div>
                ) : (
                  <p className="m-0 text-sm text-on-surface-variant">이 컨테이너의 원장 파일을 아직 찾지 못했습니다.</p>
                )}

                <ModelTable models={data.models} />
                <VariableTable title="전략에 포함되는 변수" rows={variableRows} />

                {pack ? (
                  <div>
                    <h3 className="m-0 mb-2 text-label-md font-label-md uppercase text-on-surface-variant">
                      입력 피처 ({pack.features.length}개)
                    </h3>
                    <div className="flex flex-wrap gap-1.5">
                      {pack.features.map((feature) => (
                        <span
                          key={feature}
                          className="rounded border border-outline-variant bg-surface px-2 py-0.5 font-data-mono text-[10px] text-on-surface-variant"
                        >
                          {feature}
                        </span>
                      ))}
                    </div>
                  </div>
                ) : null}
              </div>
            </details>
          </>
        ) : null}

        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-t border-outline-variant pt-3 text-xs text-on-surface-variant">
          <span>컨테이너·원장 파일 기준 · 공식 전략 레지스트리는 아직 이 컨테이너를 추적하지 않습니다</span>
          <span>{POLL_MS / 1000}초마다 자동 갱신</span>
        </div>
      </div>

      {/* 전원 조작 확인. 살아있는 페이퍼 트레이딩 프로세스를 끄고 켜는 실제 동작이라
          한 번 확인을 거친다 - MandateConfig.tsx의 초기화 확인 다이얼로그와 같은 패턴. */}
      <dialog
        ref={confirmDialogRef}
        aria-labelledby="strategy-power-dialog-title"
        className="m-auto w-[min(24rem,90vw)] rounded-lg border border-outline-variant bg-surface-container-lowest p-6 text-on-surface shadow-sm backdrop:bg-black/40"
      >
        <h2 id="strategy-power-dialog-title" className="m-0 flex items-center gap-2 text-body-md font-body-md font-bold text-on-surface">
          <span className="material-symbols-outlined text-[20px]" aria-hidden="true">power_settings_new</span>
          {pendingAction === "stop" ? "전략을 끌까요?" : "전략을 켤까요?"}
        </h2>
        <p className="mt-3 text-body-sm font-body-sm leading-relaxed text-on-surface-variant">
          {pendingAction === "stop"
            ? `${data?.container_name ?? "전략"} 컨테이너를 정지합니다. 페이퍼 매매만 멈추며, 지금까지의 체결 기록은 그대로 남습니다.`
            : `${data?.container_name ?? "전략"} 컨테이너를 다시 시작합니다.`}
        </p>
        <form method="dialog" className="mt-5 flex justify-end gap-2">
          <button className="rounded border border-outline-variant px-4 py-2 text-body-sm font-bold transition-colors hover:bg-surface-container-high">
            취소
          </button>
          <button
            onClick={confirmPower}
            className="rounded bg-primary px-4 py-2 text-body-sm font-bold text-on-primary transition-opacity hover:opacity-90"
          >
            확인
          </button>
        </form>
      </dialog>
    </section>
  );
}
