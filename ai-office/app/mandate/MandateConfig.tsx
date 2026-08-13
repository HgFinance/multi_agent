"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { MandateSubmissionError, submitMandateDraft } from "../lib/mandateClient";

/**
 * 운용 지침 설정 화면.
 *
 * 제출("지침 제출 및 검토")은 `../lib/mandateClient.ts`를 거쳐
 * `POST /ui/mandates`(최초 1회, 없을 때만) + `POST /ui/mandates/{id}/change-requests`
 * (항상)로 BFF에 전달된다. 이 화면 자체가 주문·원장·한도를 확정하지 않는다 -
 * Risk/QA 검토(LOOSEN)나 즉시 적용(TIGHTEN/NEUTRAL) 여부는 서버가 정한다.
 *
 * "임시 저장"은 여전히 브라우저(localStorage)에만 남는다 - 완성 전 초안까지
 * Mandate Version으로 만들면 `content_hash` 중복·승인 흐름이 매번 발동한다
 * (USER_INPUT_API_SPEC §2.4와 같은 이유).
 */

export type RiskProfile = "conservative" | "neutral" | "aggressive";
export type ApprovalMode = "auto" | "manual";
export type AssetClassId = "equity" | "etf" | "leverage" | "futures" | "options" | "derivatives" | "crypto";

export interface MandateDraft {
  objective: string;
  riskProfile: RiskProfile;
  baseCapital: number;
  currency: string;
  maxSingleWeightPct: number;
  grossExposurePct: number;
  /**
   * 2026-08-12 추가. `governance.mandate_versions.risk_bounds.max_drawdown_pct`의
   * 필수값이라, 슬라이더가 없으면 제출 자체가 서버에서 422로 거부된다
   * (USER_INPUT_SPEC.md §2 6번 "전체 최대 손실" — 프리셋이 아니라 직접 선택 항목).
   */
  maxDrawdownPct: number;
  /** 위와 같은 이유. §2 7번 "일일 최대 손실". `<= maxDrawdownPct`여야 한다. */
  maxDailyLossPct: number;
  allowedAssets: Record<AssetClassId, boolean>;
  approvalMode: ApprovalMode;
}

const RISK_PROFILES: { id: RiskProfile; icon: string; label: string; note: string; tone: string }[] = [
  { id: "conservative", icon: "shield", label: "보수적", note: "원금 보존 최우선", tone: "text-primary" },
  { id: "neutral", icon: "balance", label: "중립적", note: "성장과 위험의 균형", tone: "text-secondary" },
  { id: "aggressive", icon: "rocket_launch", label: "공격적", note: "높은 수익 추구\n위험 감수", tone: "text-error" },
];

const ASSET_CLASSES: { id: AssetClassId; icon: string; label: string }[] = [
  { id: "equity", icon: "show_chart", label: "주식" },
  { id: "etf", icon: "pie_chart", label: "ETF" },
  { id: "leverage", icon: "trending_up", label: "레버리지" },
  { id: "futures", icon: "timeline", label: "선물" },
  { id: "options", icon: "donut_large", label: "옵션" },
  { id: "derivatives", icon: "hub", label: "파생상품" },
  { id: "crypto", icon: "currency_bitcoin", label: "가상자산" },
];

const APPROVAL_MODES: { id: ApprovalMode; icon: string; label: string; note: string }[] = [
  { id: "auto", icon: "bolt", label: "Auto-Order Execution", note: "조건 충족 시 주문이 자동으로 실행됩니다." },
  { id: "manual", icon: "pan_tool", label: "Manual Approval Required", note: "모든 제안된 주문은 실행 전 관리자의 승인이 필요합니다." },
];

const DEFAULT_DRAFT: MandateDraft = {
  objective: "",
  riskProfile: "conservative",
  baseCapital: 100_000_000,
  currency: "USD",
  // 2026-08-12: 기본 성향(conservative)의 프리셋 상한(max_sector_weight=25%)보다
  // 낮아야 한다(mandatePresets.ts LOW 등급) - 그래야 값을 하나도 안 건드리고
  // 바로 제출해도 `max_instrument_weight <= max_sector_weight` 제약을 지킨다.
  maxSingleWeightPct: 20,
  grossExposurePct: 200,
  // USER_INPUT_API_SPEC.md 예시값과 같은 기본선(20%/3%). risk_bounds 계약상
  // maxDailyLossPct는 항상 maxDrawdownPct 이하여야 한다(§3.2 신규 제약).
  maxDrawdownPct: 20,
  maxDailyLossPct: 3,
  // 기본은 현물 Long-only. 레버리지·파생·가상자산은 정책 계층에서 꺼둔 상태로 시작한다.
  allowedAssets: { equity: true, etf: true, leverage: false, futures: false, options: false, derivatives: false, crypto: false },
  approvalMode: "manual",
};

const STORAGE_KEY = "sentient.mandate.draft";

type ChatMessage = { from: "agent" | "user"; text: string };

const OPENING: ChatMessage[] = [
  { from: "agent", text: "안녕하세요. 저는 김세리 AI 투자 어시스턴트입니다. 운용 지침 설정을 위한 기본 구성을 준비했습니다. 세부 조건을 정교화하기 위해 몇 가지 질문을 드릴게요." },
  { from: "agent", text: '먼저, 투자 기간을 알려주시겠어요? 예를 들어 "3년 이상", "은퇴 전까지", 혹은 "단기 전술 자금" 등이 있습니다.' },
];

const FOLLOW_UPS = [
  "현금화가 필요한 시점이나 유동성 조건이 있나요?",
  "특정 업종이나 피하고 싶은 자산이 있나요?",
  "손실이 발생했을 때 어느 수준까지 감내할 수 있나요?",
];

/** 폼과 같은 색·굵기를 쓰는 섹션 제목. */
function SectionHeading({ index, title, suffix }: { index: number; title: string; suffix?: string }) {
  return (
    <h2 className="text-headline-md font-headline-md mb-4 text-primary flex items-center gap-2 border-b border-outline-variant pb-2">
      <span className="text-secondary">{index}.</span> {title}
      {suffix ? <span className="text-body-sm font-body-sm font-normal text-outline normal-case ml-2">{suffix}</span> : null}
    </h2>
  );
}

/**
 * 제목 옆 ⓘ 아이콘. 마우스오버·키보드 포커스 둘 다에서 뜬다(`group-hover`
 * 만 쓰면 마우스로만 접근 가능해진다 - 아이콘에 `tabIndex`를 주고
 * `group-focus-within`도 같이 걸어 탭 이동으로도 확인할 수 있게 했다).
 *
 * 기존 드롭다운(TopNav 계정 전환)과 같은 카드 스타일
 * (surface-container-lowest/border-outline-variant/shadow-sm)을 그대로 쓴다 -
 * 이 앱에 팝오버 컴포넌트가 따로 없어서 이미 검증된 조합을 재사용했다.
 */
function InfoTooltip({ text }: { text: string }) {
  return (
    <span className="relative inline-flex group">
      <span
        tabIndex={0}
        role="note"
        aria-label={text}
        className="material-symbols-outlined text-[14px] text-outline cursor-help outline-none focus-visible:ring-1 focus-visible:ring-primary rounded-full"
      >
        info
      </span>
      <span
        role="tooltip"
        className="pointer-events-none absolute left-1/2 -translate-x-1/2 bottom-full mb-2 hidden group-hover:block group-focus-within:block w-64 p-2.5 text-[11px] font-normal normal-case leading-relaxed text-on-surface bg-surface-container-lowest border border-outline-variant rounded-lg shadow-sm z-10"
      >
        {text}
      </span>
    </span>
  );
}

function FieldLabel({ children, hint }: { children: React.ReactNode; hint?: string }) {
  return (
    <span className="block text-label-md font-label-md text-secondary mb-2 uppercase">
      {children}
      {hint ? <span className="text-on-surface-variant lowercase font-normal"> ({hint})</span> : null}
    </span>
  );
}

export default function MandateConfig() {
  const [draft, setDraft] = useState<MandateDraft>(DEFAULT_DRAFT);
  const [messages, setMessages] = useState<ChatMessage[]>(OPENING);
  const [reply, setReply] = useState("");
  const [step, setStep] = useState(0);
  const [notice, setNotice] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const chatEndRef = useRef<HTMLDivElement>(null);

  const patch = useCallback(<K extends keyof MandateDraft>(key: K, value: MandateDraft[K]) => {
    setDraft((current) => ({ ...current, [key]: value }));
  }, []);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [messages.length]);

  useEffect(() => {
    if (!notice) return undefined;
    // Case id·검토 상태처럼 읽는 데 시간이 걸리는 문구가 생겨 4초에서 늘렸다.
    const timer = window.setTimeout(() => setNotice(""), 8000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const capitalDisplay = useMemo(() => draft.baseCapital.toLocaleString("en-US"), [draft.baseCapital]);

  function onCapitalChange(raw: string) {
    const digits = raw.replace(/[^\d]/g, "");
    patch("baseCapital", digits ? Number(digits) : 0);
  }

  function toggleAsset(id: AssetClassId) {
    setDraft((current) => ({
      ...current,
      allowedAssets: { ...current.allowedAssets, [id]: !current.allowedAssets[id] },
    }));
  }

  function sendReply() {
    const value = reply.trim();
    if (!value) return;
    setMessages((current) => [
      ...current,
      { from: "user", text: value },
      { from: "agent", text: FOLLOW_UPS[step] ?? "확인했어요. 이 내용은 지침 초안에 메모해둘게요." },
    ]);
    setStep((current) => Math.min(current + 1, FOLLOW_UPS.length));
    setReply("");
  }

  function saveDraft() {
    // BFF 미연결 상태라 서버로 보내지 않는다. 브라우저 안에만 남긴다.
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(draft));
    setNotice("브라우저에 임시 저장했습니다. 서버(BFF)에는 아직 전송되지 않았습니다.");
  }

  async function submit() {
    if (submitting) return;
    setSubmitting(true);
    try {
      // USER_INPUT_SPEC.md §5: objective_text는 DB not null이라 사용자가
      // 아무것도 안 쓰면 선택 결과에서 자동 생성한다. 자연어는 판정에 쓰지
      // 않는다 - 저장·맥락 전달용일 뿐이다.
      const fallbackObjective = `${RISK_PROFILES.find((p) => p.id === draft.riskProfile)?.label ?? "균형"} 성향 · 지침`;
      const objectiveText = draft.objective.trim() || fallbackObjective;

      const result = await submitMandateDraft(draft, objectiveText);
      setNotice(
        result.stage === "FAST_APPLIED"
          ? `v${result.version}로 즉시 적용됐습니다.`
          : result.stage === "AWAITING_REVIEW"
            ? `v${result.version} 제안이 Risk/QA 검토 대기로 넘어갔습니다(Case ${result.caseId ?? "-"}).`
            : `제출됐습니다: ${result.stage}`,
      );
    } catch (error) {
      setNotice(
        error instanceof MandateSubmissionError
          ? error.message
          : "지침 제출 중 알 수 없는 오류가 발생했습니다.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-1 overflow-hidden font-sans">
      <main className="flex-1 flex flex-col md:flex-row gap-gutter p-margin-mobile md:p-margin-desktop overflow-y-auto max-w-app mx-auto w-full">
        {/* ── 좌: 헤더(카드 밖, 바탕에 그대로) + 설정 폼 카드 ── */}
        <div className="flex-1 min-w-0 flex flex-col gap-gutter min-h-0">
          <header className="flex justify-between items-start gap-4 shrink-0">
            <div className="min-w-0">
              <div className="flex items-center gap-2 text-label-md font-label-md text-secondary mb-2 uppercase flex-wrap">
                <span>User Input</span>
                <span className="material-symbols-outlined text-[14px]" aria-hidden="true">arrow_forward</span>
                <span>CEO Router</span>
                <span className="material-symbols-outlined text-[14px]" aria-hidden="true">arrow_forward</span>
                <span className="font-bold text-primary">Risk / QA Gate</span>
              </div>
              <h1 className="text-headline-lg font-headline-lg text-on-surface font-bold tracking-tight">운용 지침 설정</h1>
              <p className="text-body-sm font-body-sm text-on-surface-variant mt-2 max-w-2xl">
                기본 운용 파라미터를 확인하고 저장하세요.<br />복합적이거나 세밀한 조건은 AI 어시스턴트와의 대화를 통해 정교화되며, 최종
                거버넌스 버전은 제출 후 생성돼요.
              </p>
            </div>
            <div className="flex items-center gap-2 bg-surface-container-high px-3 py-1 rounded-full text-xs font-medium text-secondary shrink-0">
              <span className="w-2 h-2 rounded-full bg-tertiary-fixed-dim animate-pulse" aria-hidden="true" />
              CONNECTING
            </div>
          </header>

          <div className="flex-1 min-h-0 bg-surface-container-lowest border border-outline-variant rounded-lg flex flex-col overflow-hidden shadow-sm">
          <div className="p-6 space-y-10 overflow-y-auto">
            {/* 1. 목표와 위험 성향 */}
            <section>
              <SectionHeading index={1} title="목표 & 위험 성향" />
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
                <div>
                  <label>
                    <FieldLabel hint="natural language">투자 목표</FieldLabel>
                    <textarea
                      value={draft.objective}
                      onChange={(event) => patch("objective", event.target.value)}
                      className="w-full h-32 p-4 bg-surface rounded-lg border border-outline-variant focus:border-primary focus:ring-1 focus:ring-primary focus:outline-none text-body-md font-body-md resize-none shadow-inner"
                      placeholder="예: 장기적인 자산 가치 보존과 안정적인 수익 창출을 목표로 하며, 하락 리스크는 최소화하고 싶어."
                    />
                  </label>
                  <p className="text-xs text-on-surface-variant mt-2">구체적인 종목이나 기간은 AI 어시스턴트가 다음 질문으로 확인합니다.</p>
                </div>

                <div>
                  <FieldLabel hint="select one">위험 성향</FieldLabel>
                  <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 lg:h-32" role="radiogroup" aria-label="위험 성향">
                    {RISK_PROFILES.map((item) => {
                      const on = draft.riskProfile === item.id;
                      return (
                        <button
                          key={item.id}
                          type="button"
                          role="radio"
                          aria-checked={on}
                          onClick={() => patch("riskProfile", item.id)}
                          className={`h-full border rounded-lg p-4 flex flex-col items-center justify-center text-center transition-colors ${
                            on ? "border-primary bg-secondary-container shadow-sm" : "border-outline-variant hover:bg-surface-container"
                          }`}
                        >
                          <span
                            className={`material-symbols-outlined mb-2 text-2xl ${on ? "fill" : ""} ${on ? "text-primary" : item.tone}`}
                            aria-hidden="true"
                          >
                            {item.icon}
                          </span>
                          <span className={`font-semibold text-body-sm font-body-sm ${on ? "text-primary" : "text-on-surface"}`}>{item.label}</span>
                          <span className="text-[10px] text-on-surface-variant mt-1 leading-tight">{item.note}</span>
                        </button>
                      );
                    })}
                  </div>
                </div>
              </div>
            </section>

            {/* 2. 자본과 통화 */}
            <section>
              <SectionHeading index={2} title="자본 및 통화" />
              <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
                <div>
                  <label>
                    <span className="block text-label-md font-label-md text-secondary mb-1 uppercase">기본 자산</span>
                    <span className="text-[10px] text-outline block mb-2 font-mono">risk_bounds.base_capital</span>
                    <input
                      type="text"
                      inputMode="numeric"
                      value={capitalDisplay}
                      onChange={(event) => onCapitalChange(event.target.value)}
                      className="w-full p-3 bg-surface rounded border border-outline-variant focus:border-primary focus:ring-1 focus:ring-primary focus:outline-none text-data-mono font-data-mono font-bold tracking-wider"
                    />
                  </label>
                </div>
                <div>
                  <label>
                    <span className="block text-label-md font-label-md text-secondary mb-1 uppercase">통화</span>
                    <span className="text-[10px] text-outline block mb-2 font-mono">&nbsp;</span>
                    <span className="relative block">
                      <select
                        value={draft.currency}
                        onChange={(event) => patch("currency", event.target.value)}
                        className="w-full p-3 bg-surface rounded border border-outline-variant focus:border-primary focus:ring-1 focus:ring-primary focus:outline-none text-body-md font-body-md appearance-none font-medium"
                      >
                        <option value="USD">USD - US Dollar</option>
                        <option value="KRW">KRW - South Korean Won</option>
                        <option value="EUR">EUR - Euro</option>
                      </select>
                      <span className="material-symbols-outlined absolute right-3 top-3.5 text-secondary pointer-events-none" aria-hidden="true">
                        expand_more
                      </span>
                    </span>
                  </label>
                </div>
              </div>
            </section>

            {/* 3. 비중과 익스포저 한도 */}
            <section>
              <SectionHeading index={3} title="비중, 익스포저 한도" />
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-6">
                <div>
                  <div className="flex justify-between items-center mb-2 gap-2">
                    <label htmlFor="max-weight" className="text-label-md font-label-md text-secondary uppercase flex items-center gap-1">
                      한 종목 최대 투자 비율
                      <InfoTooltip text="특정 주식 하나에 최대로 투자할 수 있는 자산 비율입니다." />
                    </label>
                    <span className="text-data-mono font-data-mono font-bold bg-surface-container-high px-2 py-0.5 rounded shrink-0">
                      {draft.maxSingleWeightPct}%
                    </span>
                  </div>
                  <input
                    id="max-weight"
                    type="range"
                    min={5}
                    max={50}
                    value={draft.maxSingleWeightPct}
                    onChange={(event) => patch("maxSingleWeightPct", Number(event.target.value))}
                    className="w-full h-2 bg-surface-container-highest rounded-lg appearance-none cursor-pointer accent-primary"
                  />
                  <div className="flex justify-between text-[10px] text-outline mt-1 font-data-mono">
                    <span>5%</span>
                    <span>25%</span>
                    <span>50%</span>
                  </div>
                </div>
                <div>
                  <div className="flex justify-between items-center mb-2 gap-2">
                    <label htmlFor="gross-exposure" className="text-label-md font-label-md text-secondary uppercase flex items-center gap-1">
                      최대 위험 노출액
                      <InfoTooltip text="레버리지를 포함해 실제로 보유할 수 있는 포지션의 상한입니다. 100%는 원금만큼만, 300%는 대출을 더해 원금의 3배까지 보유한다는 뜻이며, 레버리지 특성상 100%를 넘는 값도 설정할 수 있습니다." />
                    </label>
                    <span className="text-data-mono font-data-mono font-bold bg-surface-container-high px-2 py-0.5 rounded shrink-0">
                      {draft.grossExposurePct}%
                    </span>
                  </div>
                  <input
                    id="gross-exposure"
                    type="range"
                    min={100}
                    max={300}
                    value={draft.grossExposurePct}
                    onChange={(event) => patch("grossExposurePct", Number(event.target.value))}
                    className="w-full h-2 bg-surface-container-highest rounded-lg appearance-none cursor-pointer accent-primary"
                  />
                  <div className="flex justify-between text-[10px] text-outline mt-1 font-data-mono">
                    <span>100%</span>
                    <span>200%</span>
                    <span>300%</span>
                  </div>
                </div>
                <div>
                  <div className="flex justify-between items-center mb-2 gap-2">
                    <label htmlFor="max-drawdown" className="text-label-md font-label-md text-secondary uppercase flex items-center gap-1">
                      전체 최대 손실 한도
                      <InfoTooltip text="포지션 크기와 관계없이, 원금 대비 감내 가능한 최대 손실 비율입니다. 손실 한도이므로 100%를 넘는 값은 설정할 수 없습니다." />
                    </label>
                    <span className="text-data-mono font-data-mono font-bold bg-surface-container-high px-2 py-0.5 rounded shrink-0">
                      {draft.maxDrawdownPct}%
                    </span>
                  </div>
                  <input
                    id="max-drawdown"
                    type="range"
                    min={5}
                    max={50}
                    value={draft.maxDrawdownPct}
                    onChange={(event) => {
                      const next = Number(event.target.value);
                      patch("maxDrawdownPct", next);
                      // daily <= drawdown 제약. 슬라이더 두 개가 서로 어긋나게 두지 않는다.
                      if (draft.maxDailyLossPct > next) patch("maxDailyLossPct", next);
                    }}
                    className="w-full h-2 bg-surface-container-highest rounded-lg appearance-none cursor-pointer accent-primary"
                  />
                  <div className="flex justify-between text-[10px] text-outline mt-1 font-data-mono">
                    <span>5%</span>
                    <span>25%</span>
                    <span>50%</span>
                  </div>
                </div>
                <div>
                  <div className="flex justify-between items-center mb-2 gap-2">
                    <label htmlFor="max-daily-loss" className="text-label-md font-label-md text-secondary uppercase flex items-center gap-1">
                      일일 최대 손실 한도
                      <InfoTooltip text="하루 동안 발생할 수 있는 손실의 최대 제한 금액입니다." />
                    </label>
                    <span className="text-data-mono font-data-mono font-bold bg-surface-container-high px-2 py-0.5 rounded shrink-0">
                      {draft.maxDailyLossPct}%
                    </span>
                  </div>
                  <input
                    id="max-daily-loss"
                    type="range"
                    min={1}
                    max={draft.maxDrawdownPct}
                    value={draft.maxDailyLossPct}
                    onChange={(event) => patch("maxDailyLossPct", Number(event.target.value))}
                    className="w-full h-2 bg-surface-container-highest rounded-lg appearance-none cursor-pointer accent-primary"
                  />
                  <div className="flex justify-between text-[10px] text-outline mt-1 font-data-mono">
                    <span>1%</span>
                    <span>전체 최대 손실({draft.maxDrawdownPct}%) 이하</span>
                  </div>
                </div>
              </div>
            </section>

            {/* 4. 자산 운용 정책 */}
            <section>
              <SectionHeading index={4} title="자산 운용 정책"/>
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-7 gap-3">
                {ASSET_CLASSES.map((asset) => {
                  const on = draft.allowedAssets[asset.id];
                  return (
                    <button
                      key={asset.id}
                      type="button"
                      aria-pressed={on}
                      onClick={() => toggleAsset(asset.id)}
                      className={`rounded-lg p-3 flex flex-col items-center justify-center text-center cursor-pointer transition-colors relative overflow-hidden bg-surface hover:bg-surface-container ${
                        on ? "border-2 border-tertiary-fixed-dim" : "border border-outline-variant opacity-60 hover:opacity-100"
                      }`}
                    >
                      {on ? <span className="absolute inset-0 bg-tertiary-fixed-dim opacity-10" aria-hidden="true" /> : null}
                      <span className={`material-symbols-outlined mb-1 ${on ? "text-on-tertiary-container" : "text-secondary"}`} aria-hidden="true">
                        {asset.icon}
                      </span>
                      <span className="text-label-md font-label-md font-bold text-on-surface mb-1">{asset.label}</span>
                      <span className={`text-[10px] font-semibold ${on ? "text-on-tertiary-container" : "text-error"}`}>
                        {on ? "✓ 허용됨" : "✕ 금지됨"}
                      </span>
                    </button>
                  );
                })}
              </div>
            </section>

            {/* 5. 주문 승인 모드 */}
            <section>
              <SectionHeading index={5} title="주문 승인 모드" />
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4" role="radiogroup" aria-label="주문 승인 모드">
                {APPROVAL_MODES.map((mode) => {
                  const on = draft.approvalMode === mode.id;
                  return (
                    <button
                      key={mode.id}
                      type="button"
                      role="radio"
                      aria-checked={on}
                      onClick={() => patch("approvalMode", mode.id)}
                      className={`border rounded-lg p-4 flex items-center gap-4 text-left transition-colors ${
                        on ? "border-primary bg-secondary-container shadow-sm" : "border-outline-variant hover:bg-surface-container"
                      }`}
                    >
                      <span
                        className={`material-symbols-outlined text-3xl shrink-0 ${on ? "fill text-primary" : "text-secondary"}`}
                        aria-hidden="true"
                      >
                        {mode.icon}
                      </span>
                      <span>
                        <span className={`block font-bold text-body-md font-body-md ${on ? "text-primary" : "text-on-surface"}`}>{mode.label}</span>
                        <span className="block text-xs text-on-surface-variant mt-1">{mode.note}</span>
                      </span>
                    </button>
                  );
                })}
              </div>
            </section>
          </div>

          <footer className="border-t border-outline-variant p-4 bg-surface-bright flex justify-between items-center gap-4 flex-wrap mt-auto">
            <div className="text-xs text-on-surface-variant flex items-center gap-1 font-medium">
              <span className="material-symbols-outlined text-[16px]" aria-hidden="true">info</span>
              제출은 Risk/QA Gate를 거칩니다. 완화(LOOSEN) 변경은 검토 승인 후 적용됩니다.
            </div>
            <div className="flex gap-3">
              <button
                type="button"
                onClick={saveDraft}
                className="px-6 py-2 border border-primary text-primary rounded font-bold text-body-sm hover:bg-surface-container-high transition-colors"
              >
                임시 저장
              </button>
              <button
                type="button"
                onClick={submit}
                disabled={submitting}
                className="px-6 py-2 bg-primary text-on-primary rounded font-bold text-body-sm hover:bg-primary-container transition-colors shadow-sm disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {submitting ? "제출 중..." : "지침 제출 및 검토"}
              </button>
            </div>
            {notice ? (
              <p role="status" className="w-full text-xs text-on-surface-variant">{notice}</p>
            ) : null}
          </footer>
          </div>
        </div>

        {/* ── 우: AI 어시스턴트 ────────────────────────────── */}
        {/*
          2026-08-13: 채팅 내부(max-h-[28rem])를 잡아도 이 카드 자체엔 상한이
          없어서, 헤더+상태바+채팅+입력창을 합친 실제 높이만큼은 여전히 자란다.
          이 카드를 감싸는 상위(main)가 페이지 전체 스크롤이라 카드가 길어지면
          왼쪽 폼 패널과 높이가 어긋나 보인다. max-h를 여기도 박아 카드 전체
          높이를 고정하고, 내부 채팅 영역만 스크롤되게 한다.
        */}
        <div className="w-full md:w-[380px] shrink-0 max-h-[42rem] bg-surface-container-lowest border border-outline-variant rounded-lg flex flex-col overflow-hidden shadow-sm">
          <header className="bg-primary text-on-primary p-4 flex justify-between items-center">
            <div className="flex items-center gap-2">
              <span className="material-symbols-outlined text-[20px]" aria-hidden="true">robot_2</span>
              <span className="font-bold text-body-sm font-body-sm">CEO 콘솔 · AI 어시스턴트</span>
            </div>
            <div className="flex gap-2 opacity-70" aria-hidden="true">
              <span className="material-symbols-outlined text-[16px]">minimize</span>
              <span className="material-symbols-outlined text-[16px]">close</span>
            </div>
          </header>

          <div className="bg-surface border-b border-outline-variant p-3 flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-full bg-tertiary text-on-tertiary flex items-center justify-center font-bold relative shrink-0">
                AI
                <span className="absolute bottom-0 right-0 w-3 h-3 bg-tertiary-fixed-dim border-2 border-surface rounded-full" aria-hidden="true" />
              </div>
              <div>
                <div className="font-bold text-body-sm font-body-sm text-on-surface">김세리</div>
                <div className="text-[10px] text-on-surface-variant">Mandate Interview Worker</div>
              </div>
            </div>
            <span className="text-[10px] font-bold border border-outline px-2 py-0.5 rounded text-secondary uppercase">Online</span>
          </div>

          {/*
            2026-08-13: `flex-1`만으로는 안 잡힌다 - 부모(`main`)가 페이지
            전체를 overflow-y-auto로 스크롤하고, 이 카드는 md:flex-row 안에서
            높이가 콘텐츠만큼 자라는 구조라 이 요소를 제약하는 상위 높이가
            없었다. 그래서 대화가 길어질수록 채팅창 자체가 페이지처럼
            무한히 길어졌다. max-h를 직접 박아 상위 flex 체인과 무관하게
            항상 이 안에서만 스크롤되게 한다.
          */}
          <div className="flex-1 min-h-60 max-h-[28rem] p-4 overflow-y-auto flex flex-col gap-4 bg-background" aria-live="polite" aria-label="Mandate 인터뷰 대화">
            {messages.map((message, index) => (
              <div
                key={`${message.from}-${index}`}
                className={`flex flex-col gap-1 max-w-[85%] ${message.from === "agent" ? "items-start" : "items-end self-end"}`}
              >
                <span className="text-[10px] text-secondary mx-1 font-medium">{message.from === "agent" ? "김세리" : "대표님"}</span>
                <div
                  className={`p-3 text-body-sm font-body-sm shadow-sm rounded-2xl ${
                    message.from === "agent"
                      ? "bg-surface-container border border-outline-variant text-on-surface rounded-tl-sm"
                      : "bg-secondary-container text-on-secondary-fixed rounded-tr-sm"
                  }`}
                >
                  {message.text}
                </div>
              </div>
            ))}
            <div ref={chatEndRef} aria-hidden="true" />
          </div>

          <div className="p-3 border-t border-outline-variant bg-surface">
            <div className="flex items-end gap-2">
              <textarea
                value={reply}
                onChange={(event) => setReply(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    sendReply();
                  }
                }}
                aria-label="AI 어시스턴트 답변"
                placeholder="답변을 입력하세요..."
                className="flex-1 min-w-0 bg-surface-container-lowest border border-outline-variant rounded-lg p-2 text-body-sm font-body-sm resize-none h-[44px] focus:border-primary focus:ring-1 focus:ring-primary focus:outline-none"
              />
              <button
                type="button"
                onClick={sendReply}
                disabled={!reply.trim()}
                aria-label="전송"
                className="bg-primary text-on-primary w-[44px] h-[44px] rounded-lg flex items-center justify-center hover:bg-primary-container transition-colors shrink-0 disabled:opacity-40"
              >
                <span className="material-symbols-outlined" aria-hidden="true">send</span>
              </button>
            </div>
            <p className="text-[10px] text-on-surface-variant text-center mt-2">
              Conversation context is synchronized with the mandate draft.
            </p>
          </div>
        </div>
      </main>
    </div>
  );
}
