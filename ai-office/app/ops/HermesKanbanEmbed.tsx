"use client";

const HERMES_DASHBOARD_URL = (
  process.env.NEXT_PUBLIC_HERMES_DASHBOARD_URL || "http://127.0.0.1:9119"
).replace(/\/+$/, "");

/** Hermes owns Kanban state and controls; the office only provides the doorway. */
export default function HermesKanbanEmbed() {
  return (
    <section className="win hermes-kanban-embed" aria-labelledby="hermes-kanban-title">
      <div className="win-bar">
        <span>▦ Hermes Kanban Dashboard</span>
        <span className="window-controls" aria-hidden="true">— ▢ ✕</span>
      </div>
      <div className="hermes-kanban-toolbar">
        <div className="hermes-kanban-heading">
          <div className="hermes-kanban-heading-row">
            <span className="kanban-source-badge">SOURCE OF TRUTH</span>
            <span className="kanban-source-status"><i aria-hidden="true" /> Hermes</span>
          </div>
          <h2 id="hermes-kanban-title">공용 Task Graph / Kanban</h2>
          <p>사용자 질의와 부서별 업무 배정은 이 보드의 상태를 기준으로 확인합니다.</p>
        </div>
        <a className="btn btn-ghost" href={HERMES_DASHBOARD_URL} target="_blank" rel="noreferrer">
          보드 새 창으로 열기 ↗
        </a>
      </div>
      <div className="hermes-kanban-frame-wrap">
        <iframe
          className="hermes-kanban-frame"
          src={HERMES_DASHBOARD_URL}
          title="Hermes official Kanban Dashboard"
          loading="eager"
          referrerPolicy="no-referrer"
        />
      </div>
      <p className="hermes-kanban-note">
        Dashboard Profile 인증이 필요합니다. 보드가 보이지 않으면 새 창으로 열어 Hermes 인증 상태를 확인하세요.
        <code>{HERMES_DASHBOARD_URL}</code>
      </p>
    </section>
  );
}
