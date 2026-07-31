"use client";

// 트레이딩본부 · 회계/포트폴리오본부 실제 상태 패널.
//
// 소유: 도현
// 근거: docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md 3.2(6·7), 4, 8, 11
//
// 이 패널은 픽셀 오피스 시뮬레이션과 **데이터 원천이 다르다.** 캐릭터가 책상에
// 앉았다는 이유로 무엇도 판단하지 않고(계획 3.1), Read Model이 실은 확정 상태만
// 표시한다. 그래서 mode·snapshot 시각을 항상 같이 띄운다 — 어떤 데이터를 보고
// 있는지 화면에서 구분되지 않으면 DEMO를 실거래로 착각하게 된다(계획 4절, 8절).

import { useEffect, useState } from "react";

import AgentAsk from "./AgentAsk";
import rawSnapshot from "./trading-snapshot.json";
import {
  BFF,
  parseSnapshot,
  percent,
  won,
  type BrokerOrderRow,
  type TradingSnapshot,
} from "./readModel";

/**
 * 번들된 Fixture. BFF가 안 뜬 상태에서도 화면이 비지 않게 하는 **대체재**이며
 * 최신 상태가 아니다. 그래서 아래에서 출처 배지를 항상 같이 띄운다 —
 * 어제 Fixture를 오늘 장부로 착각하는 것이 이 화면의 최악 실패다(계획 4절).
 */
let fixture: TradingSnapshot | null = null;
let fixtureError = "";
try {
  fixture = parseSnapshot(rawSnapshot);
} catch (error) {
  fixtureError = String(error instanceof Error ? error.message : error);
}

type Source = "fixture" | "bff";

/** 주문 상태 → 기존 오피스 색 토큰. 색만으로 구분하지 않고 글자를 함께 쓴다(계획 8절). */
const orderTone: Record<string, string> = {
  FILLED: "done",
  PARTIALLY_FILLED: "working",
  ACKNOWLEDGED: "working",
  SUBMITTED: "working",
  CREATED: "waiting",
  CANCEL_PENDING: "approval",
  CANCELLED: "blocked",
  REJECTED: "blocked",
  EXPIRED: "blocked",
  UNKNOWN: "blocked",
};

const intentTone: Record<string, string> = {
  READY_TO_SUBMIT: "done",
  APPROVED: "done",
  RESIZED: "working",
  RISK_PENDING: "approval",
  USER_PENDING: "approval",
  USER_APPROVED: "done",
  DRAFT: "waiting",
  REJECTED: "blocked",
  EXPIRED: "blocked",
};

function shortId(value: string): string {
  return value.length > 8 ? `${value.slice(0, 8)}…` : value;
}

function OrderRow({ order }: { order: BrokerOrderRow }) {
  return (
    <div className="result-row">
      <span>
        <b>{order.side}</b> <code>{shortId(order.client_order_id)}</code>
      </span>
      <span>
        {order.filled_quantity} / {order.requested_quantity}
      </span>
      <span>{order.average_fill_price ? won(order.average_fill_price) : "—"}</span>
      <span className={`status-pill ${orderTone[order.state] ?? "waiting"}`}>{order.state}</span>
    </div>
  );
}

export default function OpsPanel() {
  // Fixture로 먼저 그리고, BFF가 응답하면 그걸로 갈아끼운다. BFF가 없거나
  // 계약이 깨지면 Fixture에 머무르되 배지를 "번들 Fixture"로 유지한다 —
  // 실패를 조용히 통과시켜 오래된 수치를 최신처럼 보여주지 않기 위해서다.
  const [snap, setSnap] = useState<TradingSnapshot | null>(fixture);
  const [source, setSource] = useState<Source>("fixture");
  const [loadError, setLoadError] = useState(fixtureError);

  useEffect(() => {
    let alive = true;
    fetch(`${BFF}/ui/snapshot`)
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`HTTP ${res.status}`))))
      .then((raw) => {
        if (!alive) return;
        setSnap(parseSnapshot(raw));
        setSource("bff");
        setLoadError("");
      })
      .catch((error) => {
        if (!alive) return;
        setLoadError(`BFF 조회 실패 — ${String(error instanceof Error ? error.message : error)}`);
      });
    return () => {
      alive = false;
    };
  }, []);

  if (!snap) {
    return (
      <section className="win ops-snap">
        <div className="win-bar">
          <span>📉 trading_portfolio.snapshot</span>
          <span className="window-controls">—　▢　✕</span>
        </div>
        <div className="win-body">
          <p className="eyebrow">READ MODEL 오류</p>
          <p>{loadError}</p>
          <p>
            <small>
              계약이 맞지 않아 수치를 표시하지 않습니다. 추측해서 그리지 않는 것이 이 화면의 규칙입니다.
            </small>
          </p>
        </div>
      </section>
    );
  }

  const { portfolio, trading, ledger, mode } = snap;

  return (
    <section className="win ops-snap">
      <div className="win-bar">
        <span>📉 trading_portfolio.snapshot</span>
        <span className="window-controls">—　▢　✕</span>
      </div>
      <div className="win-body">
        <div className="section-heading">
          <div>
            <p className="eyebrow">트레이딩 · 회계/포트폴리오</p>
            <h2>주문·체결과 공식 장부</h2>
          </div>
          <div className="filter-tabs" role="group" aria-label="데이터 출처">
            <span className={`status-pill ${mode === "DEMO" ? "waiting" : "done"}`}>{mode}</span>
            {/* mode(DEMO/PAPER/LIVE)와 출처는 다른 축이다. DEMO Fixture와 DEMO
                BFF 응답이 같은 배지로 보이면 어느 쪽을 보는지 알 수 없다. */}
            <span className={`status-pill ${source === "bff" ? "done" : "waiting"}`}>
              {source === "bff" ? "실시간 조회" : "번들 Fixture"}
            </span>
            <span className="status-pill">v{snap.snapshot_version}</span>
          </div>
        </div>

        {loadError && (
          <p className="dash-note">
            ⚠️ {loadError} — 아래는 <b>번들 Fixture</b>이며 최신 상태가 아닙니다. BFF를 띄우세요:{" "}
            <code>uvicorn apps.api.main:app --port 8000</code>
          </p>
        )}

        <p className="dash-note">
          픽셀 오피스의 캐릭터 움직임과 <b>다른 데이터</b>입니다. 아래 수치는 OMS·원장·평가가 확정한
          값이며 화면이 계산하지 않습니다. 기준 시각 {new Date(portfolio.as_of).toLocaleString("ko-KR")}.
        </p>

        {trading.blocked_by_unknown && (
          <p className="dash-note">
            ⚠️ 상태 불명(UNKNOWN) 주문이 있어 이 Fund의 <b>신규 주문이 차단</b>된 상태입니다. Broker
            Reconciliation으로 확정해야 풀립니다.
          </p>
        )}

        <section className="summary-grid" aria-label="포트폴리오 요약">
          <article className="metric yellow">
            <span>NAV</span>
            <strong>{won(portfolio.nav)}</strong>
            <small>순자산</small>
          </article>
          <article className="metric mint">
            <span>현금</span>
            <strong>{won(portfolio.cash)}</strong>
            <small>CASH</small>
          </article>
          <article className="metric pink">
            <span>평가액</span>
            <strong>{won(portfolio.securities_value)}</strong>
            <small>SECURITIES</small>
          </article>
          <article className="metric lav">
            <span>실현손익</span>
            <strong>{won(portfolio.realized_pnl)}</strong>
            <small>REALIZED</small>
          </article>
          <article className="metric white">
            <span>평가손익</span>
            <strong>{won(portfolio.unrealized_pnl)}</strong>
            <small>UNREALIZED</small>
          </article>
        </section>

        <div className="two-col">
          <div>
            <p className="eyebrow">보유 종목 {portfolio.positions.length}</p>
            <div className="result-table">
              <div className="result-row header">
                <span>종목 · 평균단가</span>
                <span>수량</span>
                <span>평가액</span>
                <span>비중</span>
              </div>
              {portfolio.positions.length === 0 ? (
                <div className="result-row">
                  <span>보유 종목 없음</span>
                  <span>—</span>
                  <span>—</span>
                  <span>—</span>
                </div>
              ) : (
                portfolio.positions.map((position) => (
                  <div className="result-row" key={position.instrument_id}>
                    <span>
                      <code>{shortId(position.instrument_id)}</code> · {won(position.average_cost)}
                    </span>
                    <span>{position.quantity}</span>
                    <span>{won(position.market_value)}</span>
                    <span>{percent(position.weight)}</span>
                  </div>
                ))
              )}
            </div>
          </div>

          <div>
            <p className="eyebrow">브로커 주문 {trading.orders.length}</p>
            <div className="result-table">
              <div className="result-row header">
                <span>구분 · 주문번호</span>
                <span>체결/주문</span>
                <span>평균체결가</span>
                <span>상태</span>
              </div>
              {trading.orders.length === 0 ? (
                <div className="result-row">
                  <span>주문 없음</span>
                  <span>—</span>
                  <span>—</span>
                  <span>—</span>
                </div>
              ) : (
                trading.orders.map((order) => <OrderRow key={order.order_id} order={order} />)
              )}
            </div>
          </div>
        </div>

        {/* Order Intent와 Broker Order를 한 표로 합치지 않는다. 리스크본부 거부와
            브로커 거부는 서로 다른 사건이고, 그 구분이 v1.2 상태 머신 분리의 이유다. */}
        <p className="eyebrow">주문 의도(Order Intent) {trading.intents.length}</p>
        <div className="result-table">
          <div className="result-row header">
            <span>Intent</span>
            <span>수량</span>
            <span>Risk 판정</span>
            <span>상태</span>
          </div>
          {trading.intents.map((intent) => (
            <div className="result-row" key={intent.order_intent_id}>
              <span>
                <code>{shortId(intent.order_intent_id)}</code>
              </span>
              <span>{intent.requested_quantity}</span>
              <span>
                {intent.risk_decision_id ? (
                  <code>{shortId(intent.risk_decision_id)}</code>
                ) : (
                  <span className="status-pill waiting">미심사</span>
                )}
              </span>
              <span className={`status-pill ${intentTone[intent.state] ?? "waiting"}`}>
                {intent.state}
              </span>
            </div>
          ))}
        </div>

        <p className="dash-note">
          원장 분개 {ledger.journal_count}건 · 반대분개 {ledger.reversal_count}건 · 차대균형{" "}
          {ledger.balanced ? "일치" : `불일치(${ledger.trial_balance_sum})`} · 수수료{" "}
          {won(portfolio.fees)} · 세금 {won(portfolio.taxes)}
        </p>
      </div>
      <AgentAsk />
    </section>
  );
}
