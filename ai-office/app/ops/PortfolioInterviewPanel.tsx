"use client";

import { useMemo, useState } from "react";
import { useBffFeed } from "./bffClient";
import { startPortfolioRecommendation, type PortfolioInterviewInput } from "./portfolioClient";

type Recommendation = {
  portfolio_id: string;
  name: string;
  risk_band: string;
  fit_score: number;
  target_allocations: Record<string, string>;
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

export default function PortfolioInterviewPanel() {
  const { snapshot, connection, error, refresh } = useBffFeed();
  const [input, setInput] = useState<PortfolioInterviewInput>({
    user_id: "web-user",
    mindset: "BALANCED",
    experience: "BEGINNER",
    investment_horizon_years: 3,
    max_drawdown_pct: "0.10",
    liquidity_need: "MEDIUM",
  });
  const [busy, setBusy] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const runtime = snapshot?.operations?.runtime;
  const running = runtime?.status === "QUEUED" || runtime?.status === "RUNNING";
  const recommendations = useMemo(() => asRecommendations(runtime?.result && (runtime.result as { suitability?: unknown }).suitability), [runtime?.result]);

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
          <label>감내 가능한 최대 손실률<input type="number" min="1" max="100" step="1" value={Number(input.max_drawdown_pct) * 100} onChange={(event) => setInput({ ...input, max_drawdown_pct: (Number(event.target.value) / 100).toFixed(4) })} required /><small>예: 10 = 최대 -10%</small></label>
          <label>현금화 필요<select value={input.liquidity_need} onChange={(event) => setInput({ ...input, liquidity_need: event.target.value as PortfolioInterviewInput["liquidity_need"] })}><option value="HIGH">7일 안에 필요</option><option value="MEDIUM">30일 안에 필요</option><option value="LOW">당장 필요 없음</option></select></label>
          <button className="btn btn-primary" type="submit" disabled={busy || running}>{busy ? "요청 중…" : running ? "직원들이 분석 중…" : "LangGraph 분석 시작"}</button>
        </form>
        {(submitError || error) && <p className="form-error">⚠️ {submitError || error}</p>}
        {runtime?.phase && <p className="runtime-phase"><b>현재 단계</b> {runtime.phase}</p>}
        {runtime?.result && (
          <div className="portfolio-result" aria-label="포트폴리오 추천 결과">
            <div className="result-heading"><div><p className="eyebrow">BACKEND SUITABILITY RESULT</p><h3>{recommendations.length ? "추천 후보" : "조건에 맞는 후보가 없습니다"}</h3></div><span className="status-pill approval">수동 검토 필요</span></div>
            {recommendations.map((item) => <article className="portfolio-recommendation" key={item.portfolio_id}><div><strong>{item.name}</strong><code>{item.portfolio_id}</code></div><span className="score">{item.fit_score}점 · {item.risk_band}</span><p>목표 비중 {Object.entries(item.target_allocations ?? {}).map(([asset, weight]) => `${asset} ${percent(weight)}`).join(" · ")}</p><p>{item.reasons.join(" · ")}</p><small>근거 {item.evidence_refs.join(", ")}</small></article>)}
            <p className="dash-note">이 결과는 주문·승인·원장 변경이 아닌 비바인딩 자문입니다. 실제 투자 전 추가 검토가 필요합니다.</p>
          </div>
        )}
      </div>
    </section>
  );
}
