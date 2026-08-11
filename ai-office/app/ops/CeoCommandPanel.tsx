"use client";

import { useId, useState } from "react";
import { askCeo, type CeoQueryResult } from "./ceoClient";

const QUICK_PROMPTS = [
  "오늘 전체 업무 현황을 요약해줘",
  "지금 막혀 있는 업무와 이유를 알려줘",
  "리서치팀의 최신 진행 상황을 브리핑해줘",
];

export default function CeoCommandPanel() {
  const inputId = useId();
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<CeoQueryResult | null>(null);
  const [error, setError] = useState("");

  async function submit(query = draft) {
    const value = query.trim();
    if (!value || busy) return;

    setDraft("");
    setBusy(true);
    setError("");
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
      </div>
    </section>
  );
}
