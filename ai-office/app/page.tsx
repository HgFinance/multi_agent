"use client";

import type { CSSProperties } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import OfficeWorld from "./game/OfficeWorld";
import TopNav from "./components/TopNav";
import OfficeControls from "./components/OfficeControls";
import RightRail from "./components/RightRail";
import SiteFooter from "./components/SiteFooter";
import { Company, type Agent, type Snapshot } from "./game/sim";

/**
 * 픽셀 오피스 하나만 띄운다.
 *
 * 대시보드·Mandate·Ops 패널 등 이전 프론트 구성은 전부 걷어냈다.
 * 남은 것은 시뮬레이션 엔진(app/game/)과 그것을 보여주는 월드,
 * 직원 클릭 시 뜨는 프로필뿐이다. 화면은 여기서부터 다시 쌓는다.
 */

/** 모달 포커스 트랩 + Esc 닫기. */
function useDialogBehavior(onClose: () => void) {
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    const dialog = dialogRef.current;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (!dialog) return undefined;
    const focusable = () => Array.from(dialog.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ));
    focusable()[0]?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusable();
      if (items.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    dialog.addEventListener("keydown", handleKeyDown);
    return () => {
      dialog.removeEventListener("keydown", handleKeyDown);
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, []);

  return dialogRef;
}

function PixelEmployee({ hair, shirt, accent }: { hair: string; shirt: string; accent: string }) {
  const style = {
    "--pixel-hair": hair,
    "--pixel-shirt": shirt,
    "--pixel-accent": accent,
  } as CSSProperties;
  return (
    <span className="pixel-employee" style={style} aria-hidden="true">
      <i className="pixel-shadow" />
      <i className="pixel-legs" />
      <i className="pixel-body" />
      <i className="pixel-arm left" />
      <i className="pixel-arm right" />
      <i className="pixel-face">
        <b className="pixel-eyes" />
      </i>
      <i className="pixel-hair" />
      <i className="pixel-headset" />
    </span>
  );
}

function ProfileModal({ agent, onClose }: { agent: Agent; onClose: () => void }) {
  const dialogRef = useDialogBehavior(onClose);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <section
        ref={dialogRef}
        className="win team-modal"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={`${agent.name} 프로필`}
        tabIndex={-1}
      >
        <div className="win-bar">
          <span>👤 employee_profile.exe</span>
          <button type="button" className="window-close" aria-label="프로필 닫기" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="win-body employee-profile">
          <div className="profile-top">
            <PixelEmployee hair={agent.hair} shirt={agent.shirt} accent={agent.accent} />
            <div>
              <span className="status-pill working">{agent.status}</span>
              <h2>
                {agent.name}
                {agent.callsign ? <small> · {agent.callsign}</small> : null}
              </h2>
              <p>{agent.role}</p>
            </div>
          </div>
          <div className="profile-task">
            <span className="tiny-label">지금 하는 일</span>
            <strong>{agent.taskLabel}</strong>
            {agent.anim === "type" ? (
              <span className="profile-progress">
                <i style={{ width: `${Math.round(agent.progress * 100)}%` }} />
              </span>
            ) : null}
          </div>
          <div className="report-box">
            <span className="tiny-label">한마디</span>
            <strong>{agent.speech ?? agent.thoughts[0]}</strong>
          </div>
          <div className="profile-actions">
            <button className="text-button" onClick={onClose}>
              닫기
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}

export default function Home() {
  const [engine] = useState(() => new Company());
  const [snap, setSnap] = useState<Snapshot>(() => engine.snapshot());
  const [selectedId, setSelectedId] = useState<string | null>(null);

  useEffect(() => {
    let raf = 0;
    let last = performance.now();
    let acc = 0;
    const loop = (now: number) => {
      const dt = (now - last) / 1000;
      last = now;
      engine.tick(dt);
      acc += dt;
      if (acc >= 0.18) {
        acc = 0;
        setSnap(engine.snapshot());
      }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [engine]);

  const onSelect = useCallback((agent: Agent) => setSelectedId(agent.id), []);
  const selected = selectedId ? engine.agentById.get(selectedId) ?? null : null;
  const onDuty = engine.agents.filter((agent) => agent.status !== "출근 전").length;

  return (
    <div className="bg-background text-on-background min-h-screen flex flex-col font-sans">
      <TopNav current="ai-office" />
      <main className="flex-1 w-full max-w-app mx-auto p-margin-mobile md:p-margin-desktop flex flex-col gap-gutter">
        {/* 상단: 개요&컨트롤 — 가로 전체 폭 */}
        <OfficeControls engine={engine} snap={snap} onDuty={onDuty} onStart={() => engine.start()} />

        {/* 하단: 좌측 픽셀 오피스 / 우측 나머지 위젯(live.feed, staff.roster) */}
        <div className="flex flex-col lg:flex-row gap-gutter items-start">
          <div className="flex-1 min-w-0 w-full">
            {/* 픽셀 오피스 월드는 손대지 않는다. 자체 스타일(office.css)을 그대로 쓴다. */}
            <OfficeWorld engine={engine} snap={snap} selectedId={selectedId} follow onSelect={onSelect} />
          </div>
          <RightRail engine={engine} snap={snap} selectedId={selectedId} onSelect={onSelect} />
        </div>
      </main>
      <SiteFooter />
      {selected ? <ProfileModal agent={selected} onClose={() => setSelectedId(null)} /> : null}
    </div>
  );
}
