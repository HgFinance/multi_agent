"use client";

import { useMemo } from "react";
import { COMPANY } from "../../company.config";
import { Company } from "../game/sim";
import { STAFF } from "../game/staff";
import LivePortfolioPanel from "../components/LivePortfolioPanel";
import { CeoControlRoomChat } from "./CeoControlRoomChat";
import { PanelBar } from "./PanelBar";

/**
 * 대표 Dashboard.
 *
 */

/** 결과물 창고 표본. 실제 산출물 저장소가 붙기 전까지의 예시 행이다. */
const RECENT_OUTPUTS = [
  { name: "이번 주 콘텐츠 캘린더 정리", team: "기획 1팀", status: "최종 완료" },
  { name: "브랜드 템플릿 세팅", team: "이미지 제작팀", status: "최종 완료" },
];

export default function DashboardView() {
  // ponytail: 라우트가 달라 AI Office의 엔진과 상태를 공유하지 않는다. 지금은
  // 엔진의 초기 스냅샷(전부 0)을 보여준다. 실행 중 수치를 띄우려면 라우트를
  // 가로지르는 store가 먼저 필요하다.
  const stats = useMemo(() => new Company().snapshot().stats, []);

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

        {/* ── CEO Control Room / 실시간 포트폴리오 ───────── */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-gutter items-start">
          <CeoControlRoomChat />
          <LivePortfolioPanel />
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
