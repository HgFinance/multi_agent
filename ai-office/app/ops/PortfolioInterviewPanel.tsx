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
import {
  advanceMandateCase,
  decideMandateApproval,
  fetchMandateApprovals,
  fetchCurrentMandate,
  fetchMandateTimeline,
  submitMandateChange,
  type MandateApproval,
  type MandateChange,
} from "./governanceClient";
import { readPitReadiness, readablePitReason, readableRuntimeStatus } from "./statusLabels";

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

type AssetKey = "stock" | "etf" | "leveraged_etf" | "futures" | "options" | "derivatives" | "crypto";
type ApprovalMode = "AUTO" | "USER_APPROVAL";

type MandateDraft = PortfolioInterviewInput & {
  mandate_id: string;
  fund_id: string;
  objective: string;
  max_instrument_weight_pct: number;
  max_sector_weight_pct: number;
  max_gross_exposure_pct: number;
  max_concurrent_positions: number;
  max_daily_loss_pct: number;
  allowed_markets: string;
  trading_start: string;
  trading_end: string;
  approval_mode: ApprovalMode;
  allowed_assets: Record<AssetKey, boolean>;
};

const MANDATE_STORAGE_KEY = "hgfinance.mandate-config.v1";
const ASSET_OPTIONS: Array<{ key: AssetKey; label: string; icon: string }> = [
  { key: "stock", label: "단일 주식", icon: "📈" },
  { key: "etf", label: "ETF", icon: "◒" },
  { key: "leveraged_etf", label: "레버리지", icon: "↗" },
  { key: "futures", label: "선물", icon: "⌁" },
  { key: "options", label: "옵션", icon: "◎" },
  { key: "derivatives", label: "파생상품", icon: "✣" },
  { key: "crypto", label: "암호화폐", icon: "₿" },
];
const MINDSET_OPTIONS = [
  { value: "SAFETY_FIRST", label: "보수적", icon: "🛡️", copy: "원금 보존을 우선하고 변동성을 최소화합니다." },
  { value: "BALANCED", label: "중립적", icon: "⚖️", copy: "성장과 위험의 균형을 추구합니다." },
  { value: "RISK_SEEKING", label: "공격적", icon: "🚀", copy: "높은 성장 잠재력과 변동성을 감수합니다." },
] as const;
const DEFAULT_MANDATE_DRAFT: MandateDraft = {
  user_id: "web-user",
  mandate_id: process.env.NEXT_PUBLIC_MANDATE_ID?.trim() || "web-mandate",
  fund_id: process.env.NEXT_PUBLIC_FUND_ID?.trim() || "web-fund",
  objective: "장기적인 자산 가치 보존과 안정적인 수익 창출을 목표로 하며, 하락 리스크는 최소화합니다.",
  mindset: "SAFETY_FIRST",
  experience: "BEGINNER",
  investment_horizon_years: 3,
  max_drawdown_pct: "0.10",
  investment_amount: "100000000",
  currency: "KRW",
  universe_id: "KOREA_EQUITY_WATCHLIST",
  category: "PORTFOLIO_RECOMMENDATION",
  include_stock: true,
  include_derivatives: false,
  query: "",
  max_instrument_weight_pct: 30,
  max_sector_weight_pct: 50,
  max_gross_exposure_pct: 200,
  max_concurrent_positions: 10,
  max_daily_loss_pct: 2,
  allowed_markets: "KR",
  trading_start: "09:00",
  trading_end: "15:30",
  approval_mode: "USER_APPROVAL",
  allowed_assets: {
    stock: true,
    etf: true,
    leveraged_etf: true,
    futures: false,
    options: false,
    derivatives: false,
    crypto: false,
  },
};

function readSavedMandate(): MandateDraft | null {
  try {
    const raw = window.localStorage.getItem(MANDATE_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { draft?: MandateDraft };
    return parsed.draft ? { ...DEFAULT_MANDATE_DRAFT, ...parsed.draft, allowed_assets: { ...DEFAULT_MANDATE_DRAFT.allowed_assets, ...parsed.draft.allowed_assets } } : null;
  } catch {
    return null;
  }
}

function saveMandate(draft: MandateDraft, policy?: Record<string, unknown>) {
  let previous: { policy?: Record<string, unknown> } = {};
  try {
    previous = JSON.parse(window.localStorage.getItem(MANDATE_STORAGE_KEY) || "{}") as typeof previous;
  } catch {
    previous = {};
  }
  window.localStorage.setItem(MANDATE_STORAGE_KEY, JSON.stringify({
    version: 1,
    saved_at: new Date().toISOString(),
    draft,
    policy: policy ?? previous.policy,
  }));
}

function readSavedMandatePolicy(): Record<string, unknown> | undefined {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(MANDATE_STORAGE_KEY) || "{}") as { policy?: unknown };
    return parsed.policy && typeof parsed.policy === "object" ? parsed.policy as Record<string, unknown> : undefined;
  } catch {
    return undefined;
  }
}

function traceId() {
  return typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `web-${Date.now()}`;
}

function buildMandatePolicy(input: MandateDraft): Record<string, unknown> {
  return {
    // MandatePolicy의 allowed_assets는 instrument ID 계약이다. 자산군 토글은
    // 현재 추천 범위에만 반영하고, 미확정 asset-class taxonomy를 정책으로 위조하지 않는다.
    allowed_assets: [],
    forbidden_assets: [],
    risk_bounds: {
      base_capital: input.investment_amount,
      currency: input.currency,
      max_instrument_weight: (input.max_instrument_weight_pct / 100).toFixed(4),
      max_sector_weight: (input.max_sector_weight_pct / 100).toFixed(4),
      max_gross_exposure: (input.max_gross_exposure_pct / 100).toFixed(4),
      max_concurrent_positions: input.max_concurrent_positions,
      max_daily_loss: (input.max_daily_loss_pct / 100).toFixed(4),
    },
    universe_policy: {
      allowed_markets: input.allowed_markets.split(",").map((market) => market.trim()).filter(Boolean),
      trading_start: input.trading_start,
      trading_end: input.trading_end,
    },
    approval_rules: {
      paper_order_mode: input.approval_mode,
      risk_expansion_requires_user_approval: true,
    },
  };
}

type MandateWorkflowState = {
  change: MandateChange;
  approvals: MandateApproval[];
  versionObjectId: string | null;
};

const MANDATE_STAGE_LABEL: Record<string, string> = {
  AWAITING_REVIEW: "Risk·QA 검토 대기",
  AWAITING_USER_APPROVAL: "대표님 최종 승인 대기",
  ACTIVATED: "Mandate 활성화 완료",
  FAST_APPLIED: "안전 조정 즉시 적용",
  REVIEW_REJECTED: "검토 거절 · 안전 보류",
  USER_REJECTED: "대표님 거절 · 안전 보류",
};

const APPROVAL_ROLE_LABEL: Record<string, string> = {
  RISK: "Risk",
  QA: "QA",
  USER: "대표",
};

const TERMINAL_MANDATE_STAGES = new Set(["ACTIVATED", "FAST_APPLIED", "REVIEW_REJECTED", "USER_REJECTED", "CANCELLED"]);

function approvalsNeedAdvance(stage: string, approvals: MandateApproval[]): boolean {
  if (stage === "AWAITING_REVIEW") {
    const reviews = approvals.filter((approval) => approval.required_role === "RISK" || approval.required_role === "QA");
    return reviews.some((approval) => ["REJECTED", "EXPIRED", "REVOKED"].includes(approval.decision))
      || reviews.length >= 2 && reviews.every((approval) => approval.decision === "APPROVED");
  }
  return stage === "AWAITING_USER_APPROVAL" && approvals.some((approval) => approval.required_role === "USER" && approval.decision !== "PENDING");
}

async function refreshMandateWorkflow(state: MandateWorkflowState): Promise<MandateWorkflowState> {
  if (!state.change.case_id) return state;
  const timeline = await fetchMandateTimeline(state.change.case_id);
  const versionObjectId = timeline.events
    .map((event) => event.payload.mandate_version_id)
    .find((value): value is string => typeof value === "string") ?? state.versionObjectId;
  const approvals = versionObjectId ? (await fetchMandateApprovals(versionObjectId)).approvals : state.approvals;
  const change = approvalsNeedAdvance(state.change.stage, approvals)
    ? await advanceMandateCase(state.change.case_id)
    : state.change;
  return { change, approvals, versionObjectId };
}

function MandateWorkflowStatus({
  workflow,
  busy,
  error,
  onRefresh,
  onUserDecision,
  onStartRecommendation,
}: {
  workflow: MandateWorkflowState;
  busy: boolean;
  error: string;
  onRefresh: () => void;
  onUserDecision: (decision: "APPROVED" | "REJECTED") => void;
  onStartRecommendation: () => void;
}) {
  const userApproval = workflow.approvals.find((approval) => approval.required_role === "USER" && approval.decision === "PENDING");
  return (
    <section className="mandate-workflow" aria-live="polite">
      <div className="mandate-workflow-heading">
        <div><span className="tiny-label">GOVERNANCE HITL · VERSION {workflow.change.version}</span><strong>{MANDATE_STAGE_LABEL[workflow.change.stage] ?? workflow.change.stage}</strong></div>
        {workflow.change.case_id ? <code>{workflow.change.case_id.slice(0, 12)}…</code> : null}
      </div>
      <p>{workflow.change.detail}</p>
      {workflow.approvals.length > 0 && (
        <div className="mandate-approval-statuses">
          {workflow.approvals.map((approval) => <span key={approval.approval_id} className={`mandate-approval-status ${approval.decision.toLowerCase()}`}><b>{APPROVAL_ROLE_LABEL[approval.required_role] ?? approval.required_role}</b> {readableRuntimeStatus(approval.decision)}</span>)}
        </div>
      )}
      {userApproval ? <div className="mandate-workflow-actions"><button type="button" className="btn btn-primary" onClick={() => onUserDecision("APPROVED")} disabled={busy}>대표 승인</button><button type="button" className="btn btn-ghost" onClick={() => onUserDecision("REJECTED")} disabled={busy}>거절</button></div> : null}
      {workflow.change.stage === "ACTIVATED" ? <button type="button" className="btn btn-primary mandate-analysis-button" onClick={onStartRecommendation} disabled={busy}>활성화된 Mandate로 분석 시작</button> : null}
      {(workflow.change.stage === "AWAITING_REVIEW" || workflow.change.stage === "AWAITING_USER_APPROVAL") ? <div className="mandate-advisory-action"><button type="button" className="btn btn-ghost mandate-analysis-button" onClick={onStartRecommendation} disabled={busy}>승인 대기 중에도 자문 분석 시작</button><small>Governance 승인과 별개인 비구속 분석이며 주문·원장 변경은 없습니다.</small></div> : null}
      {workflow.change.case_id && workflow.change.stage !== "ACTIVATED" && !userApproval ? <button type="button" className="text-button" onClick={onRefresh} disabled={busy}>승인 상태 새로고침</button> : null}
      {error ? <small className="form-error">⚠️ {error}</small> : null}
    </section>
  );
}

export default function PortfolioInterviewPanel({
  onConfigured,
  onAnalyzed,
}: {
  onConfigured?: () => void;
  onAnalyzed?: () => void;
}) {
  const { snapshot, connection, error, refresh } = useBffFeed();
  const [input, setInput] = useState<MandateDraft>(DEFAULT_MANDATE_DRAFT);
  const [universes, setUniverses] = useState<PortfolioUniverseOption[]>([]);
  const [busy, setBusy] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const [configured, setConfigured] = useState(false);
  const [editing, setEditing] = useState(false);
  const [workflow, setWorkflow] = useState<MandateWorkflowState | null>(null);
  const [workflowError, setWorkflowError] = useState("");
  const [backendMandate, setBackendMandate] = useState<{ current_version: number; status: string } | null>(null);
  const runtime = snapshot?.operations?.runtime;
  const pitReadiness = readPitReadiness(runtime);
  const running = runtime?.status === "QUEUED" || runtime?.status === "RUNNING";
  const connectionError = submitError || (!runtime?.run_id && error) || "";

  useEffect(() => {
    const caseId = workflow?.change.case_id;
    if (!caseId || TERMINAL_MANDATE_STAGES.has(workflow.change.stage)) return;
    let active = true;
    let inFlight = false;
    const sync = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const next = await refreshMandateWorkflow(workflow);
        if (active) {
          setWorkflow(next);
          setWorkflowError("");
        }
      } catch (cause) {
        if (active) setWorkflowError(cause instanceof Error ? cause.message : String(cause));
      } finally {
        inFlight = false;
      }
    };
    void sync();
    const timer = window.setInterval(() => void sync(), 1500);
    return () => { active = false; window.clearInterval(timer); };
    // The interval intentionally follows case/stage/version changes, not every approval refresh.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflow?.change.case_id, workflow?.change.stage, workflow?.versionObjectId]);

  useEffect(() => {
    const saved = readSavedMandate();
    const hydrateTimer = saved ? window.setTimeout(() => {
      setInput(saved);
      setConfigured(true);
    }, 0) : 0;
    let active = true;
    void fetchCurrentMandate(saved?.mandate_id ?? DEFAULT_MANDATE_DRAFT.mandate_id).then((current) => {
      if (active) setBackendMandate({ current_version: current.current_version, status: current.status });
    }).catch(() => undefined);
    void fetchPortfolioUniverses().then((payload) => {
      if (!active) return;
        const domestic = payload.universes.filter((item) => item.universe_id === "KOREA_EQUITY_WATCHLIST");
        setUniverses(domestic);
    }).catch(() => undefined);
    return () => { active = false; if (hydrateTimer) window.clearTimeout(hydrateTimer); };
  }, []);

  function persist() {
    saveMandate(input);
    setConfigured(true);
    setEditing(false);
    setSubmitError("");
    onConfigured?.();
  }

  function recommendationInput(): PortfolioInterviewInput {
    return {
      user_id: input.user_id,
      mindset: input.mindset,
      experience: input.experience,
      investment_horizon_years: input.investment_horizon_years,
      max_drawdown_pct: input.max_drawdown_pct,
      investment_amount: input.investment_amount,
      currency: input.currency,
      universe_id: input.universe_id,
      category: input.category,
      include_stock: input.allowed_assets.stock,
      include_derivatives: input.allowed_assets.futures || input.allowed_assets.options || input.allowed_assets.derivatives,
      query: input.objective,
    };
  }

  async function startRecommendation() {
    await startPortfolioRecommendation(recommendationInput());
    await refresh();
    setConfigured(true);
    setEditing(false);
    onConfigured?.();
    onAnalyzed?.();
  }

  async function submit(event?: FormEvent<HTMLFormElement>) {
    event?.preventDefault();
    setBusy(true);
    setSubmitError("");
    try {
      saveMandate(input);
      const policy = buildMandatePolicy(input);
      const now = new Date();
      const change = await submitMandateChange(input.mandate_id, {
        fund_id: input.fund_id,
        policy,
        objective_text: input.objective,
        objective: {
          category: input.category,
          investment_horizon_years: input.investment_horizon_years,
          max_drawdown_pct: input.max_drawdown_pct,
          asset_preferences: input.allowed_assets,
        },
        effective_from: now.toISOString(),
        created_by: input.user_id,
        trace_id: traceId(),
        now: now.toISOString(),
        previous_policy: readSavedMandatePolicy() ?? null,
        priority: 50,
        review_expires_at: new Date(now.getTime() + 86_400_000).toISOString(),
        user_approval_ttl_seconds: 86_400,
        version_created_by: process.env.NEXT_PUBLIC_GOVERNANCE_ACTOR_USER_ID?.trim() || undefined,
        fund_base_currency: input.currency,
      });
      saveMandate(input, policy);
      setWorkflow({ change, approvals: [], versionObjectId: null });
      setBackendMandate({ current_version: change.version, status: change.stage });
      setConfigured(true);
      setEditing(false);
      onConfigured?.();
      if (change.stage === "FAST_APPLIED") await startRecommendation();
    } catch (cause) {
      setSubmitError(cause instanceof Error ? cause.message : String(cause));
      setConfigured(true);
      setEditing(false);
    } finally {
      setBusy(false);
    }
  }

  async function decideUser(decision: "APPROVED" | "REJECTED") {
    const approval = workflow?.approvals.find((item) => item.required_role === "USER" && item.decision === "PENDING");
    if (!approval) return;
    setBusy(true);
    setWorkflowError("");
    try {
      await decideMandateApproval(approval.approval_id, decision);
      if (workflow) setWorkflow(await refreshMandateWorkflow(workflow));
    } catch (cause) {
      setWorkflowError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  async function startAnalysis() {
    setBusy(true);
    setSubmitError("");
    try {
      await startRecommendation();
    } catch (cause) {
      setSubmitError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  const toggleAsset = (key: AssetKey) => {
    setInput((current) => ({
      ...current,
      allowed_assets: { ...current.allowed_assets, [key]: !current.allowed_assets[key] },
    }));
  };

  return (
    <section className="win portfolio-interview" id="portfolio-interview" aria-labelledby="portfolio-interview-title">
      <div className="win-bar"><span>🗂 mandate.configuration · F01</span><span className="window-controls" aria-hidden="true">— ✕</span></div>
      <div className="win-body">
        <div className="section-heading portfolio-heading">
          <div><p className="eyebrow">USER INPUT → CEO ROUTER → RISK / QA GATE</p><h2 id="portfolio-interview-title">Mandate Configuration</h2></div>
          <span className={`status-pill ${configured ? "status-ready" : running ? "status-running" : ""}`}>{configured ? "설정 완료" : busy ? "요청 중" : running ? "실행 중" : readableRuntimeStatus(runtime?.status ?? connection)}</span>
        </div>
        <p className="dash-note portfolio-intro">기본값을 확인해 한 번만 저장하세요. 세부 조건은 옆의 AI Assistant가 자연어로 이어서 물어봅니다.</p>
        <p className="dash-note portfolio-source-note">{backendMandate ? `Governance 현재 상태(버전만 조회) · v${backendMandate.current_version} · ${readableRuntimeStatus(backendMandate.status)}` : "현재 설정은 브라우저 초안입니다. Mandate 제출 후 Governance 버전이 표시됩니다."}</p>
        {workflow && <MandateWorkflowStatus workflow={workflow} busy={busy} error={workflowError} onRefresh={() => { if (workflow) void refreshMandateWorkflow(workflow).then(setWorkflow).catch((cause) => setWorkflowError(cause instanceof Error ? cause.message : String(cause))); }} onUserDecision={(decision) => void decideUser(decision)} onStartRecommendation={() => void startAnalysis()} />}
        {configured && !editing ? (
          <div className="mandate-saved" aria-live="polite">
            <div><span className="mini-badge mint">ONE-TIME SETUP</span><span className={`mini-badge ${backendMandate ? "mint" : "yellow"}`}>{backendMandate ? `GOVERNANCE v${backendMandate.current_version}` : "LOCAL DRAFT"}</span><strong>{input.objective}</strong></div>
            <dl><div><dt>성향</dt><dd>{MINDSET_OPTIONS.find((item) => item.value === input.mindset)?.label}</dd></div><div><dt>기준 자본</dt><dd>{Number(input.investment_amount).toLocaleString("ko-KR")} {input.currency}</dd></div><div><dt>단일 종목</dt><dd>{input.max_instrument_weight_pct}%</dd></div><div><dt>총 익스포저</dt><dd>{input.max_gross_exposure_pct}%</dd></div><div><dt>승인</dt><dd>{input.approval_mode === "AUTO" ? "자동" : "수동 승인"}</dd></div></dl>
            <div className="mandate-saved-actions"><button type="button" className="btn btn-ghost" onClick={() => setEditing(true)}>설정 수정</button><button type="button" className="btn btn-primary" onClick={() => void startAnalysis()} disabled={busy || running}>{busy ? "분석 요청 중…" : running ? "분석 실행 중…" : "이 설정으로 분석 시작"}</button></div>
          </div>
        ) : (
          <form id="portfolio-interview-form" className="mandate-form" onSubmit={submit}>
            <section className="mandate-form-section"><h3><span>1.</span> 목표 및 위험 성향</h3><div className="mandate-objective-grid"><label className="mandate-objective">투자 목표 <small>(자연어 입력)</small><textarea value={input.objective} onChange={(event) => setInput({ ...input, objective: event.target.value })} maxLength={2000} required /><small>구체적인 종목이나 기간은 AI Assistant가 다음 질문으로 확인합니다.</small></label><fieldset className="mandate-risk-options"><legend>위험 성향 <small>(하나 선택)</small></legend>{MINDSET_OPTIONS.map((item) => <label className={`mandate-risk-card ${input.mindset === item.value ? "selected" : ""}`} key={item.value}><input type="radio" name="mindset" value={item.value} checked={input.mindset === item.value} onChange={() => setInput({ ...input, mindset: item.value })} /><span className="mandate-risk-icon">{item.icon}</span><b>{item.label}</b><small>{item.copy}</small></label>)}</fieldset></div></section>
            <section className="mandate-form-section"><h3><span>2.</span> 자본 및 통화</h3><div className="mandate-inline-fields"><label>기준 자본 <small>(risk_bounds.base_capital)</small><input inputMode="decimal" min="1" value={input.investment_amount} onChange={(event) => setInput({ ...input, investment_amount: event.target.value })} required /></label><label>통화<select value={input.currency} onChange={(event) => setInput({ ...input, currency: event.target.value as PortfolioInterviewInput["currency"] })}><option value="KRW">KRW · 대한민국 원</option><option value="USD">USD · 달러</option><option value="EUR">EUR · 유로</option></select></label></div></section>
            <section className="mandate-form-section"><h3><span>3.</span> 비중 한도 및 익스포저</h3><div className="mandate-range-grid"><label>최대 단일 종목 비중 <output>{input.max_instrument_weight_pct}%</output><input type="range" min="5" max="50" step="5" value={input.max_instrument_weight_pct} onChange={(event) => setInput({ ...input, max_instrument_weight_pct: Number(event.target.value) })} /><small>5%　　25%　　50%</small></label><label>최대 총 익스포저 <output>{input.max_gross_exposure_pct}%</output><input type="range" min="100" max="500" step="25" value={input.max_gross_exposure_pct} onChange={(event) => setInput({ ...input, max_gross_exposure_pct: Number(event.target.value) })} /><small>100%　　300%　　500%</small></label></div></section>
            <section className="mandate-form-section"><h3><span>4.</span> 자산 정책 <small>(허용 vs 금지)</small></h3><div className="mandate-asset-grid">{ASSET_OPTIONS.map((item) => <button type="button" key={item.key} className={`mandate-asset-card ${input.allowed_assets[item.key] ? "allowed" : "blocked"}`} aria-pressed={input.allowed_assets[item.key]} onClick={() => toggleAsset(item.key)}><span>{item.icon}</span><b>{item.label}</b><small>{input.allowed_assets[item.key] ? "✓ 허용됨" : "× 금지됨"}</small></button>)}</div></section>
            <section className="mandate-form-section"><h3><span>5.</span> 주문 승인 방식 <small>(approval_rules.paper_order_mode)</small></h3><div className="mandate-approval-grid">{([{ value: "AUTO", icon: "⚡", label: "자동 주문", copy: "정책에 따라 주문이 자동으로 실행됩니다." }, { value: "USER_APPROVAL", icon: "✋", label: "수동 승인 필요", copy: "모든 주문은 실행 전에 수동 승인이 필요합니다." }] as const).map((item) => <label className={`mandate-approval-card ${input.approval_mode === item.value ? "selected" : ""}`} key={item.value}><input type="radio" name="approval_mode" value={item.value} checked={input.approval_mode === item.value} onChange={() => setInput({ ...input, approval_mode: item.value })} /><span>{item.icon}</span><div><b>{item.label}</b><small>{item.copy}</small></div></label>)}</div></section>
            <details className="portfolio-advanced mandate-advanced"><summary>고급 설정 · 에이전트가 대화로 확인할 세부 필드</summary><div className="portfolio-advanced-grid"><label>사용자 식별자<input value={input.user_id} onChange={(event) => setInput({ ...input, user_id: event.target.value })} required /></label><label>Mandate ID<input value={input.mandate_id} onChange={(event) => setInput({ ...input, mandate_id: event.target.value })} required /></label><label>Fund ID<input value={input.fund_id} onChange={(event) => setInput({ ...input, fund_id: event.target.value })} required /></label><label>분석 카테고리<select value={input.category} onChange={(event) => setInput({ ...input, category: event.target.value })}>{CATEGORY_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label>국내 주식 유니버스<select value={input.universe_id} onChange={(event) => setInput({ ...input, universe_id: event.target.value })} required>{universes.length > 0 ? universes.map((universe) => <option key={universe.universe_id} value={universe.universe_id}>{universe.name} · {universe.instrument_count}개</option>) : <option value="KOREA_EQUITY_WATCHLIST">국내 주식 Watchlist</option>}</select></label><label>투자 경험<select value={input.experience} onChange={(event) => setInput({ ...input, experience: event.target.value as PortfolioInterviewInput["experience"] })}><option value="BEGINNER">처음 접함</option><option value="INTERMEDIATE">어느 경험</option><option value="EXPERIENCED">경험 많음</option></select></label><label>투자 예정 기간(년)<input type="number" min="1" max="100" value={input.investment_horizon_years} onChange={(event) => setInput({ ...input, investment_horizon_years: Number(event.target.value) })} required /></label><label>누적 손실 감내율<input type="number" min="1" max="100" step="1" value={Number(input.max_drawdown_pct) * 100} onChange={(event) => setInput({ ...input, max_drawdown_pct: (Number(event.target.value) / 100).toFixed(4) })} required /><small>예: 10 = 최대 -10%</small></label><label>최대 섹터 비중(%)<input type="number" min="5" max="100" value={input.max_sector_weight_pct} onChange={(event) => setInput({ ...input, max_sector_weight_pct: Number(event.target.value) })} /></label><label>동시 보유 종목 수<input type="number" min="1" max="100" value={input.max_concurrent_positions} onChange={(event) => setInput({ ...input, max_concurrent_positions: Number(event.target.value) })} /></label><label>일일 최대 손실(%)<input type="number" min="0.1" max="20" step="0.1" value={input.max_daily_loss_pct} onChange={(event) => setInput({ ...input, max_daily_loss_pct: Number(event.target.value) })} /></label><label>허용 시장<input value={input.allowed_markets} onChange={(event) => setInput({ ...input, allowed_markets: event.target.value })} /></label><label>거래 시작<input type="time" value={input.trading_start} onChange={(event) => setInput({ ...input, trading_start: event.target.value })} /></label><label>거래 종료<input type="time" value={input.trading_end} onChange={(event) => setInput({ ...input, trading_end: event.target.value })} /></label></div></details>
            <div className="mandate-submit-row"><button type="button" className="btn btn-ghost" onClick={persist}>설정만 저장</button><button className="btn btn-primary portfolio-submit" type="submit" disabled={busy || running}>{busy ? "Mandate 제출 중…" : running ? "분석 실행 중…" : "Mandate 제출하고 검토 시작"}</button></div>
          </form>
        )}
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
        {pitReadiness && pitReadiness.quality_status !== "PASS" && (
          <div className="pit-diagnostic" role="status">
            <strong>분석 입력 진단 · {pitReadiness.quality_status === "PASS" ? "준비됨" : "보류"}</strong>
            <span>
              후보 {pitReadiness.candidate_count ?? 0}개 · 연구 문서 {pitReadiness.research_document_count ?? 0}개 · 시장 스냅샷 {pitReadiness.market_snapshot_count ?? 0}개 · 국내 종목 {pitReadiness.domestic_instrument_count ?? 0}개
            </span>
            <small>확인 필요: {(pitReadiness.reasons ?? ["PIT_DATA_NOT_READY"]).slice(0, 3).map(readablePitReason).join(" · ")}</small>
            <small>데이터를 확인한 뒤 저장된 Mandate로 안전하게 다시 요청할 수 있습니다.</small>
            <div className="pit-actions">
              <button type="button" className="btn btn-ghost" onClick={() => void refresh()}>데이터 새로고침</button>
              {!editing ? <button type="button" className="btn btn-ghost" onClick={() => { setEditing(true); window.requestAnimationFrame(() => document.getElementById("portfolio-interview-form")?.scrollIntoView({ behavior: "smooth", block: "start" })); }}>Mandate 설정 확인</button> : null}
              <button type="button" className="btn btn-primary" onClick={() => void startAnalysis()} disabled={busy || running}>{busy ? "분석 요청 중…" : running ? "분석 실행 중…" : "분석 다시 시작"}</button>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
