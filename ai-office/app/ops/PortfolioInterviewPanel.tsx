"use client";

import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useBffFeed } from "./bffClient";
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
};

type RuntimeResult = {
  suitability?: unknown;
  instrument_recommendations?: unknown;
  unresolved_asset_classes?: string[];
  instrument_recommendations_status?: string;
  universe?: { name?: string; status?: string; source?: string } | null;
  forecast_notice?: string;
};

function asRecommendations(value: unknown): Recommendation[] {
  if (typeof value !== "object" || value === null || !Array.isArray((value as { recommendations?: unknown }).recommendations)) {
    return [];
  }
  return (value as { recommendations: unknown[] }).recommendations.filter(
    (item): item is Recommendation =>
      typeof item === "object" && item !== null &&
      typeof (item as Recommendation).portfolio_id === "string" &&
      typeof (item as Recommendation).name === "string",
  );
}

function asInstrumentRecommendations(value: unknown): InstrumentRecommendation[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is InstrumentRecommendation =>
      typeof item === "object" && item !== null &&
      typeof (item as InstrumentRecommendation).portfolio_id === "string" &&
      typeof (item as InstrumentRecommendation).symbol === "string",
  );
}

function percent(value: string | number | null): string {
  if (value === null) return "산출 보류";
  const number = Number(value);
  return Number.isFinite(number) ? `${(number * 100).toFixed(0)}%` : String(value);
}

function amount(value: string, currency: string): string {
  const number = Number(value);
  return Number.isFinite(number)
    ? new Intl.NumberFormat("ko-KR", { style: "currency", currency, maximumFractionDigits: 2 }).format(number)
    : value;
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
    investment_amount: "1000000",
    currency: "KRW",
    universe_id: "KOREA_GLOBAL_MIXED",
    query: "투자 성향에 맞는 국내·글로벌 포트폴리오 후보와 종목 근거를 검토해줘",
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
  const resultCurrency = typeof (result?.suitability as { currency?: unknown } | undefined)?.currency === "string"
    ? String((result?.suitability as { currency: string }).currency)
    : input.currency;

  useEffect(() => {
    let active = true;
    void fetchPortfolioUniverses()
      .then((payload) => {
        if (active) setUniverses(payload.universes);
      })
      .catch(() => {
        // The backend validates the selection on submit and remains the source of truth.
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
        <span className="window-controls" aria-hidden="true">— ▢ ✕</span>
      </div>
      <div className="win-body">
        <div className="section-heading">
          <div>
            <p className="eyebrow">USER PROFILE → LANGGRAPH → ADVISORY PORTFOLIO</p>
            <h2 id="portfolio-interview-title">사용자에게 맞는 포트폴리오 받기</h2>
          </div>
          <span className={`status-pill ${running ? "working" : result ? "done" : "waiting"}`}>
            {busy ? "요청 중" : running ? "LANGGRAPH RUNNING" : result ? runtime?.status : connection.toUpperCase()}
          </span>
        </div>
        <p className="dash-note">처음 주식을 접해도 괜찮습니다. 투자 성향·경험·기간·손실 감내도·유니버스를 기준으로 백엔드 suitability 엔진이 후보와 종목을 선별합니다.</p>
        <form id="portfolio-interview-form" className="portfolio-form" onSubmit={submit}>
          <label>사용자 식별자<input value={input.user_id} onChange={(event) => setInput({ ...input, user_id: event.target.value })} required /></label>
          <label>투자 성향<select value={input.mindset} onChange={(event) => setInput({ ...input, mindset: event.target.value as PortfolioInterviewInput["mindset"] })}><option value="SAFETY_FIRST">안전 우선</option><option value="BALANCED">균형형</option><option value="RISK_SEEKING">성장·위험 감수</option></select></label>
          <label>투자 경험<select value={input.experience} onChange={(event) => setInput({ ...input, experience: event.target.value as PortfolioInterviewInput["experience"] })}><option value="BEGINNER">처음 접함</option><option value="INTERMEDIATE">어느 정도 경험</option><option value="EXPERIENCED">경험 많음</option></select></label>
          <label>투자 예정 기간(년)<input type="number" min="1" max="100" value={input.investment_horizon_years} onChange={(event) => setInput({ ...input, investment_horizon_years: Number(event.target.value) })} required /></label>
          <label>투자 가능 금액<input inputMode="decimal" min="1" value={input.investment_amount} onChange={(event) => setInput({ ...input, investment_amount: event.target.value })} required /></label>
          <label>통화<select value={input.currency} onChange={(event) => setInput({ ...input, currency: event.target.value as PortfolioInterviewInput["currency"] })}><option value="KRW">KRW · 원화</option><option value="USD">USD · 달러</option><option value="EUR">EUR · 유로</option></select></label>
          <label>투자 유니버스<select value={input.universe_id} onChange={(event) => setInput({ ...input, universe_id: event.target.value })} required>{universes.length ? universes.map((universe) => <option key={universe.universe_id} value={universe.universe_id}>{universe.name} · {universe.instrument_count}개 · {universe.status}</option>) : <option value="KOREA_GLOBAL_MIXED">국내·글로벌 혼합 유니버스</option>}</select></label>
          <label className="portfolio-query">원하는 투자 질문·조건<textarea value={input.query} onChange={(event) => setInput({ ...input, query: event.target.value })} maxLength={2000} placeholder="예: 국내 반도체 중심으로 3년 투자하고, 손실 위험과 근거를 함께 설명해줘" /><small>자유롭게 작성하면 CEO 라우터가 필요한 부서와 Worker만 배정합니다.</small></label>
          <label>감내 가능한 최대 손실률<input type="number" min="1" max="100" step="1" value={Number(input.max_drawdown_pct) * 100} onChange={(event) => setInput({ ...input, max_drawdown_pct: (Number(event.target.value) / 100).toFixed(4) })} required /><small>예: 10 = 최대 -10%</small></label>
        </form>
        {(submitError || error) && <p className="form-error">⚠️ {submitError || error}</p>}
        {runtime?.phase && <p className="runtime-phase"><b>현재 단계:</b> {runtime.phase}</p>}
        {result && <div className="portfolio-result" aria-label="포트폴리오 추천 결과">
          <div className="result-heading"><div><p className="eyebrow">BACKEND SUITABILITY RESULT</p><h3>{recommendations.length ? "추천 후보와 종목" : "조건에 맞는 후보가 없습니다"}</h3></div><span className={`status-pill ${approval?.status === "APPROVE" ? "done" : approval?.status === "REJECT" ? "blocked" : "approval"}`}>{approval?.status === "APPROVE" ? "사용자 승인 완료" : approval?.status === "REJECT" ? "사용자 거절" : "사용자 검토 필요"}</span></div>
          {result.universe && <p className="universe-summary">유니버스: <b>{result.universe.name}</b> · {result.universe.status} · {result.universe.source}</p>}
          {result.unresolved_asset_classes?.length ? <p className="risk-warning">선택한 유니버스에 없는 자산군: {result.unresolved_asset_classes.map((asset) => ASSET_LABEL[asset] ?? asset).join(", ")} · 완성되지 않은 추천은 승인할 수 없습니다.</p> : null}
          {recommendations.map((item) => {
            const highRisk = Object.keys(item.target_allocations ?? {}).some((asset) => ["LEVERAGED_ETF", "SHORT_EXPOSURE", "DERIVATIVES_HEDGE"].includes(asset));
            const rows = instrumentRecommendations.filter((instrument) => instrument.portfolio_id === item.portfolio_id);
            return <article className="portfolio-recommendation" key={item.portfolio_id}>
              <div><strong>{item.name}</strong><code>{item.portfolio_id}</code></div>
              <span className="score">{item.fit_score}점 · {item.risk_band}</span>
              <p>목표 비중 {Object.entries(item.target_allocations ?? {}).map(([asset, weight]) => `${ASSET_LABEL[asset] ?? asset} ${percent(weight)}`).join(" · ")}</p>
              <p>목표 금액 {Object.entries(item.target_amounts ?? {}).map(([asset, value]) => `${ASSET_LABEL[asset] ?? asset} ${amount(value, resultCurrency)}`).join(" · ")}</p>
              <div className="instrument-recommendations"><strong>추천 종목</strong>{rows.length ? rows.map((instrument) => <div className="instrument-row" key={`${item.portfolio_id}-${instrument.symbol}`}><span><b>{instrument.name}</b> <code>{instrument.exchange}:{instrument.symbol}</code></span><span>{percent(instrument.target_weight)} · {amount(instrument.target_amount, resultCurrency)}</span><small>{instrument.expected_return === null ? "예상 수익률 산출 보류" : `${percent(instrument.expected_return)} 예상`}</small></div>) : <small>선택한 유니버스에서 매칭되는 종목이 없어 추천을 확정하지 않았습니다.</small>}</div>
              {highRisk && <p className="risk-warning">고위험 요소가 포함된 후보입니다. 레버리지·공매도·파생상품 조건을 별도 검토해야 합니다.</p>}
              <p>{item.reasons.join(" · ")}</p><small>근거 {item.evidence_refs.join(", ")}</small>
            </article>;
          })}
          <p className="forecast-notice">{result.forecast_notice ?? "예상 수익률은 보장되지 않으며, 시장 데이터의 기준시점과 근거를 확인해야 합니다."}</p>
        </div>}
        {recommendations.length > 0 && approval?.status === "PENDING" && <div className="approval-actions"><p className="dash-note">추천 내용을 확인한 뒤 사용자 승인 단계로 진행합니다. 승인해도 주문·승인권한·원장 변경은 수행하지 않습니다.</p><button type="button" className="btn-primary" onClick={() => void decide("APPROVE")} disabled={approvalBusy}>{approvalBusy ? "처리 중" : "추천 승인"}</button><button type="button" className="btn-ghost" onClick={() => void decide("REJECT")} disabled={approvalBusy}>추천 거절</button></div>}
      </div>
    </section>
  );
}
