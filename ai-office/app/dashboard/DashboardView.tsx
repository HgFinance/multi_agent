"use client";

import { useMemo, useState } from "react";
import { COMPANY } from "../../company.config";
import { Company } from "../game/sim";
import { STAFF } from "../game/staff";
import { askCeo, type CeoQueryResult } from "../lib/ceoClient";

/**
 * 대표 Dashboard.
 *
 * Kanban 패널은 빈 상태만 그린다 — 보드 임베드는 동규님 담당이라 여기서
 * 만들지 않는다. 삭제 전 구현(HermesKanbanEmbed)은 커밋 49bc724에 남아 있다.
 */

/** Hermes Kanban 보드 주소. 임베드를 붙일 때 이 값을 같이 쓴다. */
const KANBAN_URL = process.env.NEXT_PUBLIC_HERMES_KANBAN_URL?.trim() || "http://127.0.0.1:9119";

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

  async function send(text: string) {
    const value = text.trim();
    if (!value || busy) return;
    setDraft("");
    setError("");
    setBusy(true);
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
        {/* ── 요약 헤더 ─────────────────────────────────── */}
        <section className="bg-surface-container-lowest border border-outline-variant rounded-lg p-6 flex justify-between items-start gap-gutter flex-wrap">
          <div className="min-w-0">
            <p className="text-label-md font-label-md text-on-surface-variant uppercase">Today</p>
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
                  {/* 이 문장은 참고용이다. 수치를 여기서 뽑아 확정하지 않는다. */}
                  <p className="text-[10px] text-outline mt-2 m-0">비공식 · 확정 수치의 출처가 아닙니다</p>
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
              <a
                href={KANBAN_URL}
                target="_blank"
                rel="noreferrer"
                className="px-4 py-2 border border-outline-variant bg-surface-container-lowest rounded font-bold text-label-md font-label-md text-primary hover:bg-surface-container transition-colors inline-flex items-center gap-1 shrink-0"
              >
                보드 새 창으로 열기
                <span className="material-symbols-outlined text-[16px]" aria-hidden="true">open_in_new</span>
              </a>
            </div>

            {/* 보드 임베드는 동규님 담당. 여기서는 빈 상태만 그린다. */}
            <div className="mx-6 mb-6 flex-1 min-h-80 bg-surface-container-low border border-outline-variant rounded flex flex-col items-center justify-center gap-3 p-6 text-center">
              <span className="material-symbols-outlined text-[40px] text-outline-variant" aria-hidden="true">account_tree</span>
              <p className="text-body-sm font-body-sm text-on-surface-variant m-0 max-w-lg">
                Dashboard Profile 인증이 필요합니다. 보드가 보이지 않으면 새 창으로 열어 Hermes 인증 상태를 확인하세요.
              </p>
              <code className="text-xs text-outline bg-surface-container px-2 py-1 rounded">{KANBAN_URL}</code>
            </div>
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
