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
};

type TaskPlan = {
  rewritten_query?: string;
  requested_departments?: string[];
};

type DepartmentReport = {
  status?: string;
  executed?: number;
  worker_ids?: string[];
};

export type RuntimeResult = {
  suitability?: unknown;
  instrument_recommendations?: unknown;
  instrument_recommendations_status?: string;
  task_plan?: TaskPlan;
  department_reports?: Record<string, DepartmentReport>;
  user_query?: string;
};

type KanbanStage = { code: string; label: string; runtimeCode: string };

const KANBAN_STAGES: KanbanStage[] = [
  { code: "research", label: "Research", runtimeCode: "research-department" },
  { code: "trading", label: "Trading", runtimeCode: "trading-department" },
  { code: "risk", label: "Risk", runtimeCode: "risk-management" },
  { code: "qa", label: "QA", runtimeCode: "qa-department" },
  { code: "accounting", label: "Accounting", runtimeCode: "accounting-portfolio-department" },
  { code: "ceo", label: "CEO", runtimeCode: "ceo-agent" },
];

const CATEGORY_OPTIONS = [
  ["PORTFOLIO_RECOMMENDATION", "포트폴리오 추천"],
  ["MARKET_RESEARCH", "시장·종목 리서치"],
  ["RISK_REVIEW", "위험·손실 검토"],
  ["TAX_LIQUIDITY", "세금·현금흐름"],
  ["REBALANCING_PROPOSAL", "리밸런싱 제안 · 주문 없음"],
] as const;

function asRecommendations(value: unknown): Recommendation[] {
  if (typeof value !== "object" || value === null) return [];
  const recommendations = (value as { recommendations?: unknown }).recommendations;
  if (!Array.isArray(recommendations)) return [];
  return recommendations.filter(
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

function explainPortfolioConnectionError(value: string): string {
  if (value.toLowerCase().includes("failed to fetch")) {
    return "BFF에 연결할 수 없습니다. 저장소 루트에서 8001 포트 서버가 실행 중인지 확인하세요.";
  }
  return value;
}

function shortRunId(runId: string | null): string {
  if (!runId) return "—";
  return runId.length > 16 ? `…${runId.slice(-12)}` : runId;
}

function stageStatus(selected: boolean, current: string | undefined, report?: DepartmentReport) {
  if (!selected) return { label: "미호출", tone: "skipped" };
  const status = String(report?.status ?? current ?? "QUEUED").toUpperCase();
  if (status === "RUNNING" || status === "QUEUED") return { label: "실행 중", tone: "running" };
  if (status === "COMPLETED" || status === "DONE") return { label: "완료", tone: "done" };
  if (status === "SKIPPED") return { label: "미호출", tone: "skipped" };
  return { label: "보류", tone: "blocked" };
}

export function PortfolioKanban({
  runtime,
  result,
  observedAt,
}: {
  runtime: OperationsRuntime;
  result: RuntimeResult | null | undefined;
  observedAt?: string;
}) {
  const requested = new Set(result?.task_plan?.requested_departments ?? []);
  const reports = result?.department_reports ?? {};
  const eventMessages = runtime.messages.slice(-5).reverse();

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
        {result?.task_plan?.rewritten_query || result?.user_query || "CEO가 사용자 요청을 부서 업무로 배정하고 있습니다."}
      </p>
      <div className="kanban-grid">
        {KANBAN_STAGES.map((stage, index) => {
          const department = runtime.departments[stage.runtimeCode];
          const report = reports[stage.code] ?? reports[stage.runtimeCode];
          const activeWorkerIds = department?.active_worker_ids ?? [];
          const hasMessages = runtime.messages.some((message) => message.department_code === stage.runtimeCode);
          const hasRuntimeEvidence = Boolean(
            report ||
              activeWorkerIds.length > 0 ||
              hasMessages ||
              (department && !["IDLE", "OFFLINE", "SKIPPED"].includes(department.status.toUpperCase())),
          );
          const selected = requested.size > 0 ? requested.has(stage.code) : hasRuntimeEvidence;
          const status = stageStatus(selected, department?.status, report);
          const completedWorkerCount = report?.executed ?? report?.worker_ids?.length ?? 0;
          return (
            <article className={`kanban-column ${status.tone}`} key={stage.code}>
              <div className="kanban-column-head">
                <span className="kanban-stage-number">{String(index + 1).padStart(2, "0")}</span>
                <strong>{stage.label}</strong>
                <span className={`kanban-status ${status.tone}`}>{status.label}</span>
              </div>
              {activeWorkerIds.length > 0 ? (
                <div className="kanban-workers">
                  {activeWorkerIds.slice(0, 3).map((workerId) => <code key={workerId}>{workerId}</code>)}
                </div>
              ) : (
                <p className="kanban-empty">
                  {status.tone === "done" ? `${completedWorkerCount} Worker 완료` : status.tone === "skipped" ? "이번 요청에 배정하지 않음" : "Worker 대기 중"}
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
        {runtime.status === "COMPLETED" || runtime.status === "WAITING_APPROVAL" ? `실행 완료 · ${observedAt ? new Date(observedAt).toLocaleTimeString("ko-KR") : "최신 projection"}` : "실시간 runtime projection · 캐시 아님"}
      </small>
    </section>
  );
}

export function PortfolioResultConsole() {
  const { snapshot, refresh } = useBffFeed();
  const [approvalBusy, setApprovalBusy] = useState(false);
  const [approvalError, setApprovalError] = useState("");
  const runtime = snapshot?.operations?.runtime;
  const result = runtime?.result as RuntimeResult | null | undefined;
  const recommendations = useMemo(() => asRecommendations(result?.suitability), [result?.suitability]);
  const domesticRows = useMemo(
    () => asInstrumentRecommendations(result?.instrument_recommendations).filter((item) => item.asset_class === "KOREA_EQUITY"),
    [result?.instrument_recommendations],
  );
  if (!runtime?.run_id) return null;

  async function decide(decision: "APPROVE" | "REJECT") {
    if (!runtime?.run_id) return;
    setApprovalBusy(true);
    setApprovalError("");
    try {
      await decidePortfolioRecommendation(runtime.run_id, decision);
      await refresh();
    } catch (cause) {
      setApprovalError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setApprovalBusy(false);
    }
  }

  return (
    <section className="ceo-recommendation" id="ceo-approval" aria-label="CEO 포트폴리오 추천 결과">
      <div className="ceo-recommendation-heading">
        <div><span className="tiny-label">PORTFOLIO ADVISORY</span><strong>국내 주식 추천 결과</strong></div>
        <span className={`mini-badge ${result?.instrument_recommendations_status === "COMPLETE" ? "mint" : "yellow"}`}>
          {result?.instrument_recommendations_status === "COMPLETE" ? "검토 가능" : "데이터 확인"}
        </span>
      </div>
      {!result ? (
        <p className="ceo-recommendation-empty">CEO가 부서 결과를 취합하고 있습니다.</p>
      ) : recommendations.length === 0 ? (
        <p className="ceo-recommendation-empty">적합성 후보가 아직 없습니다.</p>
      ) : (
        <div className="ceo-recommendation-list">
          {recommendations.slice(0, 3).map((candidate) => {
            const rows = domesticRows.filter((row) => row.portfolio_id === candidate.portfolio_id);
            return (
              <article key={candidate.portfolio_id} className="ceo-recommendation-item">
                <div className="ceo-recommendation-item-title"><b>{candidate.name}</b><span>{candidate.fit_score}점 · {candidate.risk_band}</span></div>
                <p>{candidate.reasons[0] ?? "사용자 투자 프로필과 국내 주식 유니버스를 기준으로 검토했습니다."}</p>
                <div className="ceo-ticker-list">
                  {rows.length > 0 ? rows.map((row) => <span className="ceo-ticker" key={`${row.exchange}-${row.symbol}`}><b className="ticker">{row.symbol}</b><small>{row.name} · {percent(row.target_weight)}</small></span>) : <small>국내 종목 티커를 확인 중입니다.</small>}
                </div>
              </article>
            );
          })}
        </div>
      )}
      {runtime.approval?.status === "PENDING" && recommendations.length > 0 && (
        <div className="ceo-recommendation-actions">
          <small>사용자 승인 후에도 주문·원장 변경은 수행하지 않습니다.</small>
          <div>
            <button type="button" className="btn-primary" onClick={() => void decide("APPROVE")} disabled={approvalBusy}>{approvalBusy ? "처리 중" : "추천 승인"}</button>
            <button type="button" className="btn-ghost" onClick={() => void decide("REJECT")} disabled={approvalBusy}>거절</button>
          </div>
        </div>
      )}
      {approvalError && <small className="form-error">⚠️ {approvalError}</small>}
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
    universe_id: "KOREA_EQUITY_WATCHLIST",
    category: "PORTFOLIO_RECOMMENDATION",
    include_stock: true,
    include_derivatives: false,
    query: "",
  });
  const [universes, setUniverses] = useState<PortfolioUniverseOption[]>([]);
  const [busy, setBusy] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const runtime = snapshot?.operations?.runtime;
  const running = runtime?.status === "QUEUED" || runtime?.status === "RUNNING";
  const connectionError = submitError || (!runtime?.run_id && error) || "";

  useEffect(() => {
    let active = true;
    void fetchPortfolioUniverses().then((payload) => {
      if (!active) return;
        const domestic = payload.universes.filter((item) => item.universe_id === "KOREA_EQUITY_WATCHLIST");
        setUniverses(domestic);
    }).catch(() => undefined);
    return () => { active = false; };
  }, []);

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
      <div className="win-bar"><span>🧭 portfolio.suitability.interview</span><span className="window-controls" aria-hidden="true">— ✕</span></div>
      <div className="win-body">
        <div className="section-heading portfolio-heading">
          <div><p className="eyebrow">USER PROFILE → CEO ROUTER → DOMESTIC EQUITY</p><h2 id="portfolio-interview-title">국내 주식 포트폴리오 받기</h2></div>
          <span className={`status-pill ${running ? "status-running" : ""}`}>{busy ? "요청 중" : running ? "실행 중" : runtime?.status ?? connection.toUpperCase()}</span>
        </div>
        <p className="dash-note portfolio-intro">질문과 투자 성향을 입력하면 CEO가 필요한 부서와 Worker만 배정합니다.</p>
        <form id="portfolio-interview-form" className="portfolio-form portfolio-form-compact" onSubmit={submit}>
          <label>분석 카테고리<select value={input.category} onChange={(event) => setInput({ ...input, category: event.target.value })}>{CATEGORY_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label>국내 주식 유니버스<select value={input.universe_id} onChange={(event) => setInput({ ...input, universe_id: event.target.value })} required>{universes.length > 0 ? universes.map((universe) => <option key={universe.universe_id} value={universe.universe_id}>{universe.name} · {universe.instrument_count}개</option>) : <option value="KOREA_EQUITY_WATCHLIST">국내 주식 Watchlist</option>}</select></label>
          <fieldset className="portfolio-asset-toggles"><legend>표시할 자산</legend><label className="portfolio-toggle"><input type="checkbox" checked={input.include_stock} onChange={(event) => setInput({ ...input, include_stock: event.target.checked })} /><span>국내 주식</span><small>{input.include_stock ? "ON" : "OFF"}</small></label><label className="portfolio-toggle"><input type="checkbox" checked={input.include_derivatives} onChange={(event) => setInput({ ...input, include_derivatives: event.target.checked })} /><span>파생상품</span><small>{input.include_derivatives ? "ON" : "OFF"}</small></label></fieldset>
          <label className="portfolio-query">사용자 질문·조건<input type="text" value={input.query} onChange={(event) => setInput({ ...input, query: event.target.value })} maxLength={2000} placeholder="예: 3년 동안 삼성전자·SK하이닉스 중심으로 검토해줘" /><small>비워도 됩니다. 기본값은 국내 주식 분석입니다.</small></label>
          <details className="portfolio-advanced"><summary>투자 프로필 상세 입력</summary><div className="portfolio-advanced-grid">
            <label>사용자 식별자<input value={input.user_id} onChange={(event) => setInput({ ...input, user_id: event.target.value })} required /></label>
            <label>투자 성향<select value={input.mindset} onChange={(event) => setInput({ ...input, mindset: event.target.value as PortfolioInterviewInput["mindset"] })}><option value="SAFETY_FIRST">안전 우선</option><option value="BALANCED">균형형</option><option value="RISK_SEEKING">성장·위험 감수</option></select></label>
            <label>투자 경험<select value={input.experience} onChange={(event) => setInput({ ...input, experience: event.target.value as PortfolioInterviewInput["experience"] })}><option value="BEGINNER">처음 접함</option><option value="INTERMEDIATE">어느 경험</option><option value="EXPERIENCED">경험 많음</option></select></label>
            <label>투자 예정 기간(년)<input type="number" min="1" max="100" value={input.investment_horizon_years} onChange={(event) => setInput({ ...input, investment_horizon_years: Number(event.target.value) })} required /></label>
            <label>투자 가능 금액<input inputMode="decimal" min="1" value={input.investment_amount} onChange={(event) => setInput({ ...input, investment_amount: event.target.value })} required /></label>
            <label>통화<select value={input.currency} onChange={(event) => setInput({ ...input, currency: event.target.value as PortfolioInterviewInput["currency"] })}><option value="KRW">KRW · 원화</option><option value="USD">USD · 달러</option><option value="EUR">EUR · 유로</option></select></label>
            <label>감내 가능한 최대 손실률<input type="number" min="1" max="100" step="1" value={Number(input.max_drawdown_pct) * 100} onChange={(event) => setInput({ ...input, max_drawdown_pct: (Number(event.target.value) / 100).toFixed(4) })} required /><small>예: 10 = 최대 -10%</small></label>
        </div></details>
        <button className="btn btn-primary portfolio-submit" type="submit" disabled={busy || running}>
          {busy ? "분석 요청 중…" : running ? "분석 실행 중…" : "사용자 입력으로 분석 시작"}
        </button>
      </form>
      {connectionError && (
        <div className="form-error portfolio-error" role="alert">
          <span>⚠️ {explainPortfolioConnectionError(connectionError)}</span>
          <button
            type="button"
            className="text-button"
            onClick={() => {
              setSubmitError("");
              void refresh();
            }}
          >
            연결 재시도
          </button>
        </div>
      )}
        {runtime?.phase && <p className="runtime-phase"><b>현재 단계:</b> {runtime.phase}</p>}
      </div>
    </section>
  );
}
