"use client";

import AgentAsk from "./AgentAsk";
import { useBffFeed } from "./bffClient";
import type { BrokerOrderRow } from "./readModel";
import { percent, won } from "./readModel";

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

const orderLabel: Record<string, string> = {
  FILLED: "체결 완료",
  PARTIALLY_FILLED: "부분 체결",
  ACKNOWLEDGED: "접수됨",
  SUBMITTED: "제출됨",
  CREATED: "생성됨",
  CANCEL_PENDING: "취소 대기",
  CANCELLED: "취소됨",
  REJECTED: "거절됨",
  EXPIRED: "만료됨",
  UNKNOWN: "상태 확인 필요",
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

const intentLabel: Record<string, string> = {
  READY_TO_SUBMIT: "제출 준비",
  APPROVED: "승인됨",
  RESIZED: "수량 조정됨",
  RISK_PENDING: "Risk 검토 대기",
  USER_PENDING: "대표 승인 대기",
  USER_APPROVED: "대표 승인됨",
  DRAFT: "초안",
  REJECTED: "거절됨",
  EXPIRED: "만료됨",
};

function shortId(value: string): string {
  return value.length > 8 ? `${value.slice(0, 8)}…` : value;
}

function timeLabel(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("ko-KR");
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
      <span>{won(order.average_fill_price)}</span>
      <span className={`status-pill ${orderTone[order.state] ?? "waiting"}`} title={order.state}>{orderLabel[order.state] ?? order.state}</span>
    </div>
  );
}

function BackendEmptyState({
  connection,
  error,
  refresh,
  compact = false,
}: {
  connection: string;
  error: string;
  refresh: () => Promise<void>;
  compact?: boolean;
}) {
  return (
    <section className={`${compact ? "ops-snap-compact" : "win"} ops-snap`} aria-labelledby="ops-snapshot-title">
      {!compact && <div className="win-bar">
        <span>📉 trading_portfolio.snapshot</span>
        <span className="window-controls" aria-hidden="true">
          — ▢ ✕
        </span>
      </div>}
      <div className={`${compact ? "ops-snap-body" : "win-body"} backend-empty-state`}>
        <p className="eyebrow">READ MODEL · {connection.toUpperCase()}</p>
        <h2 id="ops-snapshot-title">백엔드 Snapshot을 기다리는 중입니다</h2>
        <p>{error || "GET /ui/snapshot 응답을 기다리고 있습니다."}</p>
        <p>
          <code>uvicorn apps.api.main:app --port 8001</code>
        </p>
        <button type="button" className="btn-small" onClick={() => void refresh()}>
          다시 연결
        </button>
      </div>
    </section>
  );
}

export default function OpsPanel({ compact = false }: { compact?: boolean }) {
  const { snapshot, connection, error, lastUpdated, refresh } = useBffFeed();
  if (!snapshot) return <BackendEmptyState connection={connection} error={error} refresh={refresh} compact={compact} />;

  const { portfolio, trading, ledger, mode } = snapshot;
  return (
    <section className={`${compact ? "ops-snap-compact" : "win"} ops-snap`} aria-labelledby="ops-snapshot-title">
      {!compact && <div className="win-bar">
        <span>📉 trading_portfolio.snapshot</span>
        <span className="window-controls" aria-hidden="true">
          — ▢ ✕
        </span>
      </div>}
      <div className={compact ? "ops-snap-body" : "win-body"}>
        <div className="section-heading">
          <div>
            <p className="eyebrow">트레이딩 · 회계/포트폴리오</p>
            <h2 id="ops-snapshot-title">주문·체결과 공식 장부</h2>
          </div>
          <div className="filter-tabs" role="group" aria-label="데이터 출처">
            <span className="status-pill done">{mode}</span>
            <span className={`status-pill ${connection === "connected" ? "done" : "approval"}`}>
              {connection === "connected" ? "BFF Read Model" : connection.toUpperCase()}
            </span>
            <span className="status-pill">v{snapshot.snapshot_version}</span>
            <button type="button" className="btn-small" onClick={() => void refresh()}>
              새로고침
            </button>
          </div>
        </div>
        <p className="dash-note">
          픽셀 오피스의 캐릭터 움직임과 다른 데이터입니다. OMS·원장·평가가 확정한 값을 표시하며 화면에서 계산하지 않습니다.
          기준 시각 {timeLabel(portfolio.as_of)} · BFF 수신 {lastUpdated ? timeLabel(lastUpdated) : "—"}
        </p>
        {trading.blocked_by_unknown && (
          <p className="dash-note">
            ⚠️ 상태 불명(UNKNOWN) 주문이 있어 Fund의 <b>신규 주문이 차단</b>된 상태입니다. Broker Reconciliation으로 확정해야 풀립니다.
          </p>
        )}

        <section className="summary-grid" aria-label="포트폴리오 요약">
          {[
            ["NAV", portfolio.nav, "순자산"],
            ["현금", portfolio.cash, "CASH"],
            ["평가액", portfolio.securities_value, "SECURITIES"],
            ["실현손익", portfolio.realized_pnl, "REALIZED"],
            ["평가손익", portfolio.unrealized_pnl, "UNREALIZED"],
          ].map(([label, value, note]) => (
            <article key={label}>
              <span>{label}</span>
              <strong>{won(value as string)}</strong>
              <small>{note}</small>
            </article>
          ))}
        </section>

        <div className="two-col">
          <section>
            <div className="section-heading">
              <h3>포지션 {portfolio.positions.length}</h3>
            </div>
            <div className="result-table">
              <div className="result-row header">
                <span>종목</span>
                <span>평균단가</span>
                <span>수량</span>
                <span>평가액</span>
                <span>비중</span>
              </div>
              {portfolio.positions.length === 0 ? (
                <div className="result-row">
                  <span>없음</span>
                  <span>—</span>
                  <span>—</span>
                  <span>—</span>
                  <span>—</span>
                </div>
              ) : (
                portfolio.positions.map((position) => (
                  <div className="result-row" key={position.instrument_id}>
                    <code>{shortId(position.instrument_id)}</code>
                    <span>{won(position.average_cost)}</span>
                    <span>{position.quantity}</span>
                    <span>{won(position.market_value)}</span>
                    <span>{percent(position.weight)}</span>
                  </div>
                ))
              )}
            </div>
          </section>

          <section>
            <div className="section-heading">
              <h3>브로커 주문 {trading.orders.length}</h3>
            </div>
            <div className="result-table">
              <div className="result-row header">
                <span>주문번호</span>
                <span>체결/주문</span>
                <span>평균체결가</span>
                <span>상태</span>
              </div>
              {trading.orders.length === 0 ? (
                <div className="result-row">
                  <span>없음</span>
                  <span>—</span>
                  <span>—</span>
                  <span>—</span>
                </div>
              ) : (
                trading.orders.map((order) => <OrderRow key={order.order_id} order={order} />)
              )}
            </div>
          </section>
        </div>

        <section>
          <p className="eyebrow">ORDER INTENT {trading.intents.length}</p>
          <div className="result-table">
            <div className="result-row header">
              <span>Intent</span>
              <span>수량</span>
              <span>Risk 판정</span>
              <span>상태</span>
            </div>
            {trading.intents.map((intent) => (
              <div className="result-row" key={intent.order_intent_id}>
                <code>{shortId(intent.order_intent_id)}</code>
                <span>{intent.requested_quantity}</span>
                <span>
                  {intent.risk_decision_id ? <code>{shortId(intent.risk_decision_id)}</code> : "미심사"}
                </span>
                <span className={`status-pill ${intentTone[intent.state] ?? "waiting"}`} title={intent.state}>{intentLabel[intent.state] ?? intent.state}</span>
              </div>
            ))}
          </div>
        </section>

        <p className="dash-note">
          원장 분개 {ledger.journal_count}건 · 반대분개 {ledger.reversal_count}건 · 차대균형 {ledger.balanced ? "일치" : `불일치(${ledger.trial_balance_sum})`} · 수수료 {won(portfolio.fees)} · 세금 {won(portfolio.taxes)}
        </p>
        <AgentAsk />
      </div>
    </section>
  );
}
