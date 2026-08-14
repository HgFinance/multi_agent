"use client";

import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { COMPANY } from "../../company.config";
import { Company } from "../game/sim";
import { STAFF } from "../game/staff";
import { askCeo, ceoProgress, type CardOutcome, type CeoQueryProgress, type CeoQueryResult } from "../lib/ceoClient";
import { KANBAN_BASE_URL, resolveKanbanUrl } from "../lib/kanbanUrl";

/**
 * 대표 Dashboard.
 *
 * Hermes Kanban은 별도 인증 세션을 사용하는 외부 화면이다. Dashboard는
 * 보드 자체의 API나 인증을 소유하지 않고, Hermes가 제공하는 화면을 임베드한다.
 */

/**
 * 이 페이지의 host. 서버 렌더에는 없으므로 `useSyncExternalStore`의 서버
 * 스냅샷으로 빈 값을 준다 - effect 안에서 setState하면 렌더가 한 번 더 돌고,
 * 렌더 중에 `window`를 읽으면 SSR 결과와 달라져 hydration이 어긋난다.
 * host는 페이지 수명 동안 바뀌지 않으므로 구독할 것이 없다.
 */
const NO_SUBSCRIBE = () => () => {};

function usePageHost(): string {
  return useSyncExternalStore(
    NO_SUBSCRIBE,
    () => window.location.hostname,
    () => "",
  );
}

const QUICK_QUESTIONS = [
  "오늘 전체 업무 현황을 요약해줘",
  "지금 막혀 있는 업무와 이유를 알려줘",
  "리서치팀의 최신 진행 상황을 브리핑해줘",
];

/** 결과물 창고 표본. 실제 산출물 저장소가 붙기 전까지의 예시 행이다. */
const RECENT_OUTPUTS = [
  { name: "이번 주 콘텐츠 캘린더 정리", team: "기획 1팀", status: "최종 완료" },
  { name: "브랜드 템플릿 세팅", team: "이미지 제작팀", status: "최종 완료" },
];

/** 카드 결말별 표시. 보드의 status가 아니라 "답이 됐는가" 기준이다.
 *  특히 NO_ANSWER는 보드에서 done으로 보이는 카드라 성공으로 읽으면 안 된다. */
const OUTCOME_VIEW: Record<CardOutcome, { label: string; tone: string }> = {
  QUEUED: { label: "대기", tone: "border-outline-variant bg-surface-container text-on-surface-variant" },
  RUNNING: { label: "진행 중", tone: "border-primary/30 bg-secondary-container text-primary" },
  ANSWERED: { label: "답변 완료", tone: "border-tertiary-fixed-dim bg-tertiary-fixed/30 text-on-tertiary-fixed-variant" },
  NO_ANSWER: { label: "결과 없음", tone: "border-error/40 bg-error-container text-on-error-container" },
  BLOCKED: { label: "막힘", tone: "border-error/40 bg-error-container text-on-error-container" },
  FAILED: { label: "실패", tone: "border-error/40 bg-error-container text-on-error-container" },
  STALE: { label: "정체", tone: "border-error/40 bg-error-container text-on-error-container" },
  NO_ASSIGNEE: { label: "담당 없음", tone: "border-error/40 bg-error-container text-on-error-container" },
};

function PanelBar({ icon, title, children }: { icon: string; title: string; children?: React.ReactNode }) {
  return (
    <div className="bg-surface-container-low border-b border-outline-variant px-4 py-2.5 flex items-center justify-between gap-2">
      <span className="flex items-center gap-2 text-label-md font-label-md text-on-surface-variant">
        <span className="material-symbols-outlined text-[16px]" aria-hidden="true">{icon}</span>
        {title}
      </span>
      {children}
    </div>
  );
}

export default function DashboardView() {
  // ponytail: 라우트가 달라 AI Office의 엔진과 상태를 공유하지 않는다. 지금은
  // 엔진의 초기 스냅샷(전부 0)을 보여준다. 실행 중 수치를 띄우려면 라우트를
  // 가로지르는 store가 먼저 필요하다.
  const stats = useMemo(() => new Company().snapshot().stats, []);

  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<CeoQueryResult | null>(null);
  const [progress, setProgress] = useState<CeoQueryProgress | null>(null);
  const [kanbanState, setKanbanState] = useState<"loading" | "ready" | "error">("loading");

  const pageHost = usePageHost();
  const kanbanUrl = useMemo(() => resolveKanbanUrl(KANBAN_BASE_URL, pageHost || undefined), [pageHost]);
  // 주소 자체가 잘못된 경우와 못 불러온 경우를 같은 안내로 묶는다.
  const kanbanFailed = !kanbanUrl || kanbanState === "error";

  const rootTaskId = result?.task_id ?? null;

  // 뿌리 카드가 생기면 종료될 때까지 본부별 진행을 따라간다. 처음엔 1초, 한 번
  // 받은 뒤에는 15초 — Task 하나 완료까지 수십 초~분 단위라 그보다 촘촘히
  // 돌 필요가 없다(2026-08-13, 5초에서 늘림).
  useEffect(() => {
    if (!rootTaskId || progress?.all_terminal) return undefined;
    const timer = window.setTimeout(() => {
      ceoProgress(rootTaskId)
        .then(setProgress)
        .catch(() => undefined);
    }, progress ? 15000 : 1000);
    return () => window.clearTimeout(timer);
  }, [rootTaskId, progress]);
  useEffect(() => {
    if (!kanbanUrl || kanbanState !== "loading") return undefined;
    const timer = window.setTimeout(() => setKanbanState("error"), 8000);
    return () => window.clearTimeout(timer);
  }, [kanbanState, kanbanUrl]);

  async function send(text: string) {
    const value = text.trim();
    if (!value || busy) return;
    setDraft("");
    setError("");
    setBusy(true);
    setProgress(null);
    try {
      setResult(await askCeo(value));
    } catch (cause) {
      setResult(null);
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  const metrics = [
    { label: "LangGraph Worker", value: STAFF.length, cap: "WORKERS", lead: true },
    { label: "완료", value: stats.done, cap: "DONE", lead: false },
    { label: "진행 중", value: stats.working, cap: "WORKING", lead: false },
    { label: "대표 확인", value: stats.approval, cap: "APPROVAL", lead: false },
    { label: "연동 대기", value: stats.blocked, cap: "WAITING", lead: false },
  ];

  return (
    <>
      <main className="flex-1 w-full max-w-app mx-auto p-margin-mobile md:p-margin-desktop flex flex-col gap-gutter">
        {/* ── 요약 헤더 — 카드에 올리지 않고 바탕에 그대로 둔다 ── */}
        <section className="flex justify-between items-start gap-gutter flex-wrap">
          <div className="min-w-0">
            <p className="text-label-md font-label-md text-on-surface-variant uppercase">Today Overview</p>
            <h1 className="text-headline-lg font-headline-lg text-primary font-bold tracking-tight mt-2">
              오늘 회사가 어떻게 움직이는지 <span className="bg-secondary-container px-2">한눈에</span> 보여드려요
            </h1>
            <p className="text-body-sm font-body-sm text-on-surface-variant mt-2 max-w-3xl">
              Worker는 context를 만들고, 결정은 권한을 가진 결정론적 Gate와 대표님이 맡아요.
            </p>
          </div>
          <span className="text-label-md font-label-md text-outline shrink-0">
            실제 전송·게시·결제는 대표 승인 후 진행해요
          </span>
        </section>

        {/* ── CEO Control Room / Hermes Kanban ──────────── */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-gutter items-start">
          <section className="lg:col-span-1 bg-surface-container-lowest border border-outline-variant rounded-lg overflow-hidden shadow-sm">
            <PanelBar icon="terminal" title="CEO Control Room">
              <span className="flex items-center gap-1.5 text-xs text-on-surface-variant">
                <span className="w-2 h-2 rounded-full bg-tertiary-fixed-dim" aria-hidden="true" />
                Hermes endpoint
              </span>
            </PanelBar>

            <div className="p-6">
              <div className="flex justify-between items-center gap-2 mb-4">
                <span className="text-label-md font-label-md text-on-surface-variant uppercase">Ask The Office</span>
                <span className="text-xs bg-surface-container px-2 py-1 rounded border border-outline-variant text-on-surface-variant shrink-0">
                  ⌘ /Ctrl ↵ 전송
                </span>
              </div>

              <h2 className="text-headline-md font-headline-md text-primary mb-2">
                무엇을
                <br />
                확인할까요?
              </h2>
              <p className="text-body-sm font-body-sm text-on-surface-variant mb-6">
                자연어로 질문하면 CEO Hermes가 업무를 만들고, 결과는 Kanban에서 추적됩니다.
              </p>

              {/* 자주 쓰는 질의를 먼저 둔다. 아래 입력창은 그 밖의 상세 요청용이다. */}
              <div className="mb-6">
                <span className="block text-label-md font-label-md text-on-surface-variant mb-2">빠른 질문</span>
                <div className="flex flex-col items-stretch gap-2">
                  {QUICK_QUESTIONS.map((question) => (
                    <button
                      key={question}
                      type="button"
                      onClick={() => void send(question)}
                      disabled={busy}
                      className="px-3 py-2 rounded border border-outline-variant bg-surface-container-lowest text-body-sm font-body-sm text-on-surface-variant hover:bg-surface-container hover:border-primary transition-colors disabled:opacity-40 text-left"
                    >
                      {question}
                    </button>
                  ))}
                </div>
              </div>

              <label className="block">
                <span className="block text-label-md font-label-md text-primary mb-2">대표님 질의</span>
                <textarea
                  value={draft}
                  maxLength={2000}
                  rows={4}
                  onChange={(event) => setDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                      event.preventDefault();
                      void send(draft);
                    }
                  }}
                  placeholder="예: 오늘 리스크가 큰 업무만 먼저 브리핑해줘"
                  className="w-full p-3 bg-surface rounded border border-outline-variant focus:border-primary focus:ring-1 focus:ring-primary focus:outline-none text-body-sm font-body-sm resize-none"
                />
              </label>

              <div className="flex justify-between items-center gap-3 mt-2">
                <span className="text-xs text-outline">{draft.length}/2000 · 조회와 자문만 수행합니다</span>
                <button
                  type="button"
                  onClick={() => void send(draft)}
                  disabled={busy || !draft.trim()}
                  className="px-4 py-2 bg-surface-container-high border border-outline-variant text-primary rounded font-bold text-label-md font-label-md hover:bg-surface-container-highest transition-colors disabled:opacity-40 shrink-0"
                >
                  {busy ? "전달 중…" : "CEO에게 전달"}
                </button>
              </div>

              {error ? (
                <p role="alert" className="mt-3 text-xs text-error border border-error-container bg-error-container rounded p-2">
                  ⚠️ {error}
                </p>
              ) : null}

              {result ? (
                <div className="mt-3 border border-outline-variant rounded p-3 bg-surface" aria-live="polite">
                  <div className="flex items-center justify-between gap-2 mb-1">
                    <span className="text-label-md font-label-md text-on-surface-variant uppercase">CEO Hermes 답변</span>
                    <code className="text-[10px] text-outline">{result.task?.task_id ?? "task 대기"}</code>
                  </div>
                  <p className="text-body-sm font-body-sm text-on-surface whitespace-pre-line m-0">{result.answer}</p>
                  {/* v2(ceo.query-accepted.v2)에만 있다. v1 응답이면 통째로 없다. */}
                  {result.planning ? (
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {result.planning.selected_departments.map((dept) => (
                        <span
                          key={dept}
                          className="px-2 py-0.5 rounded-full border border-outline-variant bg-surface-container text-[11px] text-on-surface-variant"
                        >
                          {dept}
                        </span>
                      ))}
                      {result.planning.qa_required ? (
                        <span className="px-2 py-0.5 rounded-full border border-primary/30 bg-secondary-container text-[11px] text-primary">
                          QA 필요
                        </span>
                      ) : null}
                    </div>
                  ) : null}
                  {/* 이 문장은 참고용이다. 수치를 여기서 뽑아 확정하지 않는다. */}
                  <p className="text-[10px] text-outline mt-2 m-0">비공식 · 확정 수치의 출처가 아닙니다</p>
                </div>
              ) : null}

              {progress?.final_answer ? (
                <div className="mt-3 border-2 border-primary/40 rounded p-3 bg-secondary-container/30" aria-live="polite">
                  <div className="flex items-center justify-between gap-2 mb-1">
                    <span className="text-label-md font-label-md text-primary uppercase">CEO 최종 답변</span>
                    {!progress.answer_grounded ? (
                      <span className="text-[10px] text-error">⚠️ 근거 미확인</span>
                    ) : null}
                  </div>
                  <p className="text-body-sm font-body-sm text-on-surface whitespace-pre-line m-0">
                    {progress.final_answer}
                  </p>
                </div>
              ) : null}

              {progress ? (
                <div className="mt-3 border border-outline-variant rounded p-3" aria-live="polite">
                  <div className="flex items-center justify-between gap-2 mb-2">
                    <span className="text-label-md font-label-md text-on-surface-variant uppercase">본부별 진행</span>
                    <span className="text-xs font-data-mono text-on-surface-variant">
                      {progress.finished}/{progress.total} 종료{progress.all_terminal ? "" : " · 확인 중"}
                    </span>
                  </div>
                  <ul className="m-0 p-0 list-none flex flex-col gap-1.5">
                    {progress.cards
                      .filter((card) => !card.is_root)
                      .map((card) => {
                        const view = OUTCOME_VIEW[card.outcome];
                        return (
                          <li key={card.task_id} className="text-xs">
                            <span className={`inline-block px-2 py-0.5 rounded-full border mr-2 ${view.tone}`}>{view.label}</span>
                            <span className="text-on-surface">{card.department}</span>
                            {card.summary ? (
                              <span className="block text-on-surface-variant mt-0.5 ml-1">{card.summary}</span>
                            ) : null}
                          </li>
                        );
                      })}
                  </ul>
                  {progress.unusable.length > 0 ? (
                    <p className="text-xs text-error mt-2 m-0">
                      ⚠️ {progress.unusable.length}개 본부가 사용 가능한 결과를 내지 못했습니다.
                    </p>
                  ) : null}
                  {progress.all_terminal && !progress.answer_grounded ? (
                    <p className="text-xs text-error mt-1 m-0">
                      ⚠️ 근거가 확인되지 않은 답변입니다. 그대로 결정에 쓰지 마세요.
                    </p>
                  ) : null}
                </div>
              ) : null}

            </div>
          </section>

          <section className="lg:col-span-2 bg-surface-container-lowest border border-outline-variant rounded-lg overflow-hidden shadow-sm flex flex-col">
            <PanelBar icon="dashboard" title="Hermes Kanban Dashboard">
              <span className="flex gap-1.5" aria-hidden="true">
                <span className="w-2.5 h-2.5 rounded-full bg-outline-variant" />
                <span className="w-2.5 h-2.5 rounded-full bg-outline-variant" />
                <span className="w-2.5 h-2.5 rounded-full bg-outline-variant" />
              </span>
            </PanelBar>

            <div className="p-6 pb-4 flex justify-between items-start gap-4 flex-wrap">
              <div className="min-w-0">
                <div className="flex items-center gap-2 mb-2 flex-wrap">
                  <span className="bg-primary text-on-primary px-2 py-1 rounded text-label-md font-label-md">SOURCE OF TRUTH</span>
                  <span className="flex items-center gap-1.5 text-xs text-on-surface-variant">
                    <span className="w-2 h-2 rounded-full bg-tertiary-fixed-dim" aria-hidden="true" />
                    Hermes
                  </span>
                </div>
                <h2 className="text-headline-md font-headline-md text-primary">공용 Task Graph / Kanban</h2>
                <p className="text-body-sm font-body-sm text-on-surface-variant mt-1">
                  사용자 질의와 부서별 업무 배정은 이 보드의 상태를 기준으로 확인합니다.
                </p>
              </div>
              {kanbanUrl ? (
                <a
                  href={kanbanUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="px-4 py-2 border border-outline-variant bg-surface-container-lowest rounded font-bold text-label-md font-label-md text-primary hover:bg-surface-container transition-colors inline-flex items-center gap-1 shrink-0"
                >
                  보드 새 창으로 열기
                  <span className="material-symbols-outlined text-[16px]" aria-hidden="true">open_in_new</span>
                </a>
              ) : null}
            </div>
            <div className="mx-6 mb-6 flex-1 min-h-80 bg-surface-container-low border border-outline-variant rounded relative overflow-auto">
              {kanbanUrl ? (
                <iframe
                  title="Hermes Kanban 화면"
                  src={kanbanUrl}
                  onLoad={() => setKanbanState("ready")}
                  onError={() => setKanbanState("error")}
                  /*
                    로그인 폼 제출과 세션 쿠키가 필요하다. sandbox를 걸면
                    allow-same-origin 없이는 쿠키가 통째로 막혀 로그인이
                    불가능해지므로 걸지 않는다 - 어차피 같은 host의 우리 로컬
                    Hermes다. 외부 호스트를 임베드하게 되면 그때 재검토한다.
                  */
                  className="w-full h-[560px] border-0 bg-white"
                />
              ) : null}
              <div className="absolute top-3 right-3 rounded border border-outline-variant bg-surface-container-lowest/95 px-2 py-1 text-xs text-on-surface-variant">
                {/*
                  cross-origin iframe 안은 들여다볼 수 없다. onLoad는 로그인
                  화면이 떠도 똑같이 불린다 - "보드 연결됨"이라고 쓰면 로그인
                  화면을 성공으로 표시하는 셈이라 "화면 표시됨"까지만 쓴다.
                */}
                {kanbanFailed ? "보드를 불러오지 못함" : kanbanState === "loading" ? "보드 불러오는 중…" : "Hermes 화면 표시됨"}
              </div>
              {kanbanFailed ? (
                <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-surface-container-low p-6 text-center">
                  <span className="material-symbols-outlined text-[40px] text-outline-variant" aria-hidden="true">account_tree</span>
                  <p className="text-body-sm font-body-sm text-on-surface-variant m-0 max-w-lg">
                    {kanbanUrl
                      ? "Hermes 보드를 불러오지 못했습니다. 새 창으로 열어 인증 상태와 Hermes 실행 여부를 확인하세요."
                      : "Hermes Kanban 주소 설정이 올바르지 않습니다. 관리자 설정을 확인하세요."}
                  </p>
                  {kanbanUrl ? <code className="text-xs text-outline bg-surface-container px-2 py-1 rounded">{kanbanUrl}</code> : null}
                </div>
              ) : null}
            </div>
            {kanbanUrl && pageHost && new URL(kanbanUrl).hostname !== pageHost ? (
              /*
                host가 다르면 Hermes의 SameSite=Lax 세션 쿠키가 iframe에 남지
                않아 로그인이 끝없이 반복된다. 조용히 실패하게 두지 않고 어떻게
                고치는지 적는다.
              */
              <p role="status" className="mx-6 mb-6 -mt-4 text-xs text-error">
                이 페이지({pageHost})와 보드({new URL(kanbanUrl).hostname})의 호스트가 달라 iframe 안에서 로그인 세션이 유지되지 않습니다.
                주소창을 <code className="bg-surface-container px-1 rounded">{new URL(kanbanUrl).hostname}</code>으로 맞춰 접속하거나,
                보드를 새 창으로 여세요.
              </p>
            ) : null}

          </section>
        </div>

        {/* ── 오늘 업무 요약 ────────────────────────────── */}
        <section className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-4" aria-label="오늘 업무 요약">
          {metrics.map((metric) => (
            <article
              key={metric.label}
              className={`rounded-lg border border-outline-variant p-4 h-24 flex flex-col justify-between ${
                metric.lead ? "bg-secondary-container" : "bg-surface-container-lowest"
              }`}
            >
              <span className={`text-label-md font-label-md ${metric.lead ? "text-on-secondary-container" : "text-secondary"}`}>
                {metric.label}
              </span>
              <span className="flex justify-between items-end gap-2">
                <b
                  className={`text-headline-lg font-headline-lg font-data-mono ${metric.lead ? "text-primary" : "text-secondary"}`}
                >
                  {metric.value}
                </b>
                <small
                  className={`text-[10px] font-bold uppercase tracking-widest ${
                    metric.lead ? "text-on-secondary-container" : "text-secondary"
                  }`}
                >
                  {metric.cap}
                </small>
              </span>
            </article>
          ))}
        </section>

        {/* ── 결과물 창고 ───────────────────────────────── */}
        <section className="bg-surface-container-lowest border border-outline-variant rounded-lg overflow-hidden shadow-sm">
          <PanelBar icon="inventory_2" title="result_storage" />
          <div className="p-6">
            <span className="block text-label-md font-label-md text-on-surface-variant uppercase mb-1">Recent Outputs</span>
            <h2 className="text-headline-md font-headline-md text-primary mb-4">결과물 창고</h2>
            <div className="overflow-x-auto border border-outline-variant rounded">
              <table className="w-full text-left text-body-sm font-body-sm">
                <thead className="bg-surface-container-low border-b border-outline-variant text-label-md font-label-md text-secondary uppercase">
                  <tr>
                    <th className="p-4 font-semibold">결과물</th>
                    <th className="p-4 font-semibold">담당팀</th>
                    <th className="p-4 font-semibold">상태</th>
                    <th className="p-4 font-semibold text-right">바로가기</th>
                  </tr>
                </thead>
                <tbody>
                  {RECENT_OUTPUTS.map((row) => (
                    <tr key={row.name} className="border-b border-outline-variant last:border-b-0 hover:bg-surface transition-colors">
                      <td className="p-4 text-on-surface">{row.name}</td>
                      <td className="p-4 text-on-surface-variant">{row.team}</td>
                      <td className="p-4">
                        <span className="bg-surface-container-highest px-3 py-1 rounded text-xs border border-outline-variant">
                          {row.status}
                        </span>
                      </td>
                      <td className="p-4 text-right text-outline">—</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      </main>

      <footer className="border-t border-outline-variant bg-surface-container-lowest w-full">
        <div className="max-w-app mx-auto px-margin-mobile md:px-margin-desktop py-4 flex justify-between items-center gap-4 flex-wrap text-label-md font-label-md">
          <b className="text-primary">{COMPANY.name}</b>
          <span className="text-on-surface-variant">
            © {new Date().getFullYear()} {COMPANY.name}. Operational Intelligence Layer.
          </span>
          {/* 아직 화면이 없어 링크로 만들지 않는다 */}
          <span className="flex gap-4 text-on-surface-variant">
            <span>Privacy Policy</span>
            <span>Compliance</span>
            <span>API Docs</span>
          </span>
        </div>
      </footer>
    </>
  );
}
