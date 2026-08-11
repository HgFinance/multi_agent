"use client";

import { useEffect, useId, useState } from "react";
import {
  askCeo, ceoProgress,
  type CardOutcome, type CeoQueryProgress, type CeoQueryResult,
} from "./ceoClient";

const QUICK_PROMPTS = [
  "오늘 전체 업무 현황을 요약해줘",
  "지금 막혀 있는 업무와 이유를 알려줘",
  "리서치팀의 최신 진행 상황을 브리핑해줘",
];

// 카드 결말 → 화면 문구·색. 성공/실패를 뭉개지 않는 것이 이 표의 목적이다.
// 아래 Kanban 임베드는 보드 원본이라 결과가 빈 완료도 `done` 으로 보인다.
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
  "hr-department": "Agent 인사팀",
};

export default function CeoCommandPanel() {
  const inputId = useId();
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<CeoQueryResult | null>(null);
  const [progress, setProgress] = useState<CeoQueryProgress | null>(null);
  const [error, setError] = useState("");

  const rootTaskId = result?.task?.task_id ?? null;

  // 본부 카드가 다 끝날 때까지 5초마다 한 번씩 다시 본다. 상태가 바뀔 때마다
  // 다음 한 번을 예약하는 방식이라 언마운트·재질의에서 취소가 새지 않는다.
  useEffect(() => {
    if (!rootTaskId || progress?.all_terminal) return;
    const handle = setTimeout(async () => {
      try {
        setProgress(await ceoProgress(rootTaskId));
      } catch (cause) {
        // 진행 조회 실패를 "완료"로 바꾸지 않는다. 멈춘 채로 사유를 보여준다.
        setError(cause instanceof Error ? cause.message : "진행 상태를 읽지 못했습니다.");
      }
    }, progress ? 5000 : 1000);
    return () => clearTimeout(handle);
  }, [rootTaskId, progress]);

  async function submit(query = draft) {
    const value = query.trim();
    if (!value || busy) return;

    setDraft("");
    setBusy(true);
    setError("");
    setProgress(null);
    try {
      setResult(await askCeo(value));
    } catch (cause) {
      setResult(null);
      setError(cause instanceof Error ? cause.message : "CEO Hermes에 연결하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="ceo-command-panel win" aria-labelledby={`${inputId}-title`}>
      <div className="win-bar ceo-command-bar">
        <span>✦ CEO Control Room</span>
        <span className="command-live-dot"><i aria-hidden="true" /> Hermes endpoint</span>
      </div>
      <div className="ceo-command-body">
        <div className="ceo-command-intro">
          <div>
            <p className="eyebrow">ASK THE OFFICE</p>
            <h2 id={`${inputId}-title`}>무엇을 확인할까요?</h2>
            <p>자연어로 질문하면 CEO Hermes가 업무를 만들고, 결과는 Kanban에서 추적됩니다.</p>
          </div>
          <span className="command-shortcut">⌘ / Ctrl ↵ 전송</span>
        </div>

        <form className="ceo-command-form" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
          <label htmlFor={inputId}>대표님 질의</label>
          <textarea
            id={inputId}
            value={draft}
            rows={3}
            maxLength={2000}
            placeholder="예: 오늘 리스크가 큰 업무만 먼저 브리핑해줘"
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                event.preventDefault();
                void submit();
              }
            }}
          />
          <div className="ceo-command-form-footer">
            <span>{draft.length}/2000 · 조회와 자문만 수행합니다</span>
            <button type="submit" className="btn btn-primary" disabled={busy || !draft.trim()}>
              {busy ? "답변 준비 중…" : "CEO에게 전달"}
            </button>
          </div>
        </form>

        <div className="ceo-quick-prompts" aria-label="추천 질문">
          <span>빠른 질문</span>
          {QUICK_PROMPTS.map((prompt) => (
            <button type="button" key={prompt} onClick={() => void submit(prompt)} disabled={busy}>
              {prompt}
            </button>
          ))}
        </div>

        {busy ? (
          <div className="ceo-command-state" role="status" aria-live="polite">
            <i className="command-spinner" aria-hidden="true" /> CEO Hermes가 질의를 정리하고 있습니다.
          </div>
        ) : null}
        {error ? <p className="ceo-command-error" role="alert">⚠️ {error}</p> : null}
        {result ? (
          <div className="ceo-command-result" aria-live="polite">
            <div className="ceo-command-result-heading">
              <span>CEO Hermes 답변</span>
              {result.task?.task_id ? <code>{result.task.task_id}</code> : <small>Kanban task 대기</small>}
            </div>
            <p>{result.answer}</p>
            {result.task?.task_id ? <small>질의가 공용 Kanban에 등록되었습니다. 아래 보드에서 담당과 상태를 확인하세요.</small> : null}
          </div>
        ) : null}

        {progress ? (
          <div className="ceo-progress" aria-live="polite">
            <div className="ceo-progress-heading">
              <span>본부 진행</span>
              <small>
                {progress.finished}/{progress.total} 종료
                {progress.all_terminal ? "" : " · 확인 중"}
              </small>
            </div>
            <ul className="ceo-progress-cards">
              {progress.cards.map((card) => {
                const view = OUTCOME[card.outcome];
                return (
                  <li key={card.task_id} data-tone={view.tone}>
                    <span className="status-pill">{view.label}</span>{" "}
                    <b>{DEPARTMENT_LABEL[card.department] ?? card.department}</b> — {card.title}
                    {/* 사유는 접지 않는다. 왜 못 했는지가 결과보다 중요할 때가 많다. */}
                    {card.summary ? <div className="ceo-progress-reason">{card.summary}</div> : null}
                  </li>
                );
              })}
            </ul>
            {progress.unusable.length > 0 ? (
              <p className="ceo-command-error">
                ⚠️ {progress.unusable.length}개 본부가 사용 가능한 결과를 내지 못했습니다.
                위 사유를 확인하세요 — 이 부분은 답변에 반영되지 않았습니다.
              </p>
            ) : null}
            {progress.all_terminal && !progress.answer_grounded ? (
              <p className="ceo-command-error">
                ⚠️ 본부가 확인해 준 내용이 없습니다. 위 답변은 근거 없는 부분을 포함할 수 있습니다.
              </p>
            ) : null}
          </div>
        ) : null}
      </div>
    </section>
  );
}
