"use client";

import { useMemo, useState } from "react";
import { useBffFeed } from "./bffClient";
import { decidePortfolioRecommendation, startPortfolioRecommendation, type PortfolioInterviewInput } from "./portfolioClient";

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

function asRecommendations(value: unknown): Recommendation[] {
  if (typeof value !== "object" || value === null || !Array.isArray((value as { recommendations?: unknown }).recommendations)) return [];
  return (value as { recommendations: unknown[] }).recommendations.filter(
    (item): item is Recommendation => typeof item === "object" && item !== null && typeof (item as Recommendation).portfolio_id === "string" && typeof (item as Recommendation).name === "string",
  );
}

function percent(value: string): string {
  const number = Number(value);
  return Number.isFinite(number) ? `${(number * 100).toFixed(0)}%` : value;
}

function amount(value: string, currency: string): string {
  const number = Number(value);
  return Number.isFinite(number) ? new Intl.NumberFormat("ko-KR", { style: "currency", currency, maximumFractionDigits: 2 }).format(number) : value;
}

const ASSET_LABEL: Record<string, string> = {
  KOREA_EQUITY: "국내 주식",
  GLOBAL_EQUITY: "글로벌 주식",
  SHORT_TERM_BOND: "단기채권",
  LEVERAGED_ETF: "레버리지 ETF",
  SHORT_EXPOSURE: "공매도 익스포저",
  DERIVATIVES_HEDGE: "파생상품 헤지",
};

export default function PortfolioInterviewPanel() {
  const { snapshot, connection, error, refresh } = useBffFeed();
  const [input, setInput] = useState<PortfolioInterviewInput>({
    user_id: "web-user",
    mindset: "BALANCED",
    experience: "BEGINNER",
    investment_horizon_years: 3,
    max_drawdown_pct: "0.10",
    liquidity_need: "MEDIUM",
    investment_amount: "1000000",
    currency: "KRW",
  });
  const [busy, setBusy] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const [approvalBusy, setApprovalBusy] = useState(false);
  const runtime = snapshot?.operations?.runtime;
  const running = runtime?.status === "QUEUED" || runtime?.status === "RUNNING";
  const recommendations = useMemo(() => asRecommendations(runtime?.result && (runtime.result as { suitability?: unknown }).suitability), [runtime?.result]);
  const approval = runtime?.approval;
  const resultCurrency = typeof (runtime?.result as { suitability?: { currency?: unknown } } | null)?.suitability?.currency === "string"
    ? String((runtime?.result as { suitability: { currency: string } }).suitability.currency)
    : input.currency;

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

  async function submit(event: React.FormEvent<HTMLFormElement>) {
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
        <span className="window-controls" aria-hidden="true">— ▢ ✕</span>
      </div>
      <div className="win-body">
        <div className="section-heading">
          <div>
            <p className="eyebrow">USER PROFILE → LANGGRAPH → ADVISORY PORTFOLIO</p>
            <h2 id="portfolio-interview-title">사용자에게 맞는 포트폴리오 받기</h2>
          </div>
          <span className={`status-pill ${running ? "working" : runtime?.result ? "done" : "waiting"}`}>
            {running ? "LANGGRAPH RUNNING" : runtime?.result ? runtime.status : connection.toUpperCase()}
          </span>
        </div>
        <p className="dash-note">처음 주식을 접해도 괜찮습니다. 입력한 투자 성향·경험·기간·손실 감내도·현금화 필요를 기준으로 백엔드 suitability 엔진이 후보를 선별합니다.</p>
        <form className="portfolio-form" onSubmit={submit}>
          <label>사용자 식별자<input value={input.user_id} onChange={(event) => setInput({ ...input, user_id: event.target.value })} required /></label>
          <label>투자 성향<select value={input.mindset} onChange={(event) => setInput({ ...input, mindset: event.target.value as PortfolioInterviewInput["mindset"] })}><option value="SAFETY_FIRST">안전 우선</option><option value="BALANCED">균형형</option><option value="RISK_SEEKING">성장·위험 감수</option></select></label>
          <label>투자 경험<select value={input.experience} onChange={(event) => setInput({ ...input, experience: event.target.value as PortfolioInterviewInput["experience"] })}><option value="BEGINNER">처음 접함</option><option value="INTERMEDIATE">어느 정도 경험</option><option value="EXPERIENCED">경험 많음</option></select></label>
          <label>투자 예정 기간(년)<input type="number" min="1" max="100" value={input.investment_horizon_years} onChange={(event) => setInput({ ...input, investment_horizon_years: Number(event.target.value) })} required /></label>
          <label>투자 가능 금액<input inputMode="decimal" min="1" value={input.investment_amount} onChange={(event) => setInput({ ...input, investment_amount: event.target.value })} required /></label>
          <label>통화<select value={input.currency} onChange={(event) => setInput({ ...input, currency: event.target.value as PortfolioInterviewInput["currency"] })}><option value="KRW">KRW · 원화</option><option value="USD">USD · 달러</option><option value="EUR">EUR · 유로</option></select></label>
          <label>감내 가능한 최대 손실률<input type="number" min="1" max="100" step="1" value={Number(input.max_drawdown_pct) * 100} onChange={(event) => setInput({ ...input, max_drawdown_pct: (Number(event.target.value) / 100).toFixed(4) })} required /><small>예: 10 = 최대 -10%</small></label>
          <label>현금화 필요<select value={input.liquidity_need} onChange={(event) => setInput({ ...input, liquidity_need: event.target.value as PortfolioInterviewInput["liquidity_need"] })}><option value="HIGH">7일 안에 필요</option><option value="MEDIUM">30일 안에 필요</option><option value="LOW">당장 필요 없음</option></select></label>
          <button className="btn btn-primary" type="submit" disabled={busy || running}>{busy ? "요청 중…" : running ? "직원들이 분석 중…" : "LangGraph 분석 시작"}</button>
        </form>
        {(submitError || error) && <p className="form-error">⚠️ {submitError || error}</p>}
        {runtime?.phase && <p className="runtime-phase"><b>현재 단계</b> {runtime.phase}</p>}
        {runtime?.result && (
          <div className="portfolio-result" aria-label="포트폴리오 추천 결과">
            <div className="result-heading"><div><p className="eyebrow">BACKEND SUITABILITY RESULT</p><h3>{recommendations.length ? "추천 후보" : "조건에 맞는 후보가 없습니다"}</h3></div><span className={`status-pill ${approval?.status === "APPROVE" ? "done" : approval?.status === "REJECT" ? "blocked" : "approval"}`}>{approval?.status === "APPROVE" ? "사용자 승인 완료" : approval?.status === "REJECT" ? "사용자 거절" : "수동 검토 필요"}</span></div>
            {recommendations.map((item) => { const highRisk = Object.keys(item.target_allocations ?? {}).some((asset) => ["LEVERAGED_ETF", "SHORT_EXPOSURE", "DERIVATIVES_HEDGE"].includes(asset)); return <article className="portfolio-recommendation" key={item.portfolio_id}><div><strong>{item.name}</strong><code>{item.portfolio_id}</code></div><span className="score">{item.fit_score}점 · {item.risk_band}</span><p>목표 비중 {Object.entries(item.target_allocations ?? {}).map(([asset, weight]) => `${ASSET_LABEL[asset] ?? asset} ${percent(weight)}`).join(" · ")}</p><p>목표 금액 {Object.entries(item.target_amounts ?? {}).map(([asset, value]) => `${ASSET_LABEL[asset] ?? asset} ${amount(value, resultCurrency)}`).join(" · ")}</p>{highRisk && <p className="risk-warning">고위험 요소가 포함된 후보입니다. 레버리지·공매도·파생상품 조건을 별도 검토해야 합니다.</p>}<p>{item.reasons.join(" · ")}</p><small>근거 {item.evidence_refs.join(", ")}</small></article>; })}
            {recommendations.length > 0 && approval?.status === "PENDING" && <div className="approval-actions"><p className="dash-note">이 추천 구성을 사용자 승인 단계로 넘길까요? 승인해도 주문은 제출되지 않습니다.</p><div><button className="btn btn-primary" type="button" disabled={approvalBusy} onClick={() => void decide("APPROVE")}>{approvalBusy ? "처리 중…" : "이 포트폴리오 승인"}</button><button className="btn btn-ghost" type="button" disabled={approvalBusy} onClick={() => void decide("REJECT")}>추천 거절</button></div></div>}
            <p className="dash-note">이 결과는 주문·승인·원장 변경이 아닌 비바인딩 자문입니다. 실제 투자 전 추가 검토가 필요합니다.</p>
          </div>
        )}
      </div>
    </section>
  );
}
