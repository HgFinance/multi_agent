"use client";

import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useBffFeed } from "./bffClient";
import type { OperationsRuntime } from "./readModel";
import {
  decidePortfolioRecommendation,
  fetchPortfolioUniverses,
  startPortfolioRecommendation,
  type PortfolioInterviewInput,
  type PortfolioUniverseOption,
} from "./portfolioClient";

type InstrumentRecommendation = {
  portfolio_id: string;
  symbol: string;
  exchange: string;
  name: string;
  asset_class: string;
  target_weight: string;
  target_amount: string;
  expected_return: string | null;
  expected_return_status: string;
  expected_return_basis: string;
  data_status: string;
};

type Recommendation = {
  portfolio_id: string;
  name: string;
  risk_band: string;
  fit_score: number;
  target_allocations: Record<string, string>;
  target_amounts: Record<string, string>;
  reasons: string[];
  evidence_refs: string[];
  instrument_recommendations?: InstrumentRecommendation[];
};

type TaskPlan = {
  category?: string;
  original_query?: string;
  rewritten_query?: string;
  requested_departments?: string[];
  matched_terms?: Record<string, string[]>;
};

type DepartmentReport = {
  status?: string;
  executed?: number;
  worker_ids?: string[];
};

type RuntimeResult = {
  suitability?: unknown;
  instrument_recommendations?: unknown;
  unresolved_asset_classes?: string[];
  instrument_recommendations_status?: string;
  universe?: { name?: string; status?: string; source?: string } | null;
  forecast_notice?: string;
  task_plan?: TaskPlan;
  department_reports?: Record<string, DepartmentReport>;
  user_query?: string;
};

type KanbanStage = {
  code: string;
  label: string;
  runtimeCode: string;
};

const KANBAN_STAGES: KanbanStage[] = [
  { code: "research", label: "Research", runtimeCode: "research-department" },
  { code: "trading", label: "Trading", runtimeCode: "trading-department" },
  { code: "risk", label: "Risk", runtimeCode: "risk-management" },
  { code: "qa", label: "QA", runtimeCode: "qa-department" },
  {
    code: "accounting",
    label: "Accounting",
    runtimeCode: "accounting-portfolio-department",
  },
  { code: "ceo", label: "CEO", runtimeCode: "ceo-agent" },
];

const CATEGORY_OPTIONS = [
  ["PORTFOLIO_RECOMMENDATION", "포트폴리오 추천"],
  ["MARKET_RESEARCH", "시장·종목 리서치"],
  ["RISK_REVIEW", "위험·손실 검토"],
  ["TAX_LIQUIDITY", "세금·현금흐름"],
  ["REBALANCING_PROPOSAL", "리밸런싱 제안 · 주문 없음"],
] as const;

const ASSET_LABEL: Record<string, string> = {
  KOREA_EQUITY: "국내 주식",
  GLOBAL_EQUITY: "글로벌 주식",
  SHORT_TERM_BOND: "단기채권",
  LEVERAGED_ETF: "레버리지 ETF",
  SHORT_EXPOSURE: "인버스·공매도 노출",
  DERIVATIVES_HEDGE: "파생상품 헤지",
};

function asRecommendations(value: unknown): Recommendation[] {
  if (
    typeof value !== "object" ||
    value === null ||
    !Array.isArray((value as { recommendations?: unknown }).recommendations)
  ) {
    return [];
  }

  return (value as { recommendations: unknown[] }).recommendations.filter(
    (item): item is Recommendation =>
      typeof item === "object" &&
      item !== null &&
      typeof (item as Recommendation).portfolio_id === "string" &&
      typeof (item as Recommendation).name === "string",
  );
}

function asInstrumentRecommendations(value: unknown): InstrumentRecommendation[] {
  if (!Array.isArray(value)) return [];

  return value.filter(
    (item): item is InstrumentRecommendation =>
      typeof item === "object" &&
      item !== null &&
      typeof (item as InstrumentRecommendation).portfolio_id === "string" &&
      typeof (item as InstrumentRecommendation).symbol === "string",
  );
}

function percent(value: string | number | null): string {
  const number = Number(value);
  return Number.isFinite(number) ? `${(number * 100).toFixed(0)}%` : "—";
}

function amount(value: string, currency: string): string {
  const number = Number(value);
  if (!Number.isFinite(number)) return `${currency} —`;
  return `${currency} ${new Intl.NumberFormat("ko-KR", {
    maximumFractionDigits: 2,
  }).format(number)}`;
}

function stageStatus(
  selected: boolean,
  current: string | undefined,
  report: DepartmentReport | undefined,
): { label: string; tone: string } {
  if (!selected) return { label: "미호출", tone: "skipped" };
  const status = String(report?.status ?? current ?? "QUEUED").toUpperCase();
  if (status === "RUNNING" || status === "QUEUED") {
    return { label: "실행 중", tone: "running" };
  }
  if (status === "COMPLETED" || status === "DONE") {
    return { label: "완료", tone: "done" };
  }
  if (status === "SKIPPED") return { label: "미호출", tone: "skipped" };
  return { label: "보류", tone: "blocked" };
}

function shortRunId(runId: string | null): string {
  if (!runId) return "—";
  return runId.length > 16 ? `…${runId.slice(-12)}` : runId;
}

function PortfolioKanban({
  runtime,
  result,
  observedAt,
}: {
  runtime: OperationsRuntime;
  result: RuntimeResult | null | undefined;
  observedAt: string | undefined;
}) {
  const taskPlan = result?.task_plan;
  const requested = new Set(taskPlan?.requested_departments ?? []);
  const reports = result?.department_reports ?? {};
  const eventMessages = runtime.messages.slice(-4).reverse();

  return (
    <section className="runtime-kanban" aria-label="CEO task routing Kanban">
      <div className="kanban-heading">
        <div>
          <p className="eyebrow">CEO TASK ROUTING · LIVE RUNTIME</p>
          <h3>부서별 작업 보드</h3>
        </div>
        <div className="kanban-run-meta">
          <span className="runtime-live-dot" aria-hidden="true" />
          <code title={runtime.run_id ?? undefined}>run {shortRunId(runtime.run_id)}</code>
        </div>
      </div>
      <p className="kanban-query">
        {taskPlan?.rewritten_query || result?.user_query || "CEO가 구조화된 사용자 프로필을 기준으로 업무를 배정합니다."}
      </p>
      <div className="kanban-grid">
        {KANBAN_STAGES.map((stage) => {
          const department = runtime.departments[stage.runtimeCode];
          const report = reports[stage.code] ?? reports[stage.runtimeCode];
          const selected = requested.has(stage.code);
          const status = stageStatus(selected, department?.status, report);
          const activeWorkerIds = department?.active_worker_ids ?? [];
          const completedWorkerCount = report?.executed ?? report?.worker_ids?.length ?? 0;

          return (
            <article className={`kanban-column ${status.tone}`} key={stage.code}>
              <div className="kanban-column-head">
                <span className="kanban-stage-number">{String(KANBAN_STAGES.indexOf(stage) + 1).padStart(2, "0")}</span>
                <strong>{stage.label}</strong>
                <span className={`kanban-status ${status.tone}`}>{status.label}</span>
              </div>
              {activeWorkerIds.length > 0 ? (
                <div className="kanban-workers">
                  {activeWorkerIds.slice(0, 3).map((workerId) => (
                    <code key={workerId}>{workerId}</code>
                  ))}
                </div>
              ) : (
                <p className="kanban-empty">
                  {status.tone === "done"
                    ? `${completedWorkerCount} Worker 완료`
                    : status.tone === "skipped"
                      ? "이번 요청에 배정하지 않음"
                      : "Worker 대기 중"}
                </p>
              )}
            </article>
          );
        })}
      </div>
      {eventMessages.length > 0 && (
        <div className="kanban-events" aria-live="polite">
          <span className="kanban-events-label">최근 실행 이벤트</span>
          {eventMessages.map((event) => (
            <p key={event.id}>
              <time>{new Date(event.occurred_at).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" })}</time>
              {event.text}
            </p>
          ))}
        </div>
      )}
      <small className="kanban-footnote">
        {runtime.status === "COMPLETED" || runtime.status === "WAITING_APPROVAL"
          ? `실행 완료 · ${observedAt ? new Date(observedAt).toLocaleTimeString("ko-KR") : "최신 projection"}`
          : "실시간 runtime projection · 캐시 아님"}
      </small>
    </section>
  );
}

export default function PortfolioInterviewPanel() {
  const { snapshot, connection, error, refresh } = useBffFeed();
  const [input, setInput] = useState<PortfolioInterviewInput>({
    user_id: "web-user",
    mindset: "BALANCED",
    experience: "BEGINNER",
    investment_horizon_years: 3,
    max_drawdown_pct: "0.10",
    investment_amount: "1000000",
    currency: "KRW",
    universe_id: "KOREA_GLOBAL_MIXED",
    category: "PORTFOLIO_RECOMMENDATION",
    query: "",
  });
  const [universes, setUniverses] = useState<PortfolioUniverseOption[]>([]);
  const [busy, setBusy] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const [approvalBusy, setApprovalBusy] = useState(false);
  const runtime = snapshot?.operations?.runtime;
  const running = runtime?.status === "QUEUED" || runtime?.status === "RUNNING";
  const result = runtime?.result as RuntimeResult | null | undefined;
  const recommendations = useMemo(() => asRecommendations(result?.suitability), [result?.suitability]);
  const instrumentRecommendations = useMemo(
    () => asInstrumentRecommendations(result?.instrument_recommendations),
    [result?.instrument_recommendations],
  );
  const approval = runtime?.approval;
  const visibleError = submitError || (!runtime?.run_id && error ? error : "");
  const resultCurrency =
    typeof (result?.suitability as { currency?: unknown } | undefined)?.currency === "string"
      ? String((result?.suitability as { currency: string }).currency)
      : input.currency;

  useEffect(() => {
    let active = true;
    void fetchPortfolioUniverses()
      .then((payload) => {
        if (active) setUniverses(payload.universes);
      })
      .catch(() => {
        // The backend validates the selected universe on submit.
      });
    return () => {
      active = false;
    };
  }, []);

  async function decide(decision: "APPROVE" | "REJECT") {
    if (!runtime?.run_id) return;
    setApprovalBusy(true);
    setSubmitError("");
    try {
      await decidePortfolioRecommendation(runtime.run_id, decision);
      await refresh();
    } catch (cause) {
      setSubmitError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setApprovalBusy(false);
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setSubmitError("");
    try {
      await startPortfolioRecommendation(input);
      await refresh();
    } catch (cause) {
      setSubmitError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="win portfolio-interview" id="portfolio-interview" aria-labelledby="portfolio-interview-title">
      <div className="win-bar">
        <span>🧭 portfolio.suitability.interview</span>
        <span className="window-controls" aria-hidden="true">— ✕</span>
      </div>
      <div className="win-body">
        <div className="section-heading portfolio-heading">
          <div>
            <p className="eyebrow">USER PROFILE → CEO ROUTER → ADVISORY PORTFOLIO</p>
            <h2 id="portfolio-interview-title">사용자에게 맞는 포트폴리오 받기</h2>
          </div>
          <span className={`status-pill ${running ? "status-running" : ""}`}>
            {busy ? "요청 중" : running ? "실행 중" : result ? runtime?.status : connection.toUpperCase()}
          </span>
        </div>
        <p className="dash-note portfolio-intro">
          성향·경험·기간·손실 한도와 자유 질문을 함께 받아, CEO가 필요한 부서와 Worker만 배정합니다.
        </p>

        <form id="portfolio-interview-form" className="portfolio-form" onSubmit={submit}>
          <label>
            사용자 식별자
            <input value={input.user_id} onChange={(event) => setInput({ ...input, user_id: event.target.value })} required />
          </label>
          <label>
            투자 성향
            <select value={input.mindset} onChange={(event) => setInput({ ...input, mindset: event.target.value as PortfolioInterviewInput["mindset"] })}>
              <option value="SAFETY_FIRST">안전 우선</option>
              <option value="BALANCED">균형형</option>
              <option value="RISK_SEEKING">성장·위험 감수</option>
            </select>
          </label>
          <label>
            투자 경험
            <select value={input.experience} onChange={(event) => setInput({ ...input, experience: event.target.value as PortfolioInterviewInput["experience"] })}>
              <option value="BEGINNER">처음 접함</option>
              <option value="INTERMEDIATE">어느 경험</option>
              <option value="EXPERIENCED">경험 많음</option>
            </select>
          </label>
          <label>
            분석 카테고리
            <select value={input.category} onChange={(event) => setInput({ ...input, category: event.target.value })}>
              {CATEGORY_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
          <label>
            투자 예정 기간(년)
            <input type="number" min="1" max="100" value={input.investment_horizon_years} onChange={(event) => setInput({ ...input, investment_horizon_years: Number(event.target.value) })} required />
          </label>
          <label>
            투자 가능 금액
            <input inputMode="decimal" min="1" value={input.investment_amount} onChange={(event) => setInput({ ...input, investment_amount: event.target.value })} required />
          </label>
          <label>
            통화
            <select value={input.currency} onChange={(event) => setInput({ ...input, currency: event.target.value as PortfolioInterviewInput["currency"] })}>
              <option value="KRW">KRW · 원화</option>
              <option value="USD">USD · 달러</option>
              <option value="EUR">EUR · 유로</option>
            </select>
          </label>
          <label>
            투자 유니버스
            <select value={input.universe_id} onChange={(event) => setInput({ ...input, universe_id: event.target.value })} required>
              {universes.length > 0 ? universes.map((universe) => <option key={universe.universe_id} value={universe.universe_id}>{universe.name} · {universe.instrument_count}개 · {universe.status}</option>) : <option value="KOREA_GLOBAL_MIXED">국내·글로벌 혼합 유니버스</option>}
            </select>
          </label>
          <label>
            감내 가능한 최대 손실률
            <input type="number" min="1" max="100" step="1" value={Number(input.max_drawdown_pct) * 100} onChange={(event) => setInput({ ...input, max_drawdown_pct: (Number(event.target.value) / 100).toFixed(4) })} required />
            <small>예: 10 = 최대 -10%</small>
          </label>
          <label className="portfolio-query">
            사용자 질문·조건
            <input type="text" value={input.query} onChange={(event) => setInput({ ...input, query: event.target.value })} maxLength={2000} placeholder="예: 국내 반도체 중심으로 3년 투자하고 손실 위험과 근거를 설명해줘" />
            <small>비워도 됩니다. 카테고리와 프로필을 기준으로 CEO가 업무를 배정합니다.</small>
          </label>
        </form>

        {visibleError && <p className="form-error">⚠️ {visibleError}</p>}
        {runtime?.phase && <p className="runtime-phase"><b>현재 단계:</b> {runtime.phase}</p>}
        {runtime?.run_id && <PortfolioKanban runtime={runtime} result={result} observedAt={snapshot?.operations?.observed_at} />}

        {result && (
          <div className="portfolio-result" aria-label="포트폴리오 추천 결과">
            <div className="result-heading">
              <div>
                <p className="eyebrow">BACKEND SUITABILITY RESULT</p>
                <h3>추천 후보와 종목 티커</h3>
              </div>
              <span className={`status-pill ${result.instrument_recommendations_status === "COMPLETE" ? "status-ready" : ""}`}>
                {result.instrument_recommendations_status === "COMPLETE" ? "검토 가능" : "추가 데이터 필요"}
              </span>
            </div>
            {result.universe && <p className="universe-summary">유니버스: <b>{result.universe.name}</b> · {result.universe.status} · {result.universe.source}</p>}
            {result.unresolved_asset_classes?.length ? <p className="risk-warning">선택한 유니버스에 없는 자산군: {result.unresolved_asset_classes.map((asset) => ASSET_LABEL[asset] ?? asset).join(", ")}</p> : null}
            {recommendations.length === 0 ? (
              <p className="dash-note">백엔드가 확정한 적합성 후보가 없습니다.</p>
            ) : recommendations.map((item) => {
              const rootRows = instrumentRecommendations.filter((instrument) => instrument.portfolio_id === item.portfolio_id);
              const rows = rootRows.length > 0 ? rootRows : item.instrument_recommendations ?? [];
              const highRisk = Object.keys(item.target_allocations).some((asset) => ["LEVERAGED_ETF", "SHORT_EXPOSURE", "DERIVATIVES_HEDGE"].includes(asset));
              return (
                <article className="portfolio-recommendation" key={item.portfolio_id}>
                  <div><strong>{item.name}</strong><code>{item.portfolio_id}</code></div>
                  <span className="score">{item.fit_score}점 · {item.risk_band}</span>
                  <p>목표 비중: {Object.entries(item.target_allocations).map(([asset, weight]) => `${ASSET_LABEL[asset] ?? asset} ${percent(weight)}`).join(" · ")}</p>
                  <p>목표 금액: {Object.entries(item.target_amounts).map(([asset, value]) => `${ASSET_LABEL[asset] ?? asset} ${amount(value, resultCurrency)}`).join(" · ")}</p>
                  <div className="instrument-recommendations">
                    <strong>추천 종목</strong>
                    {rows.length > 0 ? rows.map((instrument) => (
                      <div className="instrument-row" key={`${item.portfolio_id}-${instrument.exchange}-${instrument.symbol}`}>
                        <span><b className="ticker">{instrument.symbol}</b><span>{instrument.name}</span><code>{instrument.exchange}</code></span>
                        <span>{percent(instrument.target_weight)} · {amount(instrument.target_amount, resultCurrency)}</span>
                        <small>{instrument.expected_return ? `${percent(instrument.expected_return)} 예상` : "예상 수익률 미산출"} · {instrument.data_status}</small>
                      </div>
                    )) : <small>선택한 유니버스에서 매칭되는 티커가 없습니다.</small>}
                  </div>
                  {highRisk && <p className="risk-warning">고위험 자산군이 포함되어 있어 별도 검토가 필요합니다.</p>}
                  <p className="recommendation-reason">{item.reasons.join(" · ")}</p>
                  <small>근거: {item.evidence_refs.join(", ")}</small>
                </article>
              );
            })}
            {result.forecast_notice && <p className="forecast-notice">{result.forecast_notice}</p>}
          </div>
        )}

        {recommendations.length > 0 && approval?.status === "PENDING" && (
          <div className="approval-actions">
            <p className="dash-note">추천 내용을 확인한 뒤 사용자 승인 단계로 진행합니다. 승인해도 주문·권한 변경·원장 변경은 수행하지 않습니다.</p>
            <div>
              <button type="button" className="btn-primary" onClick={() => void decide("APPROVE")} disabled={approvalBusy}>{approvalBusy ? "처리 중" : "추천 승인"}</button>
              <button type="button" className="btn-ghost" onClick={() => void decide("REJECT")} disabled={approvalBusy}>추천 거절</button>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
