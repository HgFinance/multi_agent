"use client";

import { useEffect, useRef, useState } from "react";
import type { Agent, Company, DeptStatus, Snapshot } from "../game/sim";
import { STAFF } from "../game/staff";
import { DEPT_ROOMS } from "../game/world";

/**
 * 우측 레일 — ceo.console / live.feed / staff.roster.
 *
 * 셋 다 엔진 상태를 읽어 보여줄 뿐이고, 유일한 쓰기 경로는 engine.command()다.
 * 지시 해석·응답·로그 적재는 전부 엔진 안에 있던 것을 그대로 쓴다.
 */

const QUICK_ORDERS = [
  { label: "현황 보고", command: "현황 보고해줘" },
  { label: "회의 소집", command: "전 부서 회의 소집" },
  { label: "왜 늦어져?", command: "왜 늦어지고 있어?" },
];

const DEPT_DOT: Record<DeptStatus, string> = {
  "완료": "bg-tertiary-fixed-dim border-tertiary-fixed-dim",
  "진행 중": "bg-primary border-primary",
  "승인 대기": "bg-error border-error",
  "연동 대기": "bg-outline border-outline",
  "대기": "bg-transparent border-outline",
};

function PanelBar({
  tone,
  icon,
  title,
  children,
}: {
  tone: "primary" | "secondary";
  icon: string;
  title: string;
  children?: React.ReactNode;
}) {
  return (
    <div
      className={`${tone === "primary" ? "bg-primary" : "bg-secondary"} text-on-primary px-4 py-2.5 flex items-center justify-between gap-2 shrink-0`}
    >
      <span className="flex items-center gap-2 font-bold text-body-sm font-body-sm">
        <span className="material-symbols-outlined text-[18px]" aria-hidden="true">{icon}</span>
        {title}
      </span>
      <span className="flex gap-1 opacity-70" aria-hidden="true">{children}</span>
    </div>
  );
}

export default function RightRail({
  engine,
  snap,
  selectedId,
  onSelect,
}: {
  engine: Company;
  snap: Snapshot;
  selectedId: string | null;
  onSelect: (agent: Agent) => void;
}) {
  // 레일에 lg:overflow-y-auto 를 둔 것은 안전장치다. 세 패널 높이 합이 고정
  // 높이를 넘으면 레일이 뷰포트 밖으로 흘러 페이지 스크롤로도 닿을 수 없게
  // 되는데, 그때 레일이 스스로 스크롤한다. 평소엔 staff.roster가 안에서
  // 스크롤하므로 쓰이지 않는다.
  const [draft, setDraft] = useState("");
  const chatRef = useRef<HTMLDivElement>(null);
  const chatCount = snap.chat.length;

  useEffect(() => {
    const el = chatRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [chatCount]);

  function send(text: string) {
    const value = text.trim();
    if (!value) return;
    engine.command(value);
    setDraft("");
  }

  return (
    <aside className="w-full lg:w-[390px] shrink-0 flex flex-col gap-4 lg:sticky lg:top-4 lg:h-[calc(100vh-2rem)] lg:min-h-0 lg:overflow-y-auto">
      {/* ── ceo.console ─────────────────────────────────── */}
      <section className="bg-surface-container-lowest border border-outline-variant rounded-lg overflow-hidden shadow-sm flex flex-col shrink-0">
        <PanelBar tone="primary" icon="desktop_windows" title="ceo.console">
          <span className="material-symbols-outlined text-[16px]">minimize</span>
          <span className="material-symbols-outlined text-[16px]">crop_square</span>
          <span className="material-symbols-outlined text-[16px]">close</span>
        </PanelBar>

        <div ref={chatRef} className="p-4 flex flex-col gap-3 overflow-y-auto max-h-64" aria-live="polite" aria-label="대표 지시창 대화">
          {snap.chat.map((entry) => (
            <div
              key={entry.id}
              className={`rounded-lg border p-3 max-w-[92%] ${
                entry.from === "ceo"
                  ? "self-end bg-secondary-container border-secondary-container"
                  : "self-start bg-surface-container-low border-outline-variant"
              }`}
            >
              <div className="font-bold text-body-sm font-body-sm text-primary mb-1">
                {entry.from === "ceo" ? "대표님" : entry.name}
              </div>
              <p className="text-body-sm font-body-sm text-on-surface m-0 whitespace-pre-line">{entry.text}</p>
              <div className="text-right text-[10px] text-outline mt-1 font-data-mono">{entry.time}</div>
            </div>
          ))}
        </div>

        <div className="px-4 pb-3 flex flex-wrap gap-1.5 shrink-0">
          {QUICK_ORDERS.map((item) => (
            <button
              key={item.label}
              type="button"
              onClick={() => send(item.command)}
              className="px-2 py-0.5 rounded-full border border-outline-variant bg-surface-container-low text-[11px] leading-4 text-on-surface-variant hover:bg-surface-container transition-colors"
            >
              {item.label}
            </button>
          ))}
        </div>

        <form
          className="border-t border-outline-variant p-3 flex gap-2 shrink-0"
          onSubmit={(event) => {
            event.preventDefault();
            send(draft);
          }}
        >
          <input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="Instruct AI Office..."
            aria-label="대표 지시 입력"
            className="flex-1 min-w-0 border border-outline-variant rounded p-2.5 text-body-sm font-body-sm bg-surface-container-lowest focus:border-primary focus:ring-1 focus:ring-primary focus:outline-none"
          />
          <button
            type="submit"
            disabled={!draft.trim()}
            className="px-5 bg-primary text-on-primary rounded font-bold text-body-sm hover:bg-primary-container transition-colors disabled:opacity-40 shrink-0"
          >
            Send
          </button>
        </form>
      </section>

      {/* ── live.feed ───────────────────────────────────── */}
      <section className="bg-surface-container-lowest border border-outline-variant rounded-lg overflow-hidden shadow-sm flex flex-col shrink-0">
        <PanelBar tone="secondary" icon="bolt" title="live.feed">
          <span className="material-symbols-outlined text-[16px]">close</span>
        </PanelBar>
        {snap.meetingTitle ? (
          <p className="m-0 px-4 py-2 bg-secondary-container text-body-sm font-body-sm font-bold text-on-secondary-container border-b border-outline-variant">
            회의 진행 중 — {snap.meetingTitle}
          </p>
        ) : null}
        <ul className="m-0 p-0 list-none max-h-52 overflow-y-auto">
          {snap.log.map((entry) => (
            <li key={entry.id} className="flex gap-3 px-4 py-2.5 border-b border-outline-variant last:border-b-0">
              <span className="text-body-sm font-body-sm font-data-mono text-outline shrink-0">{entry.time}</span>
              <span className="text-body-sm font-body-sm text-on-surface min-w-0">
                <span aria-hidden="true">{entry.icon} </span>
                {entry.text}
              </span>
            </li>
          ))}
        </ul>
      </section>

      {/* ── staff.roster — 남은 높이를 다 먹고 그 안에서 스크롤한다 ── */}
      <section className="bg-surface-container-lowest border border-outline-variant rounded-lg overflow-hidden shadow-sm flex flex-col flex-1 min-h-40">
        <PanelBar tone="secondary" icon="groups" title="staff.roster">
          <span className="material-symbols-outlined text-[16px]">search</span>
        </PanelBar>
        {/* min-h-0 이 없으면 flex 자식이 안 줄어들어 스크롤이 안 생긴다 */}
        <div className="flex-1 min-h-0 overflow-y-auto p-4 flex flex-col gap-4" aria-label="직원 명단">
          {DEPT_ROOMS.map((room) => (
            <div key={room.id}>
              <p className="flex items-center gap-2 m-0 mb-2">
                <span aria-hidden="true">{room.icon}</span>
                <b className="text-body-md font-body-md font-bold text-on-surface">{room.name}</b>
                <i
                  className={`ml-auto w-3 h-3 rounded-full border-2 shrink-0 ${DEPT_DOT[snap.deptStatus[room.id] ?? "대기"]}`}
                  title={snap.deptStatus[room.id] ?? "대기"}
                />
              </p>
              <div className="flex flex-wrap gap-2">
                {STAFF.filter((seed) => seed.deptId === room.id).map((seed) => {
                  const agent = engine.agentById.get(seed.id);
                  const on = selectedId === seed.id;
                  return (
                    <button
                      key={seed.id}
                      type="button"
                      onClick={() => agent && onSelect(agent)}
                      className={`flex items-center gap-2 px-3 py-2 rounded border text-body-sm font-body-sm transition-colors ${
                        on ? "border-primary bg-secondary-container" : "border-outline-variant hover:bg-surface-container"
                      }`}
                    >
                      <i
                        className="w-2.5 h-2.5 rounded-full border shrink-0"
                        style={{ background: seed.shirt, borderColor: seed.hair }}
                        aria-hidden="true"
                      />
                      <span className="font-medium text-on-surface">{seed.name}</span>
                      <small className="text-outline">{agent?.status ?? "출근 전"}</small>
                    </button>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      </section>
    </aside>
  );
}
