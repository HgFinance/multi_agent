"use client";

import type { Company, Snapshot } from "../game/sim";
import { COMPANY } from "../../company.config";

/**
 * AI Office 히어로 + 실행 컨트롤.
 *
 * 버튼은 전부 기존 엔진 API(start / togglePause / setSpeed / skipToDecision)를
 * 그대로 부른다. 이 컴포넌트는 상태를 만들지 않고 snapshot을 읽어 보여줄 뿐이다.
 */

const SPEEDS = [1, 2, 4] as const;

export default function OfficeControls({
  engine,
  snap,
  onDuty,
  onStart,
}: {
  engine: Company;
  snap: Snapshot;
  onDuty: number;
  onStart: () => void;
}) {
  const counters: { label: string; value: number }[] = [
    { label: "근무", value: onDuty },
    { label: "완료", value: snap.stats.done },
    { label: "진행", value: snap.stats.working },
    { label: "연동대기", value: snap.stats.blocked },
  ];

  return (
    <>
      {/* 우측 레일이 붙으면 좌측이 좁아진다. 텍스트가 줄어들게 두고 시계는 안 밀리게 한다. */}
      <header className="flex justify-between items-start gap-gutter mb-gutter">
        <div className="flex-1 min-w-0">
          <p className="text-label-md font-label-md text-secondary uppercase">
            AI Office · Workers · LangGraph Projection
          </p>
          <h1 className="text-headline-lg font-headline-lg text-primary font-bold tracking-tight mt-2">
            {COMPANY.titlePrefix} {COMPANY.titleAccent}
          </h1>
          <p className="text-body-md font-body-md text-on-surface-variant mt-2 max-w-2xl">
            실제 LangGraph가 실행 중인 Worker만 부서 안에서 작업하고, 부서 간 handoff는 부서장끼리만 진행합니다.
          </p>
        </div>

        <div className="bg-primary text-on-primary rounded-lg px-8 py-4 text-center shrink-0">
          <div className="text-label-md font-label-md text-primary-fixed-dim">SEOUL</div>
          <div className="text-display-lg font-display-lg font-bold font-data-mono leading-none my-1">{snap.clock}</div>
          <div className="text-xs opacity-80">업무시간 09:00~18:00</div>
        </div>
      </header>

      <section
        className="bg-surface-container-low border border-outline-variant rounded-lg p-4 mb-gutter"
        aria-label="오피스 실행 제어"
      >
        <div className="flex items-center gap-3 flex-wrap">
          <button
            type="button"
            onClick={onStart}
            disabled={snap.running}
            className="px-6 py-3 bg-primary text-on-primary rounded font-bold text-body-sm hover:bg-primary-container transition-colors disabled:opacity-45 disabled:hover:bg-primary"
          >
            {snap.running ? "Simulation Running…" : "Office Simulation 시작"}
          </button>

          <button
            type="button"
            onClick={() => engine.togglePause()}
            className="px-6 py-3 border border-outline-variant bg-surface-container-lowest text-on-surface rounded font-bold text-body-sm hover:bg-surface-container transition-colors inline-flex items-center gap-2"
          >
            <span className="material-symbols-outlined text-[18px]" aria-hidden="true">
              {snap.paused ? "play_arrow" : "pause"}
            </span>
            {snap.paused ? "Resume" : "Pause"}
          </button>

          <div
            className="flex items-stretch border border-outline-variant rounded overflow-hidden bg-surface-container-lowest"
            role="group"
            aria-label="재생 속도"
          >
            <span
              className="px-4 flex items-center text-label-md font-label-md text-secondary bg-surface-container-low"
              title="시뮬레이션 전체(걷기·업무·대사)가 함께 빨라집니다. 실제 외부 작업 속도와는 무관합니다."
            >
              Speed
            </span>
            {SPEEDS.map((value) => {
              const on = !snap.turbo && snap.speed === value;
              return (
                <button
                  key={value}
                  type="button"
                  aria-pressed={on}
                  onClick={() => engine.setSpeed(value)}
                  className={`px-4 py-3 border-l border-outline-variant text-body-sm font-bold transition-colors ${
                    on ? "bg-secondary-container text-primary" : "text-secondary hover:bg-surface-container"
                  }`}
                >
                  {value}x
                </button>
              );
            })}
            <button
              type="button"
              onClick={() => engine.skipToDecision()}
              disabled={!snap.running || snap.approvalPending}
              title="대표님이 결정할 일이 생길 때까지 단숨에 건너뜁니다"
              className={`px-4 py-3 border-l border-outline-variant text-body-sm font-bold transition-colors inline-flex items-center gap-1 disabled:opacity-45 ${
                snap.turbo ? "bg-secondary-container text-primary" : "text-secondary hover:bg-surface-container"
              }`}
            >
              <span className="material-symbols-outlined text-[16px]" aria-hidden="true">fast_forward</span>
              {snap.turbo ? "건너뛰는 중…" : "결정까지"}
            </button>
          </div>
        </div>

        <div className="flex items-center gap-6 flex-wrap mt-4">
          <div className="min-w-56 flex-1 max-w-md">
            <div className="flex justify-between items-baseline gap-2 mb-1">
              <span className="text-body-sm font-body-sm text-on-surface-variant">{snap.phase}</span>
              <span className="text-body-sm font-body-sm font-data-mono font-bold text-on-surface">{snap.progress}%</span>
            </div>
            <div
              className="h-1.5 rounded-full bg-surface-container-highest overflow-hidden"
              role="progressbar"
              aria-valuenow={snap.progress}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-label="하루 진행률"
            >
              <div
                className="h-full bg-primary origin-left transition-transform duration-300"
                style={{ transform: `scaleX(${snap.progress / 100})` }}
              />
            </div>
          </div>

          <div className="flex gap-2 flex-wrap">
            {counters.map((counter) => (
              <span
                key={counter.label}
                className="px-4 py-2 border border-outline-variant rounded bg-surface-container-lowest text-body-sm font-bold text-on-surface"
              >
                {counter.label} <span className="font-data-mono">{counter.value}</span>
              </span>
            ))}
          </div>
        </div>
      </section>
    </>
  );
}
