"use client";

import type { CSSProperties, FormEvent } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import OfficeWorld from "./game/OfficeWorld";
import {
  buildReport,
  fetchIntegrations,
  publish,
  type IntegrationStatus,
  type PublishResult,
} from "./game/report";
import { Company, PHASES, type Agent, type DeptStatus, type Snapshot } from "./game/sim";
import { CEO, DEPT_BRIEF, DEPT_LEAD, STAFF } from "./game/staff";
import { DEPT_ROOMS } from "./game/world";
import { COMPANY, STORAGE_LINK } from "../company.config";
import OpsPanel from "./ops/OpsPanel";
import DepartmentRuntimePanel from "./ops/RiskQaPanel";
import DepartmentCommunicationPanel from "./ops/DepartmentCommunicationPanel";
import { BffProvider } from "./ops/bffClient";
import { useBffFeed } from "./ops/bffClient";
import PortfolioInterviewPanel, { PortfolioKanban, PortfolioResultConsole, type RuntimeResult } from "./ops/PortfolioInterviewPanel";
import { startSavedPortfolioRecommendation } from "./ops/portfolioClient";
import type { LlmPerformanceMetric } from "./ops/readModel";
import { groupRuntimeMessages, readPitReadiness, readablePitReason, readableRuntimeKind, readableRuntimeMessage, readableRuntimeStatus } from "./ops/statusLabels";
import { canUseSimulation } from "./ops/projectionSource";

type View = "live" | "dashboard" | "mandate";
type DashboardAudience = "executive" | "operations";
const CANONICAL_DEPARTMENT_COUNT = 8;
const CANONICAL_WORKER_COUNT = 35;

const statusClass: Record<DeptStatus, string> = {
  "완료": "done",
  "진행 중": "working",
  "승인 대기": "approval",
  "연동 대기": "blocked",
  "대기": "waiting",
};


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

/** 링크만 걸려 있는 항목 (서버 연동과 무관) */
const integrations2Static = STORAGE_LINK
  ? [{ name: "결과물 보관함", status: "링크 연결", tone: "mint", href: STORAGE_LINK }]
  : [];

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

function RuntimeSync({ engine, onSync }: { engine: Company; onSync: () => void }) {
  const { snapshot } = useBffFeed();
  useEffect(() => {
    const mode = snapshot?.mode ?? "DEMO";
    engine.setSimulationMode(canUseSimulation(mode));
    if (!canUseSimulation(mode)) {
      engine.applyRuntime(snapshot?.operations?.runtime ?? null);
    }
    onSync();
  }, [engine, onSync, snapshot?.mode, snapshot?.operations?.runtime]);
  return null;
}

export default function Home() {
  const [engine] = useState(() => new Company());
  const [snap, setSnap] = useState<Snapshot>(() => engine.snapshot());
  const syncRuntime = useCallback(() => setSnap(engine.snapshot()), [engine]);
  const [view, setView] = useState<View>("live");
  const [dashboardAudience, setDashboardAudience] = useState<DashboardAudience>("executive");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [follow, setFollow] = useState(true);
  const [briefing, setBriefing] = useState(false);
  const [filter, setFilter] = useState<"전체" | DeptStatus>("전체");
  const [toast, setToast] = useState("");
  const [integrations, setIntegrations] = useState<IntegrationStatus | null>(null);
  const [publishState, setPublishState] = useState<{ busy: boolean; result: PublishResult | null; error: string }>({
    busy: false,
    result: null,
    error: "",
  });
  const publishedRef = useRef(false);

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

  useEffect(() => {
    engine.setBriefingHandler(() => setBriefing(true));
    return () => engine.setBriefingHandler(null);
  }, [engine]);

  const showToast = useCallback((message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(""), 2400);
  }, []);

  const onSelect = useCallback((agent: Agent) => setSelectedId(agent.id), []);

  // 연동 설정 여부를 서버에서 받아온다 (값이 아니라 설정 여부만)
  useEffect(() => {
    fetchIntegrations()
      .then(setIntegrations)
      .catch(() => setIntegrations(null));
  }, []);

  const sendReport = useCallback(
    async (auto: boolean) => {
      setPublishState((state) => ({ ...state, busy: true, error: "" }));
      try {
        const result = await publish(buildReport(engine.snapshot()));
        setPublishState({ busy: false, result, error: "" });

        const parts: string[] = [];
        parts.push(result.notion.ok ? "Notion 저장 완료" : `Notion ${result.notion.detail ?? "실패"}`);
        parts.push(result.discord.ok ? "Discord 전송 완료" : `Discord ${result.discord.detail ?? "실패"}`);
        engine.pushLog(
          result.notion.ok && result.discord.ok ? "📤" : "⚠️",
          `완료 보고 발행 — ${parts.join(" / ")}`,
          result.notion.ok && result.discord.ok ? "mint" : "lav",
        );
        engine.pushChat("staff", "김세리", `보고서 발행 결과입니다.\n· ${parts.join("\n· ")}`);
        if (!auto) showToast(result.notion.ok || result.discord.ok ? "보고서를 발행했어요" : "발행 실패 — 연동 설정 필요");
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setPublishState({ busy: false, result: null, error: message });
        engine.pushLog("⚠️", `완료 보고 발행 실패 — ${message}`, "lav");
        if (!auto) showToast("발행 실패 — 연동 설정을 확인해주세요");
      }
    },
    [engine, showToast],
  );

  // 하루가 끝나면 자동으로 한 번 발행한다
  useEffect(() => {
    if (snap.dayComplete && !publishedRef.current) {
      publishedRef.current = true;
      void sendReport(true);
    }
    if (!snap.dayComplete && snap.running) publishedRef.current = false;
  }, [snap.dayComplete, snap.running, sendReport]);

  const askAgent = useCallback(
    (agent: Agent) => {
      engine.command(`${agent.name} 지금 뭐해?`);
      setSelectedId(null);
      window.setTimeout(
        () => document.getElementById("ceo-console")?.scrollIntoView({ behavior: "smooth", block: "center" }),
        60,
      );
  },
  [engine],
  );

  const start = () => {
    setView("mandate");
  };

  const approve = () => {
    showToast("추천 결과는 비바인딩 자문입니다. 별도 수동 검토가 필요합니다.");
  };

  const teams = useMemo(
    () =>
      DEPT_ROOMS.map((room) => {
        const lead = DEPT_LEAD[room.id];
        const status = snap.deptStatus[room.id] ?? "대기";
        return {
          id: room.id,
          icon: room.icon,
          name: room.name,
          room: room.short,
          lead,
          status,
          ...DEPT_BRIEF[room.id],
        };
      }),
    [snap.deptStatus],
  );

  const filteredTeams = filter === "전체" ? teams : teams.filter((team) => team.status === filter);
  const selected = selectedId ? engine.agentById.get(selectedId) ?? null : null;
  const todo = snap.approvalPending ? 1 : 0;
  const onDuty = engine.agents.filter((a) => a.status === "업무 중").length;

  return (
    <main className="page-shell">
      <div className="wrap">
        <nav className="app-nav" aria-label="AI Company 화면 전환">
          <div className="brand-chip">
            <span>{COMPANY.logoLetter}</span>
            <b>{COMPANY.name}</b>
          </div>
          <div className="nav-tabs">
            <button className={view === "live" ? "active" : ""} onClick={() => setView("live")}>
              🎮 라이브 오피스
            </button>
            <a
              className={`nav-link ${view === "dashboard" ? "active" : ""}`}
              href="/dashboard"
              role="button"
              onClick={(event) => {
                event.preventDefault();
                setView("dashboard");
              }}
            >
              📊 대시보드
            </a>
            <a
              className={`nav-link ${view === "mandate" ? "active" : ""}`}
              href="/mandate"
              role="button"
              onClick={(event) => {
                event.preventDefault();
                setView("mandate");
              }}
            >
              🗂 Mandate 설정
            </a>
            <button
              className={`todo-tab ${todo ? "urgent" : ""}`}
              onClick={() => {
                setView("live");
                window.setTimeout(
                  () => document.getElementById("ceo-approval")?.scrollIntoView({ behavior: "smooth", block: "center" }),
                  60,
                );
              }}
            >
              📋 대표 할 일 <i>{todo}</i>
            </button>
          </div>
        </nav>

      <BffProvider>
        <RuntimeSync engine={engine} onSync={syncRuntime} />
        {view === "mandate" ? (
          <MandateConfigView onAnalyzed={() => setView("dashboard")}
            onBackToOperations={dashboardAudience === "operations" ? () => { setView("dashboard"); setDashboardAudience("operations"); } : undefined}
          />
        ) : view === "live" ? (
          <>
            <LiveView
              engine={engine}
              snap={snap}
              follow={follow}
              setFollow={setFollow}
              selectedId={selectedId}
              onSelect={onSelect}
              onStart={start}
              onApprove={approve}
              onDuty={onDuty}
              onPublish={() => showToast("포트폴리오 결과는 BFF에서 확인하고 별도 수동 검토합니다.")}
              publishBusy={publishState.busy}
              publishResult={publishState.result}
            />
          </>
        ) : (
          <DashboardView
            filteredTeams={filteredTeams}
            filter={filter}
            setFilter={setFilter}
            snap={snap}
            onApprove={approve}
            onSelect={(id) => setSelectedId(id)}
            integrations={integrations}
            publishResult={publishState.result}
            audience={dashboardAudience}
            setAudience={setDashboardAudience}
            onOpenMandate={() => { setDashboardAudience("operations"); setView("mandate"); }}
          />
        )}
      </BffProvider>

        <footer>
          {COMPANY.name} · {COMPANY.titlePrefix} {COMPANY.titleAccent}
        </footer>
      </div>

      {selected ? (
        <ProfileModal
          agent={selected}
          onClose={() => setSelectedId(null)}
          onAsk={(agent) => {
            setView("live");
            askAgent(agent);
          }}
        />
      ) : null}
      {briefing ? <BriefingModal snap={snap} onClose={() => setBriefing(false)} /> : null}
      <div className={`toast ${toast ? "show" : ""}`} role="status">
        {toast}
      </div>
    </main>
  );
}

export function MandateConfigView({ onAnalyzed, onBackToOperations }: { onAnalyzed: () => void; onBackToOperations?: () => void }) {
  return (
    <>
      <header className="mandate-hero win">
        <div className="win-bar"><span>📁 Mandate Configuration [F01]</span><span className="window-controls" aria-hidden="true">—　▢　✕</span></div>
        <div className="mandate-hero-body">
          <div><p className="eyebrow">ONE-TIME USER SETUP · ADVISORY ONLY</p><h1>대표님의 투자 기준을<br /><em className="highlight">한 번만 알려주세요</em></h1><p>기본값을 확인하고 저장하면, 이후 세부 조건은 AI Assistant가 대화로 확인합니다. 저장된 설정은 주문·원장 변경을 직접 수행하지 않습니다.</p></div>
          <div className="mandate-hero-stamp"><span>MODE</span><b>DEMO</b><small>프론트엔드 설정 화면</small>{onBackToOperations ? <button type="button" className="btn btn-ghost" onClick={onBackToOperations}>Operations Console로 돌아가기</button> : null}</div>
        </div>
      </header>
      <section className="mandate-layout">
        <div><PortfolioInterviewPanel onAnalyzed={onAnalyzed} /></div>
        <aside className="mandate-sidebar">
          <MandateAssistant />
          <section className="win mandate-guide"><div className="win-bar"><span>💡 parameter.guide</span><span className="window-controls" aria-hidden="true">—　▢　✕</span></div><div className="win-body"><ul><li>목표 문장은 Risk·QA 검토자가 맥락을 이해하는 데 사용됩니다.</li><li>기본값은 안전한 방향으로 채워져 있으며 고급 설정에서 바꿀 수 있습니다.</li><li>추천 승인도 주문 제출이나 원장 변경을 의미하지 않습니다.</li></ul></div></section>
        </aside>
      </section>
    </>
  );
}

/** Hydration fallback for the dashboard navigation. */
export function DashboardRouteView() {
  const { snapshot } = useBffFeed();
  const [operations, setOperations] = useState(false);

  if (operations) {
    return (
      <OperationsConsoleView
        snapshot={snapshot}
        onBack={() => setOperations(false)}
        onOpenMandate={() => window.location.assign("/mandate")}
      />
    );
  }

  return (
    <section className="win hero" aria-labelledby="dashboard-route-title">
      <div className="win-bar">
        <span>👑 CEO Dashboard</span>
        <span className="window-controls" aria-hidden="true">— ▢ ✕</span>
      </div>
      <div className="hero-body">
        <div className="hero-copy">
          <p className="eyebrow">READ MODEL · {snapshot ? "CONNECTED" : "OFFLINE"}</p>
          <h1 id="dashboard-route-title">대표 Dashboard</h1>
          <p>운영 상태와 실행 경계를 BFF Read Model로 확인합니다.</p>
        </div>
        <button type="button" className="btn btn-primary" onClick={() => setOperations(true)}>
          Operations Console
        </button>
      </div>
    </section>
  );
}

type AssistantMessage = { from: "agent" | "user"; text: string };

function MandateAssistant() {
  const [draft, setDraft] = useState("");
  const [step, setStep] = useState(0);
  const chatEndRef = useRef<HTMLDivElement>(null);
  const [messages, setMessages] = useState<AssistantMessage[]>([
    { from: "agent", text: "안녕하세요. 저는 김세리 AI 투자 어시스턴트입니다.\n기본 설정은 준비해 두었어요. 세부 조건은 제가 하나씩 여쭤볼게요." },
    { from: "agent", text: "먼저 투자 기간을 알려주세요. 예: 3년 이상, 은퇴 전까지, 단기 자금이에요." },
  ]);
  const questions = [
    "현금화가 필요한 시점이나 유동성 조건이 있나요?",
    "특정 업종이나 피하고 싶은 자산이 있나요?",
    "손실이 발생했을 때 어느 수준까지 감내할 수 있나요?",
  ];

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [messages.length]);

  function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = draft.trim();
    if (!value) return;
    setMessages((current) => [...current, { from: "user", text: value }, { from: "agent", text: questions[step] ?? "확인했어요. 이 내용은 설정 초안에 반영해둘게요." }]);
    setStep((current) => Math.min(current + 1, questions.length));
    setDraft("");
  }

  return (
    <section className="win mandate-assistant" aria-label="Mandate AI Assistant">
      <div className="win-bar"><span>🤖 CEO Console · AI Assistant</span><span className="window-controls" aria-hidden="true">—　▢　✕</span></div>
      <div className="win-body">
        <div className="assistant-heading"><div className="assistant-avatar">AI</div><div><strong>김세리</strong><small>Mandate interview worker · ONLINE</small></div><span className="mini-badge mint">ONLINE</span></div>
        <div className="assistant-chat" aria-live="polite" aria-label="Mandate 인터뷰 대화" tabIndex={0}>{messages.map((message, index) => <div className={`assistant-bubble ${message.from}`} key={`${message.from}-${index}`}><b>{message.from === "agent" ? "김세리 AI" : "대표님"}</b><p>{message.text}</p></div>)}<div ref={chatEndRef} aria-hidden="true" /></div>
        <form className="assistant-input" onSubmit={send}><input value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="자연어로 답해주세요…" aria-label="AI Assistant 답변" /><button type="submit">전송</button></form>
        <small className="assistant-note">대화 내용은 현재 화면의 설정 초안에만 표시됩니다.</small>
      </div>
    </section>
  );
}

function LiveView({
  engine,
  snap,
  follow,
  setFollow,
  selectedId,
  onSelect,
  onStart,
  onApprove,
  onDuty,
  onPublish,
  publishBusy,
  publishResult,
}: {
  engine: Company;
  snap: Snapshot;
  follow: boolean;
  setFollow: (value: boolean) => void;
  selectedId: string | null;
  onSelect: (agent: Agent) => void;
  onStart: () => void;
  onApprove: () => void;
  onDuty: number;
  onPublish: () => void;
  publishBusy: boolean;
  publishResult: PublishResult | null;
}) {
  const { snapshot: bffSnapshot } = useBffFeed();
  const runtime = bffSnapshot?.operations?.runtime;
  const mode = bffSnapshot?.mode ?? "DEMO";
  const progress = Math.round((snap.phaseIndex / (PHASES.length - 1)) * 100);

  return (
    <>
      <header className="live-hero">
        <div>
<p className="eyebrow">AI OFFICE · {CANONICAL_WORKER_COUNT} WORKERS · LANGGRAPH PROJECTION</p>
          <h1>
            {COMPANY.titlePrefix} <em className="highlight">{COMPANY.titleAccent}</em>
          </h1>
<p>실제 LangGraph가 실행 중인 Worker만 부서 안에서 작업하고, 부서 간 handoff는 부서장끼리만 진행합니다.</p>
        </div>
        <div className="live-clock">
          <span className={`mode-badge mode-${mode.toLowerCase()}`}>MODE · {mode}</span>
          <span>SEOUL</span>
          <b>{snap.clock}</b>
          <small>업무시간 09:00–18:00 · {snap.phase}</small>
        </div>
      </header>

      <section className="live-bar">
        <button className="btn btn-primary" onClick={onStart} disabled={snap.running}>
          {snap.running ? "실제 Worker가 분석 중…" : "사용자 입력으로 분석 시작"}
        </button>
        <button className="btn btn-ghost" onClick={() => engine.togglePause()}>
          {snap.paused ? "▶ 재생" : "⏸ 일시정지"}
        </button>
        <div className="speed-wrap">
          <span className="speed-label" title="시뮬레이션 전체(걷기·업무·대사)가 함께 빨라져요. 실제 외부 작업 속도와는 무관합니다.">
            재생 속도
          </span>
          <div className="speed-group" role="group" aria-label="재생 속도">
            {[1, 2, 4].map((value) => (
              <button
                key={value}
                className={!snap.turbo && snap.speed === value ? "on" : ""}
                onClick={() => engine.setSpeed(value)}
                title={value === 1 ? "말풍선 읽기·화면녹화용" : value === 4 ? "결과만 빠르게" : "기본"}
              >
                {value}x
              </button>
            ))}
            <button
              className={`skip ${snap.turbo ? "on" : ""}`}
              onClick={() => engine.skipToDecision()}
              disabled={!snap.running || snap.approvalPending}
              title="대표님이 결정할 일이 생길 때까지 단숨에 건너뜁니다"
            >
              {snap.turbo ? "건너뛰는 중…" : "⏭ 결정까지"}
            </button>
          </div>
        </div>
        <button className={`btn btn-ghost ${follow ? "on" : ""}`} onClick={() => setFollow(!follow)}>
          🎥 자동 추적 {follow ? "ON" : "OFF"}
        </button>
        <button
          className={`btn btn-ghost publish-btn ${publishResult?.notion.ok || publishResult?.discord.ok ? "sent" : ""}`}
          onClick={onPublish}
          disabled={publishBusy}
          title="완료 보고를 Notion에 저장하고 같은 내용을 Discord로 보냅니다"
        >
          {publishBusy ? "발행 중…" : "📤 보고 발행"}
        </button>
        <div className="live-progress">
          <span>
            {snap.phase} · {progress}%
          </span>
          <i>
            <b style={{ transform: `scaleX(${progress / 100})` }} />
          </i>
        </div>
        <div className="live-counts">
          <span className="lc on-duty">근무 {onDuty}</span>
          <span className="lc done">완료 {snap.stats.done}</span>
          <span className="lc working">진행 {snap.stats.working}</span>
          <span className="lc blocked">연동대기 {snap.stats.blocked}</span>
        </div>
      </section>

      <section className="live-grid">
        <OfficeWorld engine={engine} snap={snap} selectedId={selectedId} follow={follow} onSelect={onSelect} />

        <aside className="live-rail">
          <CeoConsole engine={engine} snap={snap} />

        <section className="win rail-card" id="legacy-ceo-approval">
            <div className="win-bar">
              <span>✅ ceo.approval</span>
              <span className="window-controls">—　▢　✕</span>
            </div>
            <div className={`win-body approval-body ${snap.approvalPending ? "pending" : ""}`}>
              {snap.approvalPending ? (
                <>
                  <div className="approval-top">
                    <span className="mini-badge yellow">TOP 1 제안 · 92점</span>
                    <span className="score blink">결재 대기</span>
                  </div>
                  <h3>AI 회사가 매일 아침 나 대신 출근한다면?</h3>
                  <p>회의실에서 최아름·한도빈·김세리가 대표님을 기다리고 있어요.</p>
                  <div className="reason-list">
                    <span>① 실제 구축 과정</span>
                    <span>② 저장할 운영 구조</span>
                    <span>③ 날것의 시행착오</span>
                  </div>
                  <button className="btn approve-button" onClick={onApprove}>
                    이 콘텐츠 승인하기
                  </button>
                </>
              ) : (
                <>
                  <div className="approval-top">
<span className="mini-badge mint">{runtime?.result ? "추천 결과 준비" : "결정 대기 없음"}</span>
                  </div>
<h3>{runtime?.result ? "사용자 적합성 결과가 준비됐습니다" : runtime?.phase ?? "프로필 입력을 기다리고 있습니다"}</h3>
                  <p>
                    {snap.approved
                      ? "대표 승인 이후 원고 → 제작 → 보관까지 이어집니다."
                      : "업무를 시작하면 콘텐츠 전략팀이 TOP 3를 회의실로 올려요."}
                  </p>
                </>
              )}
            </div>
          </section>

          <section className="win rail-card feed-card">
            <div className="win-bar">
              <span>📡 live.feed</span>
              <span className="window-controls">—　▢　✕</span>
            </div>
            <div className="win-body feed-body">
              {snap.meetingTitle ? <div className="feed-now">💬 회의 진행 중 — {snap.meetingTitle}</div> : null}
              <ul className="feed-list">
                {snap.log.map((entry) => (
                  <li key={entry.id} className={entry.tone}>
                    <b>{entry.time}</b>
                    <i>{entry.icon}</i>
                    <span>{entry.text}</span>
                  </li>
                ))}
              </ul>
            </div>
          </section>

          <section className="win rail-card">
            <div className="win-bar">
              <span>👥 staff.roster</span>
              <span className="window-controls">—　▢　✕</span>
            </div>
            <div className="win-body roster-body">
              {DEPT_ROOMS.map((room) => (
                <div className="roster-dept" key={room.id}>
                  <p>
                    <b>
                      {room.icon} {room.name}
                    </b>
                    <i className={`rm-dot ${statusClass[snap.deptStatus[room.id] ?? "대기"]}`} />
                  </p>
                  <div className="roster-chips">
                    {STAFF.filter((s) => s.deptId === room.id).map((seed) => {
                      const agent = engine.agentById.get(seed.id);
                      return (
                        <button
                          key={seed.id}
                          className={`roster-chip ${selectedId === seed.id ? "on" : ""}`}
                          onClick={() => agent && onSelect(agent)}
                        >
                          <i style={{ background: seed.shirt, borderColor: seed.hair }} />
                          {seed.name}
                          <small>{agent?.status ?? "출근 전"}</small>
                        </button>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          </section>
        </aside>
      </section>
    </>
  );
}

const QUICK_ORDERS = [
  { label: "현황 보고", command: "현황 보고해줘" },
  { label: "왜 늦어져?", command: "왜 늦어지고 있어?" },
  { label: "회의 소집", command: "전 부서 회의 소집" },
  { label: "지금 브리핑", command: "지금 브리핑 올라와" },
  { label: "집중 모드", command: "집중 모드" },
  { label: "속도 올려", command: "속도 좀 올려줘" },
];

function CeoConsole({ engine, snap }: { engine: Company; snap: Snapshot }) {
  const [draft, setDraft] = useState("");
  const logRef = useRef<HTMLDivElement>(null);
  const count = snap.chat.length;

  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [count]);

  const send = (text: string) => {
    const value = text.trim();
    if (!value) return;
    engine.command(value);
    setDraft("");
  };

  return (
    <section className="win rail-card console-card" id="ceo-console">
      <div className="win-bar">
        <span>🎤 ceo.console — 대표 지시창</span>
        <span className="window-controls">—　▢　✕</span>
      </div>
      <div className="win-body console-body">
        <div className="console-status">
          <span className={`mini-badge ${snap.focusMode ? "yellow" : "mint"}`}>
            {snap.focusMode ? "집중 모드 ON" : "평시 운영"}
          </span>
          {snap.busyWithOrder ? <span className="mini-badge lav">지시 처리 중…</span> : null}
        </div>

        <div className="console-log" ref={logRef}>
          {snap.chat.map((entry) => (
            <div key={entry.id} className={`console-line ${entry.from}`}>
              <b>{entry.from === "ceo" ? "대표님" : entry.name}</b>
              <p>{entry.text}</p>
              <small>{entry.time}</small>
            </div>
          ))}
        </div>

        <PortfolioResultConsole />

        <div className="console-quick">
          {QUICK_ORDERS.map((item) => (
            <button key={item.label} onClick={() => send(item.command)}>
              {item.label}
            </button>
          ))}
        </div>

        <form
          className="console-input"
          onSubmit={(event) => {
            event.preventDefault();
            send(draft);
          }}
        >
          <input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="예: 리서치팀 지금 뭐해?"
            aria-label="대표 지시 입력"
          />
          <button type="submit">지시</button>
        </form>
      </div>
    </section>
  );
}

function ProfileModal({
  agent,
  onClose,
  onAsk,
}: {
  agent: Agent;
  onClose: () => void;
  onAsk: (agent: Agent) => void;
}) {
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
            <button className="btn btn-primary" onClick={() => onAsk(agent)}>
              🎤 지금 뭐 하는지 물어보기
            </button>
            <button className="text-button" onClick={onClose}>
              닫기
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}

function BriefingModal({ snap, onClose }: { snap: Snapshot; onClose: () => void }) {
  const dialogRef = useDialogBehavior(onClose);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <section
        ref={dialogRef}
        className="win team-modal"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="김비서 브리핑"
        tabIndex={-1}
      >
        <div className="win-bar">
          <span>📋 kim_secretary.brief</span>
          <button type="button" className="window-close" aria-label="브리핑 닫기" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="win-body">
          <p className="brief-date">{snap.clock} · 김세리 비서실장 최종 브리핑</p>
          <h3>대표님, 오늘 회사 업무가 정리됐어요.</h3>
          <ul>
            <li>
              <span className="dot green" />
              완료 {snap.stats.done}팀 — 조사·기획·QA·대본·제작·저장까지 마쳤어요
            </li>
            <li>
              <span className="dot green" />
              대표 승인 1건 반영 — TOP 1 콘텐츠 제작 완료
            </li>
            <li>
              <span className="dot gray" />
              연동 대기 {snap.stats.blocked}팀 — 외부 서비스 연결이 필요해요
            </li>
          </ul>
          <div className="decision-box">
            <span className="tiny-label">오늘 대표님이 결정할 것</span>
            <strong>없습니다. 내일 07:00에 다시 출근할게요 ✨</strong>
          </div>
          <button className="btn btn-primary" onClick={onClose}>
            확인
          </button>
        </div>
      </section>
    </div>
  );
}

type TeamRow = {
  id: string;
  icon: string;
  name: string;
  room: string;
  lead: (typeof DEPT_LEAD)[string];
  status: DeptStatus;
  task: string;
  report: string;
};

function DashboardView({
  filteredTeams,
  filter,
  setFilter,
  snap,
  onApprove,
  onSelect,
  integrations,
  publishResult,
  audience,
  setAudience,
  onOpenMandate,
}: {
  filteredTeams: TeamRow[];
  filter: "전체" | DeptStatus;
  setFilter: (value: "전체" | DeptStatus) => void;
  snap: Snapshot;
  onApprove: () => void;
  onSelect: (id: string) => void;
  integrations: IntegrationStatus | null;
  publishResult: PublishResult | null;
  audience: DashboardAudience;
  setAudience: (value: DashboardAudience) => void;
  onOpenMandate: () => void;
}) {
  const { snapshot: bffSnapshot } = useBffFeed();
  const portfolioRuntime = bffSnapshot?.operations?.runtime;
  const portfolioResult = portfolioRuntime?.result as RuntimeResult | null | undefined;
  const mode = bffSnapshot?.mode ?? "DEMO";

  // 서버가 알려준 실제 설정 상태로 표시한다 (연결됐다고 거짓 보고하지 않는다)
  const liveRows = integrations
    ? [
        {
          name: "Notion 저장",
          status: publishResult?.notion.ok
            ? "저장 성공"
            : integrations.notion?.configured
              ? "키 설정됨"
              : "키 미설정",
          tone: publishResult?.notion.ok ? "mint" : integrations.notion?.configured ? "yellow" : "lav",
          href: "",
        },
        {
          name: "Discord 전송",
          status: publishResult?.discord.ok
            ? "전송 성공"
            : integrations.discord?.configured
              ? "웹훅 설정됨"
              : "웹훅 미설정",
          tone: publishResult?.discord.ok ? "mint" : integrations.discord?.configured ? "yellow" : "lav",
          href: "",
        },
        { name: "Instagram", status: integrations.instagram?.need ?? "연동 대기", tone: "lav", href: "" },
        { name: "Gmail", status: integrations.gmail?.need ?? "연동 대기", tone: "lav", href: "" },
        { name: "재무 파일", status: integrations.finance?.need ?? "자료 대기", tone: "lav", href: "" },
      ]
    : [];
  const rows = [...integrations2Static, ...liveRows];

  if (audience === "operations") {
    return (
      <OperationsConsoleView
        snapshot={bffSnapshot}
        onBack={() => setAudience("executive")}
        onOpenMandate={onOpenMandate}
      />
    );
  }

  return (
    <>
      <header className="win hero">
        <div className="win-bar">
          <span>👑 {COMPANY.windowLabel}</span>
          <span className="window-controls" aria-hidden="true">
            —　▢　✕
          </span>
        </div>
        <div className="hero-body">
          <div className="hero-copy">
          <p className="eyebrow">TODAY · 07:00 AUTO START <span className={`mode-badge mode-${mode.toLowerCase()}`}>MODE · {mode}</span></p>
            <h1>
              오늘 회사가 어떻게 움직이는지 <em className="highlight">한눈에</em> 보여드려요
            </h1>
 <p>Worker는 context를 만들고, 결정은 권한을 가진 결정론적 Gate와 대표님이 맡아요. {CANONICAL_DEPARTMENT_COUNT}개 부서 {CANONICAL_WORKER_COUNT}명의 흐름을 Projection으로 보여줘요.</p>
          </div>
          <div className="hero-actions">
            <span className="trust-copy">실제 전송·게시·결제는 대표 승인 후 진행해요</span>
            <div className="dashboard-audience-switch" role="group" aria-label="대시보드 사용자 전환">
              <span>VIEW</span>
              <button
                type="button"
                className={audience === "executive" ? "active" : ""}
                aria-pressed={audience === "executive"}
                onClick={() => setAudience("executive")}
              >
                대표 Dashboard
              </button>
              <button
                type="button"
                className=""
                aria-pressed={false}
                onClick={() => setAudience("operations")}
              >
                Operations Console
              </button>
            </div>
          </div>
        </div>
      </header>

      {portfolioRuntime?.run_id && <PortfolioKanban runtime={portfolioRuntime} result={portfolioResult} observedAt={bffSnapshot?.operations?.observed_at} />}
      {portfolioRuntime?.run_id && <PortfolioResultConsole />}

      <section className="summary-grid" aria-label="오늘 업무 요약">
        <article className="metric yellow">
<span>LangGraph Worker</span>
<strong>{CANONICAL_WORKER_COUNT}</strong>
<small>WORKERS</small>
        </article>
        <article className="metric mint">
          <span>완료</span>
          <strong>{snap.stats.done}</strong>
          <small>DONE</small>
        </article>
        <article className="metric pink">
          <span>진행 중</span>
          <strong>{snap.stats.working}</strong>
          <small>WORKING</small>
        </article>
        <article className="metric lav">
          <span>대표 확인</span>
          <strong>{snap.stats.approval}</strong>
          <small>APPROVAL</small>
        </article>
        <article className="metric white">
          <span>연동 대기</span>
          <strong>{snap.stats.blocked}</strong>
          <small>WAITING</small>
        </article>
      </section>

      <section className="workspace">
        <aside className="side-stack">
          <section className="win">
            <div className="win-bar">
              <span>⚡ automation.status</span>
              <span className="window-controls">—　▢　✕</span>
            </div>
            <div className="win-body">
              <div className="schedule-card">
                <div>
                  <span className="tiny-label">NEXT RUN</span>
                  <strong>매일 오전 7:00</strong>
                  <p>컴퓨터 지시 없이 하루 업무 시작</p>
                </div>
                <span className="toggle-on">ON</span>
              </div>
              <div className="flow-list">
                {PHASES.slice(1, 12).map((item, index) => (
                  <div className={`flow-row ${snap.phaseIndex > index + 1 ? "past" : ""}`} key={item}>
                    <span>{String(index + 1).padStart(2, "0")}</span>
                    <b>{item}</b>
                    <i>{snap.phaseIndex === index + 1 ? "●" : snap.phaseIndex > index + 1 ? "✓" : "·"}</i>
                  </div>
                ))}
              </div>
            </div>
          </section>

          <section className="win">
            <div className="win-bar">
              <span>🔗 integrations.link</span>
              <span className="window-controls">—　▢　✕</span>
            </div>
            <div className="win-body integration-list">
              {rows.map((item) =>
                item.href ? (
                  <a key={item.name} href={item.href} target="_blank" rel="noreferrer" className="integration-row">
                    <b>{item.name}</b>
                    <span className={`mini-badge ${item.tone}`}>{item.status}</span>
                  </a>
                ) : (
                  <div key={item.name} className="integration-row">
                    <b>{item.name}</b>
                    <span className={`mini-badge ${item.tone}`}>{item.status}</span>
                  </div>
                ),
              )}
            </div>
          </section>
        </aside>

        <div className="main-stack">
          <section className="win">
            <div className="win-bar">
              <span>🏢 team_office.board</span>
              <span className="window-controls">—　▢　✕</span>
            </div>
            <div className="win-body">
              <div className="section-heading">
                <div>
<p className="eyebrow">DEMO ORGANIZATION</p>
<h2>{CANONICAL_DEPARTMENT_COUNT}개 부서 · {CANONICAL_WORKER_COUNT} Worker 현황</h2>
                </div>
                <div className="filter-tabs" role="group" aria-label="팀 상태 필터">
                  {(["전체", "진행 중", "완료", "승인 대기", "연동 대기"] as const).map((item) => (
                    <button key={item} type="button" aria-pressed={filter === item} className={filter === item ? "active" : ""} onClick={() => setFilter(item)}>
                      {item}
                    </button>
                  ))}
                </div>
              </div>
              <div className="team-grid">
                {filteredTeams.map((team) => (
                  <button className="team-card" key={team.id} onClick={() => onSelect(team.lead.id)}>
                    <span className={`status-dot ${statusClass[team.status]}`} aria-hidden="true" />
                    <span className="mini-pixel">
                      <PixelEmployee hair={team.lead.hair} shirt={team.lead.shirt} accent={team.lead.accent} />
                    </span>
                    <span className="team-copy">
                      <b>
                        {team.lead.name} · {team.name}
                      </b>
                      <small>{team.task}</small>
                    </span>
                    <span className={`status-pill ${statusClass[team.status]}`}>{team.status}</span>
                  </button>
                ))}
              </div>
            </div>
          </section>

          <section className="two-col">
            <section className="win">
              <div className="win-bar">
                <span>✅ ceo.approval</span>
                <span className="window-controls">—　▢　✕</span>
              </div>
              <div className="win-body approval-body">
                <div className="approval-top">
                  <span className="mini-badge yellow">TOP 1 제안</span>
                  <span className="score">92점</span>
                </div>
                <h3>
                  AI 회사가 매일 아침
                  <br />
                  나 대신 출근한다면?
                </h3>
                <p>지금 만들고 있는 시스템 자체를 날것의 성장기로 공개하는 크리에이터 아이덴티티 콘텐츠예요.</p>
                <button
                  className={`btn approve-button ${snap.approved ? "approved" : ""}`}
                  onClick={onApprove}
                  disabled={!snap.approvalPending}
                >
                  {snap.approved ? "승인 완료 · 제작팀 전달됨" : snap.approvalPending ? "이 콘텐츠 승인하기" : "대기 중인 안건 없음"}
                </button>
              </div>
            </section>

            <section className="win secretary">
              <div className="win-bar">
                <span>📋 kim_secretary.brief</span>
                <span className="window-controls">—　▢　✕</span>
              </div>
              <div className="win-body">
                <p className="brief-date">2026.07.26 · {snap.clock} 현재</p>
                <h3>{snap.dayComplete ? "대표님, 오늘 업무가 정리됐어요." : "대표님, 현재 진행 상황이에요."}</h3>
                <ul>
                  <li>
                    <span className="dot green" />
                    {snap.phase} 진행 중 — 완료 {snap.stats.done}팀
                  </li>
                  <li>
                    <span className={`dot ${snap.approvalPending ? "yellow" : "green"}`} />
                    {snap.approvalPending ? "TOP 1 대표 확인 필요" : "대기 중인 결재 없음"}
                  </li>
                  <li>
                    <span className="dot gray" />
                    외부 서비스 연동 대기
                  </li>
                </ul>
                <div className="decision-box">
                  <span className="tiny-label">대표님이 오늘 결정할 1개</span>
                  <strong>
                    {snap.approvalPending
                      ? "TOP 1 콘텐츠를 제작할지 승인해주세요."
                      : snap.approved
                        ? "결정 완료! 제작팀이 다음 업무를 진행해요."
                        : "아직 올라온 안건이 없어요."}
                  </strong>
                </div>
              </div>
            </section>
          </section>
        </div>
      </section>

      <details className="dashboard-detail-disclosure">
        <summary>
          <span>실행·직원·장부 상세 보기</span>
          <small>실제 runtime 상태, 부서 통신, 공식 Snapshot</small>
        </summary>
        <div className="dashboard-detail-stack">
          <DepartmentRuntimePanel />
          <DepartmentCommunicationPanel />
          <OpsPanel />
        </div>
      </details>

      <section className="win storage">
        <div className="win-bar">
          <span>📦 result_storage</span>
          <span className="window-controls">—　▢　✕</span>
        </div>
        <div className="win-body">
          <div className="section-heading">
            <div>
              <p className="eyebrow">RECENT OUTPUTS</p>
              <h2>결과물 창고</h2>
            </div>
            {STORAGE_LINK ? (
              <a className="btn btn-small" href={STORAGE_LINK} target="_blank" rel="noreferrer">
                보관함 열기
              </a>
            ) : null}
          </div>
          <div className="result-table">
            <div className="result-row header">
              <span>결과물</span>
              <span>담당팀</span>
              <span>상태</span>
              <span>바로가기</span>
            </div>
            <div className="result-row">
              <b>이번 주 콘텐츠 캘린더 정리</b>
              <span>기획 1팀</span>
              <span className="status-pill done">최종 완료</span>
              <span>—</span>
            </div>
            <div className="result-row">
              <b>브랜드 템플릿 세팅</b>
              <span>이미지 제작팀</span>
              <span className="status-pill done">최종 완료</span>
              <span>—</span>
            </div>
          </div>
        </div>
      </section>

      <p className="dash-note">
        대표 {CEO.name}({CEO.callsign}) · {CANONICAL_DEPARTMENT_COUNT}개 Hermes 부서 · {CANONICAL_WORKER_COUNT}개 독립 LangGraph Worker · 화면은
        DEMO Projection입니다.
      </p>
    </>
  );
}

function OperationsConsoleView({
  snapshot,
  onBack,
  onOpenMandate,
}: {
  snapshot: ReturnType<typeof useBffFeed>["snapshot"];
  onBack: () => void;
  onOpenMandate: () => void;
}) {
  const { refresh: refreshBff } = useBffFeed();
  const [recoveryBusy, setRecoveryBusy] = useState(false);
  const [recoveryMessage, setRecoveryMessage] = useState("");
  const [departmentFilter, setDepartmentFilter] = useState<"attention" | "running" | "all">("attention");
  const [departmentQuery, setDepartmentQuery] = useState("");
  const traceDisclosureRef = useRef<HTMLDetailsElement>(null);
  const operations = snapshot?.operations;
  const runtime = operations?.runtime;
  const result = runtime?.result as Record<string, unknown> | null | undefined;
  const riskGate = (result?.risk_gate as Record<string, unknown> | undefined) ?? {};
  const qaGate = (result?.qa_gate as Record<string, unknown> | undefined) ?? {};
  const departmentRows = useMemo(() => operations?.departments ?? [], [operations?.departments]);
  const activeWorkers = runtime?.active_workers ?? [];
  const observedAgents = operations?.agent_statuses ?? [];
  const performanceMetrics = runtime?.performance_metrics ?? [];
  const groupedMessages = useMemo(() => groupRuntimeMessages(runtime?.messages ?? []), [runtime?.messages]);
  const pitReadiness = readPitReadiness(runtime);
  const pitBlocked = pitReadiness?.quality_status
    ? pitReadiness.quality_status !== "PASS"
    : runtime?.messages.some((message) => message.text.includes("PIT 입력이 준비되지 않아")) ?? false;
  const attentionDepartments = useMemo(
    () => departmentRows.filter((department) => ["ERROR", "BLOCKED", "DEGRADED", "WAITING_APPROVAL"].includes(department.status)),
    [departmentRows],
  );
  const visibleDepartments = useMemo(() => {
    const query = departmentQuery.trim().toLowerCase();
    const hasAttention = attentionDepartments.length > 0;
    const effectiveFilter = departmentFilter === "attention" && !hasAttention ? "all" : departmentFilter;
    return departmentRows.filter((department) => {
      const matchesFilter = effectiveFilter === "all"
        || effectiveFilter === "attention" && attentionDepartments.some((item) => item.department_code === department.department_code)
        || effectiveFilter === "running" && (department.active_worker_count > 0 || ["RUNNING", "QUEUED"].includes(department.status));
      const haystack = `${department.name} ${department.department_code}`.toLowerCase();
      return matchesFilter && (!query || haystack.includes(query));
    });
  }, [attentionDepartments, departmentFilter, departmentQuery, departmentRows]);
  const tone = (status: string | undefined) => {
    const value = String(status ?? "OFFLINE").toUpperCase().replace(/\s+/g, "_");
    if (["RUNNING", "CONNECTED", "COMPLETED", "PASS", "APPROVE"].includes(value)) return "done";
    if (["DEGRADED", "WAITING_APPROVAL", "PENDING", "WARN"].includes(value)) return "approval";
    if (["ERROR", "BLOCKED", "FAIL", "REJECT"].includes(value)) return "blocked";
    return "waiting";
  };

  async function restartAnalysis() {
    setRecoveryBusy(true);
    setRecoveryMessage("");
    try {
      await startSavedPortfolioRecommendation();
      await refreshBff();
      setRecoveryMessage("저장된 Mandate로 분석을 다시 요청했습니다. 실행 단계에서 상태를 확인하세요.");
    } catch (cause) {
      setRecoveryMessage(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setRecoveryBusy(false);
    }
  }

  function focusDepartment(department: { department_code: string }) {
    setDepartmentFilter("all");
    setDepartmentQuery(department.department_code);
    traceDisclosureRef.current?.setAttribute("open", "");
    window.requestAnimationFrame(() => document.getElementById("ops-department-search")?.focus());
  }

  return (
    <>
      <header className="win operations-hero">
        <div className="win-bar">
          <span>🛰 operations.console · execution observability</span>
          <span className="window-controls" aria-hidden="true">— ▢ ✕</span>
        </div>
        <div className="operations-hero-body">
          <div>
            <p className="eyebrow">FOR OPERATORS · LIVE READ MODEL</p>
            <h1>실행의 모든 흔적을 <em className="highlight">한 화면에</em></h1>
            <p>부서 handoff, Worker, Gate, 이벤트 순서를 확인합니다. 이 화면은 금융 상태를 직접 변경하지 않습니다.</p>
          </div>
          <button type="button" className="btn btn-ghost" onClick={onBack}>← 대표 Dashboard</button>
        </div>
      </header>

      <div className="ops-glossary" role="note" aria-label="운영 화면 용어 안내">
        <span><b>PIT</b> 특정 시점으로 고정한 데이터</span>
        <span><b>Registry</b> 등록된 직원 목록</span>
        <span><b>Live event</b> 실제 실행 중 수신한 메시지</span>
      </div>

      {attentionDepartments.length > 0 ? (
        <section className="ops-attention-strip" aria-label="주의가 필요한 부서">
          <strong>주의 부서 {attentionDepartments.length}개</strong>
          <div>
            {attentionDepartments.map((department) => (
              <button type="button" key={department.department_code} onClick={() => focusDepartment(department)}>
                {department.name} · {readableRuntimeStatus(department.status)}
              </button>
            ))}
          </div>
        </section>
      ) : null}

      <section className="ops-health-grid" aria-label="운영 연결 상태">
        {[
          ["BFF", operations?.status ?? "OFFLINE", operations?.runtime_connected ? "runtime projection 수신" : "snapshot 대기"],
          ["Event bridge", operations?.event_bridge_connected ? "CONNECTED" : "DEGRADED", `sequence ${operations?.sequence ?? 0}`],
          ["LangGraph run", runtime?.status ?? "OFFLINE", runtime?.run_id ? `run ${runtime.run_id.slice(0, 10)}…` : "실행 대기"],
          ["Paper Order", "USER APPROVAL", "백엔드 제출 API 연결 전까지 생성하지 않음"],
        ].map(([label, status, detail]) => (
          <article className="ops-health-card" key={label}>
            <span className="tiny-label">{label}</span>
            <strong className={`status-pill ${tone(status)}`}>{readableRuntimeStatus(status)}</strong>
            <p>{detail}</p>
          </article>
        ))}
      </section>

      <section className="win operations-run-panel" aria-labelledby="operations-run-title">
        <div className="win-bar">
          <span>🧭 run.timeline · trace projection</span>
          <span className="window-controls" aria-hidden="true">— ▢ ✕</span>
        </div>
        <div className="win-body">
          <div className="section-heading">
            <div>
              <p className="eyebrow">RUN / TRACE / GATE</p>
              <h2 id="operations-run-title">현재 실행과 다음 안전 경계</h2>
            </div>
            <span className={`status-pill ${tone(runtime?.status)}`}>{readableRuntimeStatus(runtime?.status)}</span>
          </div>

          <div className="ops-run-meta">
            <span><b>Phase</b> {runtime?.phase ?? "실행 없음"}</span>
            <span><b>Active workers</b> {activeWorkers.length}</span>
            <span><b>Messages</b> {runtime?.messages.length ?? 0}</span>
            <span><b>최근 이벤트</b> {readableRuntimeKind(runtime?.messages.at(-1)?.kind ?? "—")}</span>
            <span><b>LangSmith</b> {readableRuntimeStatus(runtime?.observability?.langsmith?.status)}</span>
          </div>
          {runtime?.error ? (
            <div className="ops-runtime-error" role="alert">
              <strong>실행을 완료하지 못했습니다.</strong>
              <p>Worker 실행을 확인하고, 연결 또는 입력 상태를 점검한 뒤 다시 시도하세요.</p>
              <details>
                <summary>기술 상세 보기</summary>
                <code>{runtime.error}</code>
              </details>
            </div>
          ) : null}

          {pitBlocked ? (
            <section className="ops-recovery-panel" aria-labelledby="ops-recovery-title" role="status">
              <div>
                <span className="tiny-label">PIT RECOVERY</span>
                <h3 id="ops-recovery-title">분석 입력이 준비되지 않아 실행을 보류했습니다.</h3>
                <p>{pitReadiness?.reasons?.length ? pitReadiness.reasons.slice(0, 2).map(readablePitReason).join(" · ") : "시점 고정 데이터 상태를 다시 확인하세요."}</p>
                <small className="ops-recovery-hint">PIT는 분석 기준 시점에 맞춰 고정한 데이터입니다.</small>
              </div>
              <div className="ops-recovery-actions">
                <button type="button" className="btn btn-ghost" onClick={() => void refreshBff()} disabled={recoveryBusy}>데이터 새로고침</button>
                <button type="button" className="btn btn-ghost" onClick={onOpenMandate}>Mandate 설정 확인</button>
                <button type="button" className="btn btn-primary" onClick={() => void restartAnalysis()} disabled={recoveryBusy || runtime?.status === "RUNNING" || runtime?.status === "QUEUED"}>
                  {recoveryBusy ? "재실행 요청 중…" : "분석 다시 시작"}
                </button>
              </div>
              {recoveryMessage ? <p className="ops-recovery-feedback" aria-live="polite">{recoveryMessage}</p> : null}
            </section>
          ) : null}

          <section className="ops-observability-panel" aria-labelledby="ops-observability-title">
            <div className="communication-toolbar">
              <div>
                <p className="eyebrow">LANGSMITH · REDACTED OBSERVABILITY</p>
                <h3 id="ops-observability-title">LLM 성과 추적 <span>{performanceMetrics.length}개 metric</span></h3>
              </div>
              <span className={`status-pill ${tone(runtime?.observability?.langsmith?.status)}`}>
                {readableRuntimeStatus(runtime?.observability?.langsmith?.status)}
              </span>
            </div>
            <div className="ops-observability-grid">
              <article>
                <span>Input</span>
                <strong>원문 비활성화</strong>
                <small>LangSmith에 입력 텍스트를 보내지 않습니다.</small>
              </article>
              <article>
                <span>Output</span>
                <strong>원문 비활성화</strong>
                <small>출력 텍스트 대신 상태·해시·계약 검증만 추적합니다.</small>
              </article>
              <article>
                <span>Metadata</span>
                <strong>정량 지표만</strong>
                <small>model, role, latency, tokens, eval score</small>
              </article>
            </div>
            {performanceMetrics.length > 0 ? (
              <div className="llm-metric-list" aria-label="LangSmith 정량 성과 목록">
                {performanceMetrics.slice(-8).reverse().map((metric: LlmPerformanceMetric) => (
                  <div className="llm-metric-row" key={`${metric.worker_id}-${metric.stage}-${metric.latency_ms}-${metric.attempts}`}>
                    <b>{metric.worker_id}</b>
                    <span>{metric.model_name}</span>
                    <span>{metric.stage}</span>
                    <span>{metric.latency_ms}ms</span>
                    <span>eval {metric.eval_score == null ? "—" : metric.eval_score.toFixed(2)}</span>
                  </div>
                ))}
              </div>
            ) : (
              <p className="dash-note">Worker가 실행되면 여기와 LangSmith에 동일한 정량 metric이 표시됩니다. 현재는 실행 전이거나 PIT 안전 보류 상태입니다.</p>
            )}
          </section>

          <details ref={traceDisclosureRef} className="ops-detail-disclosure ops-trace-disclosure">
            <summary>
              <span>실행 단계·직원 추적·최근 이벤트</span>
              <small>{visibleDepartments.length}/{departmentRows.length}개 부서 · {observedAgents.length}명 관찰 · {groupedMessages.length}개 요약</small>
            </summary>
            <div className="ops-department-tools" aria-label="부서 실행 필터">
              <div className="filter-tabs" role="group" aria-label="부서 상태 필터">
                {([[
                  "attention", `오류·보류 ${attentionDepartments.length}`,
                ], ["running", "업무 중"], ["all", "전체 부서"]] as const).map(([value, label]) => (
                  <button type="button" key={value} aria-pressed={departmentFilter === value} className={departmentFilter === value ? "active" : ""} onClick={() => setDepartmentFilter(value)}>
                    {label}
                  </button>
                ))}
              </div>
              <label className="ops-department-search">
                <span className="sr-only">부서 검색</span>
                <input id="ops-department-search" value={departmentQuery} onChange={(event) => setDepartmentQuery(event.target.value)} placeholder="부서명 또는 코드 검색" />
              </label>
            </div>
            <div className="ops-trace-stack">
              <div className="ops-stage-list" aria-label="부서별 실행 단계">
                {visibleDepartments.map((department) => (
                  <article className="ops-stage-row" key={department.department_code}>
                    <div>
                      <strong>{department.name}</strong>
                      <code>{department.department_code}</code>
                    </div>
                    <span className={`status-pill ${tone(department.status)}`}>{readableRuntimeStatus(department.status)}</span>
                    <span>{department.active_worker_count}/{department.worker_count}명 업무 중</span>
                    <p>{department.current_stage ? `${department.current_stage} · ` : ""}{readableRuntimeMessage(department.status_reason).summary}</p>
                  </article>
                ))}
                {visibleDepartments.length === 0 && <p className="backend-empty-state">조건에 맞는 부서가 없습니다. 필터나 검색어를 바꿔보세요.</p>}
              </div>

              <div className="ops-worker-trace" aria-labelledby="ops-worker-trace-title">
                <div className="communication-toolbar">
                  <div>
                    <p className="eyebrow">EMPLOYEE TRACE · agent.status.v1</p>
                    <h3 id="ops-worker-trace-title">부서원 실행 상태 <span>{observedAgents.length}명 관찰됨</span></h3>
                  </div>
                  <span className="status-pill working">
                    {observedAgents.filter((agent) => agent.status === "RUNNING").length}명 업무 중
                  </span>
                </div>
                {observedAgents.length > 0 ? (
                  <div className="ops-worker-grid" aria-label="직원별 실행 상태">
                    {observedAgents.map((agent) => (
                      <article className="ops-worker-row" key={agent.agent_id}>
                        <div>
                          <strong>{agent.role || agent.worker_id || agent.agent_id}</strong>
                          <small>{agent.department_code} · {agent.worker_id || agent.agent_id}</small>
                        </div>
                        <span className={`status-pill ${tone(agent.status)}`}>{readableRuntimeStatus(agent.status)}</span>
                        <p>{agent.reason || "최근 Worker 상태 이벤트를 수신했습니다."}</p>
                      </article>
                    ))}
                  </div>
                ) : (
                  <p className="backend-empty-state">실제 Worker status event가 아직 없어 직원 상태를 추측하지 않습니다.</p>
                )}
              </div>

              {groupedMessages.length ? (
                <div className="ops-event-list" aria-label="최근 runtime 이벤트">
                  <span className="tiny-label">RECENT RUNTIME MESSAGES</span>
                  {groupedMessages.map((message) => {
                    const readable = readableRuntimeMessage(message.text);
                    return <div key={`${message.kind}-${message.department_code}-${message.id}`}>
                      <time>{new Date(message.occurred_at).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time>
                      <small>{message.department_code ?? "runtime"}</small>
                      <b title={message.kind}>{readableRuntimeKind(message.kind)}{message.count > 1 ? ` · ${message.count}회` : ""}</b>
                      <span>{readable.summary}{readable.action ? ` · 다음: ${readable.action}` : ""}</span>
                    </div>;
                  })}
                </div>
              ) : null}
            </div>
          </details>

          <div className="ops-gate-grid" aria-label="Risk QA 사용자 승인 Gate">
            {([
              ["Risk Gate", String(riskGate.verdict ?? riskGate.status ?? "PENDING"), String(riskGate.reason ?? "검토 대기")],
              ["QA Gate", String(qaGate.decision ?? qaGate.status ?? "PENDING"), String(qaGate.reason ?? "검토 대기")],
              ["대표 승인", String(runtime?.approval?.status ?? "PENDING"), String(runtime?.approval?.comment ?? "명시적 승인 필요")],
            ] as Array<[string, string, string]>).map(([label, status, detail]) => (
              <article key={label}>
                <span>{label}</span>
                <strong className={`status-pill ${tone(status)}`}>{readableRuntimeStatus(status)}</strong>
                <small>{detail}</small>
              </article>
            ))}
          </div>

          <div className="ops-paper-note">
            <strong>Paper 제출 경계</strong>
            <p>Risk·QA 통과와 대표의 명시적 승인이 모두 확인되기 전에는 프론트에서 Paper Order를 만들지 않습니다. 현재 화면은 관찰 전용입니다.</p>
          </div>

        </div>
      </section>

      <details className="ops-detail-disclosure">
        <summary>
          <span>부서원·부서 통신 상세</span>
          <small>문제가 있는 부서를 선택하면 직원 상태와 내부 메시지를 확인합니다.</small>
        </summary>
        <DepartmentCommunicationPanel compact />
      </details>
      <details className="ops-detail-disclosure">
        <summary>
          <span>주문·체결 공식 Snapshot</span>
          <small>OMS·원장·평가가 확정한 읽기 전용 데이터입니다.</small>
        </summary>
        <OpsPanel compact />
      </details>
    </>
  );
}
