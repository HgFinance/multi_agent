"use client";

const HERMES_DASHBOARD_URL = (
  process.env.NEXT_PUBLIC_HERMES_DASHBOARD_URL || "http://127.0.0.1:9119"
).replace(/\/+$/, "");

/**
 * The official Hermes Dashboard is the Kanban UI. This component embeds that
 * UI instead of recreating its board in the Pixel Office, so task state,
 * assignees and controls remain owned by Hermes.
 */
export default function HermesKanbanEmbed() {
  return (
    <section className="win hermes-kanban-embed" aria-labelledby="hermes-kanban-title">
      <div className="win-bar">
        <span>🗂 Hermes Kanban Dashboard</span>
        <span className="window-controls" aria-hidden="true">— ▢ ✕</span>
      </div>
      <div className="hermes-kanban-toolbar">
        <div>
          <p className="eyebrow">SOURCE OF TASK STATE · HERMES</p>
          <h2 id="hermes-kanban-title">공용 Task Graph / Kanban</h2>
          <p>사용자 질의와 부서별 업무 배정은 Hermes 보드에서 확인합니다.</p>
        </div>
        <a
          className="btn btn-ghost"
          href={HERMES_DASHBOARD_URL}
          target="_blank"
          rel="noreferrer"
        >
          새 창에서 열기
        </a>
      </div>
      <div className="hermes-kanban-frame-wrap">
        <iframe
          className="hermes-kanban-frame"
          src={HERMES_DASHBOARD_URL}
          title="Hermes official Kanban Dashboard"
          loading="lazy"
          referrerPolicy="no-referrer"
        />
      </div>
      <p className="hermes-kanban-note">
        Dashboard Profile이 켜져 있고 자체 인증이 설정되어 있어야 표시됩니다. 현재 개발 기본 주소는
        <code>{HERMES_DASHBOARD_URL}</code>입니다.
      </p>
    </section>
  );
}
