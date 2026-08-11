"use client";

// 사용자 입구 — 질문 하나가 CEO 를 거쳐 본부로 갈라졌다가 다시 모이는 화면.
//
// 소유: 재일 (리서치 + 퀀트·백테스트) — 사용자 입구 배선분
// 근거: docs/HEDGE_FUND_MASTER_PLAN.md 5.6(권한 분리)
//       docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md 6(명령 경계)
//
// ▶ 이 상자는 본부를 고르지 않는다.
//   `AgentAsk`(회계 전용 상자)와 다른 물건이다. 저건 화면이 이미 부서를 아는
//   경우고, 이건 사용자가 그냥 묻는 자리다. 어느 본부가 필요한지는 CEO 가
//   정한다 - 화면에 부서 선택을 두는 순간 라우팅이 사용자 몫이 된다.
//
// ▶ 여기 숫자는 공식 수치가 아니다.
//   NAV·Position·수익률의 공식 값은 `/ui/snapshot` 카드뿐이다. 이 답변은
//   본부들이 쓴 문장을 CEO 가 모은 것이고, 화면은 여기서 숫자를 옮겨 적지 않는다.
//
// ▶ 실패를 성공처럼 그리지 않는다.
//   카드가 `done` 이어도 결과 본문이 비면 백엔드가 `NO_ANSWER` 로 준다
//   (실측: 회계본부가 "NAV 데이터가 없어 산출 불가"를 완료로 기록했다).
//   그건 초록색이 아니라 노란색이고, 사유를 그대로 보여준다.

import { useEffect, useState } from "react";

import { BFF } from "./readModel";

type CardOutcome =
  | "QUEUED" | "RUNNING" | "ANSWERED" | "NO_ANSWER" | "BLOCKED" | "FAILED"
  | "STALE" | "NO_ASSIGNEE";

type Card = {
  task_id: string;
  title: string;
  department: string;
  outcome: CardOutcome;
  summary: string;
  has_result: boolean;
  depends_on: string[];
};

type Ticket = {
  ticket_id: string;
  phase: "ROUTING" | "WORKING" | "SYNTHESIZING" | "ANSWERED" | "TIMEOUT" | "FAILED";
  question: string;
  routing_note: string;
  answer: string;
  error: string;
  elapsed_seconds: number;
  answer_grounded: boolean;
  total?: number;
  finished?: number;
  cards?: Card[];
  unusable?: string[];
  stalled?: string[];
};

const PHASE_LABEL: Record<Ticket["phase"], string> = {
  ROUTING: "대표이사가 담당 본부를 정하는 중",
  WORKING: "본부가 작업 중",
  SYNTHESIZING: "대표이사가 결과를 모으는 중",
  ANSWERED: "답변 완료",
  TIMEOUT: "시간 안에 끝나지 않음",
  FAILED: "실패",
};

// 카드 결말 → 화면 문구·색. 성공/실패를 뭉개지 않는 것이 이 표의 목적이다.
const OUTCOME: Record<CardOutcome, { label: string; tone: "ok" | "warn" | "bad" | "wait" }> = {
  QUEUED: { label: "대기", tone: "wait" },
  RUNNING: { label: "작업 중", tone: "wait" },
  ANSWERED: { label: "답변함", tone: "ok" },
  NO_ANSWER: { label: "답을 못 냄", tone: "warn" },
  BLOCKED: { label: "보류", tone: "warn" },
  FAILED: { label: "실행 실패", tone: "bad" },
  STALE: { label: "아무도 안 집어감", tone: "bad" },
  // 없는 본부에 배정된 카드는 기다려도 안 돈다. "대기"로 보이면 안 된다.
  NO_ASSIGNEE: { label: "실행 불가", tone: "bad" },
};

const DEPARTMENT_LABEL: Record<string, string> = {
  "ceo-agent": "대표이사실",
  "research-department": "리서치본부",
  "trading-department": "트레이딩본부",
  "risk-management": "리스크관리본부",
  "quant-backtest-department": "퀀트·백테스트본부",
  "accounting-portfolio-department": "회계·포트폴리오본부",
  "qa-department": "AI QA·감사본부",
  "workforce-management": "Agent 인사팀",
};

const DONE_PHASES = new Set<Ticket["phase"]>(["ANSWERED", "TIMEOUT", "FAILED"]);

export default function UserAskPanel() {
  const [query, setQuery] = useState("");
  const [ticket, setTicket] = useState<Ticket | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // 폴링은 티켓 상태가 바뀔 때마다 다음 한 번을 예약하는 방식이다.
  // 스스로를 호출하는 setTimeout 재귀는 언마운트·재요청 때 취소가 새기 쉽다.
  useEffect(() => {
    if (!ticket || DONE_PHASES.has(ticket.phase)) return;
    const handle = setTimeout(async () => {
      try {
        const res = await fetch(`${BFF}/ui/ask/${ticket.ticket_id}`);
        if (!res.ok) {
          setError(`상태 조회 실패 (HTTP ${res.status})`);
          setBusy(false);
          return;
        }
        const body: Ticket = await res.json();
        setTicket(body);
        if (DONE_PHASES.has(body.phase)) setBusy(false);
      } catch (e) {
        // 연결이 끊긴 것을 "완료"로 바꾸지 않는다. 멈춘 채로 사유를 보여준다.
        setError(`BFF 연결이 끊겼습니다 — ${String(e)}`);
        setBusy(false);
      }
    }, 5000);
    return () => clearTimeout(handle);
  }, [ticket]);

  async function ask() {
    setBusy(true);
    setError("");
    setTicket(null);
    try {
      const res = await fetch(`${BFF}/ui/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
      });
      const body = await res.json();
      // 503 은 고장이 아니라 기본 상태다 — 비용이 나가는 경로라 기본은 닫혀 있다.
      if (!res.ok) {
        setError(body.detail ?? `HTTP ${res.status}`);
        setBusy(false);
        return;
      }
      setTicket(body);  // 위 useEffect 가 이어서 폴링한다
    } catch (e) {
      setError(`BFF 연결 실패 — uvicorn 이 떠 있는지 확인하세요 (${String(e)})`);
      setBusy(false);
    }
  }

  const cards = ticket?.cards ?? [];
  const unusable = cards.filter((c) =>
    ["NO_ANSWER", "BLOCKED", "FAILED", "STALE", "NO_ASSIGNEE"].includes(c.outcome),
  );

  return (
    <section className="win-body" aria-label="대표이사실에 묻기">
      <div className="section-heading">
        <div>
          <p className="eyebrow">대표이사실 · 사용자 입구</p>
          <h2>무엇이든 물어보세요</h2>
        </div>
        <span className="status-pill waiting">참고용</span>
      </div>

      <p className="dash-note">
        어느 본부가 맡을지는 <b>대표이사가 정합니다.</b> 본부가 확인하지 못한 항목은
        빈칸으로 남고, 대표이사가 대신 채우지 않습니다. 공식 수치는 위 스냅샷 카드입니다.
      </p>

      <textarea
        className="agent-ask-input"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        rows={3}
        maxLength={4000}
        placeholder="예: 삼성전자를 300주 들고 있는데 이 비중을 유지해도 될까요?"
        disabled={busy}
      />
      <button className="btn btn-small" onClick={ask} disabled={busy || !query.trim()}>
        {busy ? "본부에 전달 중…" : "물어보기"}
      </button>

      {error && <p className="dash-note">⚠️ {error}</p>}

      {ticket && (
        <div className="user-ask-result">
          <p className="dash-note">
            <span className={`status-pill ${DONE_PHASES.has(ticket.phase) ? "" : "waiting"}`}>
              {PHASE_LABEL[ticket.phase]}
            </span>{" "}
            {ticket.elapsed_seconds}초 경과
            {typeof ticket.total === "number" && ticket.total > 0
              ? ` · 카드 ${ticket.finished ?? 0}/${ticket.total}`
              : ""}
          </p>

          {ticket.routing_note && (
            <p className="dash-note">
              <b>배분:</b> {ticket.routing_note}
            </p>
          )}

          {cards.length > 0 && (
            <ul className="user-ask-cards">
              {cards.map((card) => {
                const view = OUTCOME[card.outcome];
                return (
                  <li key={card.task_id} data-tone={view.tone}>
                    <span className="status-pill">{view.label}</span>{" "}
                    <b>{DEPARTMENT_LABEL[card.department] ?? card.department}</b> — {card.title}
                    {/* 사유는 접지 않는다. 왜 못 했는지가 결과보다 중요할 때가 많다. */}
                    {card.summary && <div className="user-ask-reason">{card.summary}</div>}
                  </li>
                );
              })}
            </ul>
          )}

          {ticket.answer && (
            <div className="user-ask-answer">
              <p className="dash-note">
                <span className="status-pill">비공식</span>
                {!ticket.answer_grounded && (
                  <span className="status-pill waiting"> 본부 확인 없음</span>
                )}
              </p>
              <p className="dash-note" style={{ whiteSpace: "pre-wrap" }}>{ticket.answer}</p>
            </div>
          )}

          {unusable.length > 0 && (
            <p className="dash-note">
              ⚠️ {unusable.length}개 본부가 결과를 내지 못했습니다. 위 사유를 확인하세요 —
              이 부분은 답변에 반영되지 않았습니다.
            </p>
          )}

          {ticket.error && <p className="dash-note">⚠️ {ticket.error}</p>}
        </div>
      )}
    </section>
  );
}
