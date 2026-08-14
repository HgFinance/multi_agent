"use client";

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import {
  DEFAULT_DRAFT,
  INTERVIEW,
  INTERVIEW_DONE,
  MINDSET_BY_RISK_PROFILE,
  MandateSubmissionError,
  applyChoice,
  applySuggestions,
  loadInvestorProfile,
  loadMandateForFund,
  nextStep,
  policyToDraft,
  requestMandateSuggestion,
  submitMandateDraft,
  validateDraft,
} from "../lib/mandateClient";
import { provisionalRiskScore, type Experience } from "../lib/mandatePresets";
import {
  DEFAULT_ACCOUNT,
  accountFor,
  readStoredAccountId,
  subscribeToAccountChange,
} from "../lib/currentAccount";

/**
 * 운용 지침 설정 화면.
 *
 * 좌측은 정책 폼, 우측은 그 폼을 채우는 인터뷰 콘솔이다.
 *
 * "지침 저장"은 `../lib/mandateClient.ts`를 거쳐 `POST /ui/mandates`(최초 1회,
 * 없을 때만) + `POST /ui/mandates/{id}/versions`(항상) + `POST /ui/investor-profiles`
 * 로 BFF에 전달된다. 이 화면은 정책 version과 적합성 프로필을 저장할 뿐,
 * 주문·원장·한도나 Risk/QA 승인 흐름을 시작하지 않는다.
 *
 * "임시 저장"은 브라우저(localStorage)에만 남는다 - 완성 전 초안까지 Mandate
 * Version으로 만들면 `content_hash` 중복·승인 흐름이 매번 발동한다
 * (USER_INPUT_API_SPEC §2.4와 같은 이유). **사용자별로 키를 나눈다** - 단일
 * 키면 계정을 전환했을 때 남의 초안이 보인다.
 *
 * ## 우측 콘솔이 좌측 폼을 어디까지 고칠 수 있는가
 *
 * LLM이 닿는 값은 **투자 목표 문장·투자 기간·유동성 필요도 셋뿐**이다
 * (서버 `ALLOWED_SUGGESTION_FIELDS`). 위험 성향·투자 경험·자본·승인 방식은
 * 칩 버튼과 숫자 입력, 즉 사용자의 명시적 선택에서만 나온다 —
 * `USER_INPUT_SPEC` 4.1과 `suitability.py`가 LLM의 성향·경험 추론을 영구
 * 금지하기 때문이다. 대본(`INTERVIEW`)이 그 경계를 구조로 강제한다.
 */

export type RiskProfile = "conservative" | "neutral" | "aggressive";
export type ApprovalMode = "auto" | "manual";
export type AssetClassId = "equity" | "etf" | "leverage" | "futures" | "options" | "derivatives" | "crypto";
/** `suitability.py` `LiquidityNeed`와 같은 값. 현금이 급히 필요할 가능성. */
export type LiquidityNeed = "HIGH" | "MEDIUM" | "LOW";

export interface MandateDraft {
  objective: string;
  riskProfile: RiskProfile;
  /**
   * 2026-08-14 추가. 이 필드가 없어서 이 화면과 `mandateClient.ts`가 각각
   * INTERMEDIATE를 자리표시자로 박았고, 등급이 `min(mindset, experience)`라
   * RISK_SEEKING(3)이 항상 2로 잘렸다 - "공격적"을 눌러도 "중립적"과 똑같은
   * 기본값이 나오고 등급 3은 이 화면에서 도달 자체가 불가능했다.
   * 두 자리가 같은 필드를 읽으면 그 어긋남이 구조적으로 불가능해진다.
   */
  experience: Experience;
  /**
   * `InvestorProfileIn` 필수 필드(1~100). 아직 안 물어봤으면 `null`이다 -
   * 0으로 채우면 "0년 투자"라는 답을 사용자가 한 것처럼 저장된다.
   */
  investmentHorizonYears: number | null;
  /** 위와 같은 이유로 미응답은 `null`. */
  liquidityNeed: LiquidityNeed | null;
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

const EXPERIENCES: { id: Experience; label: string; note: string }[] = [
  { id: "BEGINNER", label: "초보", note: "1년 미만" },
  { id: "INTERMEDIATE", label: "중급", note: "1~5년" },
  { id: "EXPERIENCED", label: "숙련", note: "5년 이상" },
];

/** 사용자별로 나눈다 - 단일 키면 계정을 전환했을 때 남의 초안이 보인다. */
function storageKeyFor(userId: string): string {
  return `sentient.mandate.draft.${userId}`;
}

/**
 * 임시 저장해둔 초안. 없거나 읽을 수 없으면 `null`.
 *
 * `DEFAULT_DRAFT`를 바탕에 깔고 저장된 값을 덮는 이유: 필드가 추가된 뒤에
 * 저장된 옛 초안에는 그 키가 없어서, 그대로 쓰면 `undefined`가 폼에 들어간다
 * (실제로 `experience`가 그렇게 추가됐다).
 */
function readLocalDraft(userId: string): MandateDraft | null {
  try {
    const raw = window.localStorage.getItem(storageKeyFor(userId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<MandateDraft>;
    return {
      ...DEFAULT_DRAFT,
      ...parsed,
      allowedAssets: { ...DEFAULT_DRAFT.allowedAssets, ...(parsed.allowedAssets ?? {}) },
    };
  } catch {
    return null;
  }
}

type ChatMessage = { from: "agent" | "user"; text: string };

/**
 * 첫 인사. 두 번째 줄을 손으로 적지 않고 대본 1번을 그대로 쓴다 - 따로 적으면
 * 대본을 고쳤을 때 인사말만 옛 질문에 남는다.
 */
const OPENING: ChatMessage[] = [
  { from: "agent", text: "안녕하세요. 저는 김세리 AI 투자 어시스턴트입니다. 몇 가지 여쭤보면서 좌측 운용 지침을 함께 채워드릴게요." },
  { from: "agent", text: INTERVIEW[0].prompt },
];

const RESTORED_PLACEHOLDER = "예: 장기적인 자산 가치 보존과 안정적인 수익 창출을 목표로 하며, 하락 리스크는 최소화하고 싶어.";
const LOCKED_PLACEHOLDER = "화면 우측의 콘솔창을 이용해서 대화하세요.";

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

/**
 * 계정 전환을 구독하고, **`key`로 폼을 통째로 다시 마운트한다.**
 *
 * 계정이 바뀔 때 상태를 손으로 되돌리면(`setDraft(DEFAULT_DRAFT)` 등) 두 가지가
 * 생긴다 - 상태를 하나 추가할 때마다 초기화도 같이 적어야 하고(빠뜨리면 옛
 * 사용자 값이 남는다), effect 안에서 동기 `setState`를 하게 돼 렌더가 한 번 더
 * 돈다. `key`를 바꾸면 React가 알아서 전부 버린다.
 *
 * `TopNav`가 같은 탭에서 계정을 바꾸면 `ACCOUNT_CHANGED_EVENT`가, 다른 탭이면
 * `storage`가 날아온다 - `currentAccount.ts`가 둘 다 듣는 구독을 이미 갖고 있어
 * 그대로 쓴다.
 */
export default function MandateConfig() {
  const userId = useSyncExternalStore(
    subscribeToAccountChange,
    readStoredAccountId,
    () => DEFAULT_ACCOUNT.userId,
  );
  return <MandateConfigForm key={userId} userId={userId} />;
}

function MandateConfigForm({ userId }: { userId: string }) {
  const [draft, setDraft] = useState<MandateDraft>(DEFAULT_DRAFT);
  const [messages, setMessages] = useState<ChatMessage[]>(OPENING);
  const [reply, setReply] = useState("");
  const [step, setStep] = useState(0);
  const [notice, setNotice] = useState("");
  const [submitting, setSubmitting] = useState(false);
  /** 어시스턴트 응답 대기 중. 입력창을 잠가 같은 질문에 두 번 답하지 않게 한다. */
  const [busy, setBusy] = useState(false);
  /** 서버가 정한 실질 위험 등급. **화면이 재계산하지 않는다**(API_SPEC 2.3). */
  const [riskBand, setRiskBand] = useState("");
  const chatEndRef = useRef<HTMLDivElement>(null);

  /**
   * 어느 계정으로 보고 있는지. `TopNav`가 같은 탭에서 계정을 바꾸면
   * `ACCOUNT_CHANGED_EVENT`가 날아오고, 다른 탭이면 `storage`가 날아온다 -
   * `currentAccount.ts`가 둘 다 듣는 구독을 이미 갖고 있어 그대로 쓴다.
   */
  /** 인터뷰가 끝나야 투자 목표 입력창이 열린다. 파생값이라 따로 상태를 두지 않는다. */
  const locked = step < INTERVIEW.length;
  const violations = useMemo(() => validateDraft(draft), [draft]);

  /**
   * 지금 물어보고 있는 질문. 칩 단계에서는 자유 입력을 막는다 - 선택지가 있는
   * 질문에 자유 문장을 받아봐야 어느 칩을 고른 것인지 판정해야 하고, 그 판정은
   * LLM이 성향을 정하는 것과 같아진다(USER_INPUT_SPEC 4.1).
   */
  const currentStep = INTERVIEW[step];
  const inputDisabled = busy || !currentStep || Boolean(currentStep.choices);
  const inputPlaceholder = busy
    ? "답변을 기다리는 중입니다…"
    : !currentStep
      ? "설정이 끝났습니다. 좌측 폼에서 직접 수정하세요."
      : currentStep.choices
        ? "아래 버튼에서 골라주세요."
        : "답변을 입력하세요...";

  const patch = useCallback(<K extends keyof MandateDraft>(key: K, value: MandateDraft[K]) => {
    setDraft((current) => ({ ...current, [key]: value }));
  }, []);

  /**
   * 위험 성향·투자 경험을 고르면 슬라이더 4개가 그 등급의 잠정 기본값으로 바뀐다.
   * 두 선택 모두 `applyChoice` 하나를 거치는 이유는 그쪽 주석에 적어뒀다 -
   * 요약하면 등급이 `min(성향, 경험)`이라 어느 쪽이 바뀌어도 재계산해야 한다.
   */
  const selectRiskProfile = useCallback((profile: RiskProfile) => {
    setDraft((current) => applyChoice(current, { riskProfile: profile }));
  }, []);

  const selectExperience = useCallback((experience: Experience) => {
    setDraft((current) => applyChoice(current, { experience }));
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

  /**
   * 이 사용자의 저장본을 불러온다. 계정 전환 시 폼 초기화는 상위의 `key`가
   * 담당하므로(아래 `MandateConfig` 참고) 여기서 손으로 되돌리지 않는다.
   *
   * 저장된 지침이 있으면 인터뷰를 건너뛴다 - 이미 답한 사람에게 같은 질문을
   * 7개 다시 시키지 않는다. 없으면 기본값 + 인터뷰 1단계부터다.
   *
   * `cancelled` 가드는 응답 도착 전에 언마운트된 경우를 막는다 - 계정을 빠르게
   * 두 번 바꾸면 옛 인스턴스의 응답이 뒤늦게 도착한다.
   */
  useEffect(() => {
    let cancelled = false;
    const account = accountFor(userId);

    async function hydrate() {
      if (!account.fundId) {
        if (!cancelled) setNotice("이 계정에는 연결된 Fund가 없어 저장된 지침을 불러올 수 없습니다.");
        return;
      }
      let next = DEFAULT_DRAFT;
      let loadedVersion = 0;

      try {
        const stored = await loadMandateForFund(account.fundId);
        if (cancelled) return;
        if (stored?.policy) {
          next = policyToDraft(next, stored.policy, stored.objectiveText);
          loadedVersion = stored.version;
        }
      } catch (cause) {
        if (!cancelled) {
          setNotice(cause instanceof Error ? cause.message : "저장된 지침을 불러오지 못했습니다.");
        }
        return;
      }

      // 성향·경험·기간·유동성은 정책이 아니라 적합성 프로필에 있다. 없으면(404)
      // 조용히 넘어간다 - 프로필을 아직 안 만든 사용자다.
      try {
        const profile = await loadInvestorProfile(account.userId, account.fundId);
        if (cancelled) return;
        if (profile) {
          next = {
            ...next,
            riskProfile: profile.riskProfile,
            experience: profile.experience,
            investmentHorizonYears: profile.investmentHorizonYears,
            liquidityNeed: profile.liquidityNeed,
          };
          setRiskBand(profile.effectiveRiskReason || profile.effectiveRiskBand);
        }
      } catch {
        // 적합성 조회 실패가 정책 표시를 막을 이유는 없다.
      }
      if (cancelled) return;

      // 저장하지 않은 임시 초안이 있으면 그게 더 최신이다(사용자가 저장본을 본
      // 뒤에 눌렀으므로). 어느 쪽을 보고 있는지 반드시 알린다.
      const local = readLocalDraft(account.userId);
      if (local) {
        setDraft(local);
        // 인터뷰 단계를 저장하지 않으므로(이어하기 기능이 아니다) 폼은 열어둔다.
        // 대신 인터뷰 중에 임시 저장한 초안이면 적합성 프로필에 필요한 답이
        // 비어 있을 수 있어, 저장 때 가서야 알게 되지 않도록 지금 알린다.
        setStep(INTERVIEW.length);
        setNotice("저장되지 않은 임시 초안을 복원했습니다. DB 저장본과 다를 수 있습니다.");
        setMessages([
          { from: "agent", text: "저장되지 않은 임시 초안을 불러왔습니다. 좌측에서 이어서 수정하세요." },
          ...(local.investmentHorizonYears === null || local.liquidityNeed === null
            ? [{
                from: "agent" as const,
                text: "투자 기간·유동성 응답이 초안에 없습니다. 이대로 저장하면 지침은 저장되지만 적합성 프로필은 저장되지 않습니다.",
              }]
            : []),
        ]);
        return;
      }

      setDraft(next);
      if (loadedVersion > 0) {
        setStep(INTERVIEW.length);
        setMessages([
          { from: "agent", text: `저장된 지침 v${loadedVersion}을 불러왔습니다. 좌측에서 바로 수정하실 수 있어요.` },
        ]);
      }
    }

    void hydrate();
    return () => {
      cancelled = true;
    };
  }, [userId]);

  const capitalDisplay = useMemo(() => draft.baseCapital.toLocaleString("en-US"), [draft.baseCapital]);

  /**
   * 투자 경험이 성향을 끌어내렸는지. `EXPERIENCED`는 `min()`에서 항상 성향 쪽이
   * 이기므로 "경험 상한이 없을 때의 등급"과 같다.
   *
   * **등급 이름을 여기서 보여주지 않는다** - 사용자에게 보이는 실질 등급은
   * 서버(`effective_risk_band`)만 정한다(`mandatePresets.ts` 주석과 API_SPEC 2.3).
   * 이 값은 "왜 공격적을 눌렀는데 슬라이더가 안 올라가지"를 설명하는 용도다.
   */
  const clampedByExperience = useMemo(() => {
    const mindset = MINDSET_BY_RISK_PROFILE[draft.riskProfile];
    return provisionalRiskScore(mindset, draft.experience) < provisionalRiskScore(mindset, "EXPERIENCED");
  }, [draft.riskProfile, draft.experience]);

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

  /** 대본의 다음 질문(또는 완료 문구)을 붙이고 단계를 옮긴다. */
  function advance(from: number, next: MandateDraft, extra: ChatMessage[] = []) {
    const target = nextStep(next, from);
    setStep(target);
    setMessages((current) => [
      ...current,
      ...extra,
      { from: "agent", text: INTERVIEW[target]?.prompt ?? INTERVIEW_DONE },
    ]);
  }

  /** 칩 버튼 하나를 고른 것. LLM을 거치지 않는 결정론 경로다. */
  function choose(label: string, choicePatch: Partial<MandateDraft>) {
    if (busy) return;
    const next = applyChoice(draft, choicePatch);
    setDraft(next);
    setMessages((current) => [...current, { from: "user", text: label }]);
    advance(step + 1, next);
  }

  async function send() {
    const value = reply.trim();
    const current = INTERVIEW[step];
    if (!value || busy || !current) return;
    setReply("");
    setMessages((existing) => [...existing, { from: "user", text: value }]);

    // 숫자 응답은 화면이 직접 해석한다 - 자본·기간을 LLM에 맡길 이유가 없고,
    // 맡기면 같은 입력이 다르게 읽힐 수 있다.
    if (current.parse) {
      const parsed = current.parse(value);
      if (!parsed) {
        setMessages((existing) => [
          ...existing,
          { from: "agent", text: current.retry ?? "다시 한 번 입력해 주세요." },
        ]);
        return;
      }
      const next = applyChoice(draft, parsed);
      setDraft(next);
      advance(step + 1, next);
      return;
    }

    setBusy(true);
    try {
      const history = [...messages, { from: "user" as const, text: value }].map((message) => ({
        role: message.from === "agent" ? ("assistant" as const) : ("user" as const),
        content: message.text,
      }));
      const result = await requestMandateSuggestion(history, draft);
      const applied = applySuggestions(draft, result.suggestions);
      // 제안이 목표 문장을 못 뽑았어도(LLM 장애 시 서버가 빈 제안으로 감싼다)
      // 사용자가 직접 쓴 문장은 그대로 남긴다 - 추론이 아니라 사용자의 말이다.
      const next = applied.draft.objective ? applied.draft : { ...applied.draft, objective: value };
      setDraft(next);

      const extra: ChatMessage[] = [{ from: "agent", text: result.reply }];
      const skipped = [...applied.unapplied, ...result.dropped_fields];
      if (skipped.length > 0) {
        extra.push({
          from: "agent",
          text: `${skipped.join(", ")}은(는) 제가 정할 수 있는 항목이 아니라 반영하지 않았습니다. 아래에서 직접 골라주세요.`,
        });
      }
      advance(step + 1, next, extra);
    } catch (cause) {
      /*
       * 어시스턴트가 죽어도 대본을 멈추지 않는다. 여기서 단계를 붙잡으면
       * 인터뷰가 끝나야 열리는 투자 목표 입력창이 영영 안 열려서, LLM 장애
       * 하나가 폼 전체를 잠그는 교착이 된다.
       *
       * 대신 **사용자가 방금 친 문장을 그대로** 목표로 넣고 넘어간다 - 추론이
       * 아니라 사용자의 말 그대로라 원칙 5에 걸리지 않는다. 어시스턴트가 하려던
       * 일은 문장을 다듬는 것뿐이었고, 기간·유동성은 어차피 뒤 단계에서 직접
       * 물어본다(`skipIf`가 null이라 안 건너뛴다).
       */
      const next = { ...draft, objective: draft.objective || value };
      setDraft(next);
      advance(step + 1, next, [
        {
          from: "agent",
          text: `${cause instanceof Error ? cause.message : "제안을 받지 못했습니다."} 적어주신 내용은 목표에 그대로 넣어뒀습니다.`,
        },
      ]);
    } finally {
      setBusy(false);
    }
  }

  function saveDraft() {
    // 이 브라우저에만 남긴다. DB 저장은 "지침 저장"이다.
    try {
      window.localStorage.setItem(storageKeyFor(userId), JSON.stringify(draft));
      setNotice("이 브라우저에만 임시 저장했습니다. DB에 남기려면 [지침 저장]을 누르세요.");
    } catch {
      // Safari 사생활 보호 모드 등에서 던진다. 저장을 못 하는 것이 화면을 멈출 이유는 아니다.
      setNotice("이 브라우저가 임시 저장을 허용하지 않습니다. [지침 저장]으로 DB에 저장하세요.");
    }
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
      // 커밋됐으므로 임시 초안은 지운다 - 남겨두면 다음 방문에 DB 저장본 대신
      // 옛 초안이 복원돼 방금 저장한 값이 안 보인다.
      try {
        window.localStorage.removeItem(storageKeyFor(userId));
      } catch {
        /* 삭제 실패는 무시한다 - 저장 자체는 이미 성공했다. */
      }
      // 정책과 적합성 프로필은 서비스가 달라 한 트랜잭션이 아니다. 하나만
      // 저장됐으면 둘 다 됐다고 말하지 않는다.
      setNotice(
        result.profileError
          ? `지침 v${result.version}은 DB에 저장됐습니다. 다만 적합성 프로필은 저장되지 않았습니다 - ${result.profileError}`
          : `지침 v${result.version}과 적합성 프로필이 DB에 저장됐습니다.`,
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
                      disabled={locked}
                      className="w-full h-32 p-4 bg-surface rounded-lg border border-outline-variant focus:border-primary focus:ring-1 focus:ring-primary focus:outline-none text-body-md font-body-md resize-none shadow-inner disabled:opacity-60 disabled:cursor-not-allowed"
                      placeholder={locked ? LOCKED_PLACEHOLDER : RESTORED_PLACEHOLDER}
                    />
                  </label>
                  <p className="text-xs text-on-surface-variant mt-2">
                    {locked
                      ? "대화가 끝나면 이 칸을 직접 수정할 수 있습니다."
                      : "구체적인 종목이나 기간은 AI 어시스턴트가 다음 질문으로 확인합니다."}
                  </p>
                </div>

                <div>
                  <FieldLabel hint="select one">위험 성향</FieldLabel>
                  <div className="grid grid-cols-1 sm:grid-cols-3 gap-4" role="radiogroup" aria-label="위험 성향">
                    {RISK_PROFILES.map((item) => {
                      const on = draft.riskProfile === item.id;
                      return (
                        <button
                          key={item.id}
                          type="button"
                          role="radio"
                          aria-checked={on}
                          onClick={() => selectRiskProfile(item.id)}
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

                  {/*
                    투자 경험. 예전엔 이 문항이 없어서 화면과 `mandateClient.ts`가
                    각각 INTERMEDIATE를 자리표시자로 박았고, 등급이
                    `min(성향, 경험)`이라 "공격적"이 영원히 "중립적"과 같은
                    기본값을 냈다. 사용자에게 직접 묻는 것이 그 자리표시자를
                    없애는 유일한 방법이다.
                  */}
                  <div className="mt-4">
                    <FieldLabel hint="select one">투자 경험</FieldLabel>
                    <div className="grid grid-cols-3 gap-2" role="radiogroup" aria-label="투자 경험">
                      {EXPERIENCES.map((item) => {
                        const on = draft.experience === item.id;
                        return (
                          <button
                            key={item.id}
                            type="button"
                            role="radio"
                            aria-checked={on}
                            onClick={() => selectExperience(item.id)}
                            className={`border rounded-lg py-2 px-1 text-center transition-colors ${
                              on ? "border-primary bg-secondary-container shadow-sm" : "border-outline-variant hover:bg-surface-container"
                            }`}
                          >
                            <span className={`block font-semibold text-body-sm font-body-sm ${on ? "text-primary" : "text-on-surface"}`}>
                              {item.label}
                            </span>
                            <span className="block text-[10px] text-on-surface-variant">{item.note}</span>
                          </button>
                        );
                      })}
                    </div>
                    {clampedByExperience ? (
                      <p className="text-[11px] text-on-surface-variant mt-2 leading-relaxed">
                        투자 경험이 성향보다 낮아 더 보수적인 등급의 기본값이 제안됐습니다. 아래 슬라이더로 직접 조정할 수 있습니다.
                      </p>
                    ) : null}
                    {riskBand ? (
                      <p className="text-[11px] text-secondary mt-1 leading-relaxed">저장된 적합성 판정: {riskBand}</p>
                    ) : null}
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
                      // daily <= drawdown 제약. 슬라이더 두 개가 서로 어긋나게 두지 않는다.
                      // `setDraft` 한 번으로 둘을 같이 옮긴다 - `patch`를 두 번 부르면
                      // 두 번째가 렌더 클로저의 옛 `draft.maxDailyLossPct`를 읽어서,
                      // 슬라이더를 빠르게 끌 때 제약 위반이 그대로 남는다.
                      setDraft((current) => ({
                        ...current,
                        maxDrawdownPct: next,
                        maxDailyLossPct: Math.min(current.maxDailyLossPct, next),
                      }));
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
              저장하면 새 지침 version과 적합성 프로필만 기록됩니다. 활성화·주문·원장 변경은 수행하지 않습니다.
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
                disabled={submitting || violations.length > 0}
                className="px-6 py-2 bg-primary text-on-primary rounded font-bold text-body-sm hover:bg-primary-container transition-colors shadow-sm disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {submitting ? "저장 중..." : "지침 저장"}
              </button>
            </div>
            {/*
              서버가 어차피 같은 제약으로 거절하지만, 422를 받고 나서야 알려주면
              어느 슬라이더를 만져야 하는지 화면이 설명할 수 없다. 위반이 있는
              동안은 저장 버튼도 잠근다.
            */}
            {violations.length > 0 ? (
              <ul role="alert" className="w-full text-xs text-error list-disc pl-4 space-y-0.5">
                {violations.map((violation) => (
                  <li key={violation.rule}>
                    {violation.rule} ({violation.detail})
                  </li>
                ))}
              </ul>
            ) : null}
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

            {/*
              대기 표시를 `messages`에 넣지 않는 이유: 응답이 오면 다시 빼야
              하고, 실패 경로에서 빼는 걸 빠뜨리면 영원히 "생각 중"이 남는다.
              조건부 렌더는 정리할 것이 없다.
            */}
            {busy ? (
              <div className="flex flex-col gap-1 max-w-[85%] items-start">
                <span className="text-[10px] text-secondary mx-1 font-medium">김세리</span>
                <div className="p-3 text-body-sm font-body-sm shadow-sm rounded-2xl rounded-tl-sm bg-surface-container border border-outline-variant text-on-surface-variant italic">
                  답변을 생각하고 있습니다…
                </div>
              </div>
            ) : null}

            {/*
              칩 버튼. 성향·경험·유동성·승인 방식은 **여기서만** 정해진다 -
              LLM이 고르지 않는다(USER_INPUT_SPEC 4.1).
            */}
            {!busy && currentStep?.choices ? (
              <div className="flex flex-wrap gap-2" role="group" aria-label="선택지">
                {currentStep.choices.map((choice) => (
                  <button
                    key={choice.label}
                    type="button"
                    onClick={() => choose(choice.label, choice.patch)}
                    className="border border-primary text-primary rounded-full px-3 py-1.5 text-body-sm font-body-sm font-medium hover:bg-secondary-container transition-colors"
                  >
                    {choice.label}
                  </button>
                ))}
              </div>
            ) : null}

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
                    void send();
                  }
                }}
                disabled={inputDisabled}
                aria-label="AI 어시스턴트 답변"
                placeholder={inputPlaceholder}
                className="flex-1 min-w-0 bg-surface-container-lowest border border-outline-variant rounded-lg p-2 text-body-sm font-body-sm resize-none h-[44px] focus:border-primary focus:ring-1 focus:ring-primary focus:outline-none disabled:opacity-60 disabled:cursor-not-allowed"
              />
              <button
                type="button"
                onClick={() => void send()}
                disabled={inputDisabled || !reply.trim()}
                aria-label="전송"
                className="bg-primary text-on-primary w-[44px] h-[44px] rounded-lg flex items-center justify-center hover:bg-primary-container transition-colors shrink-0 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <span className="material-symbols-outlined" aria-hidden="true">send</span>
              </button>
            </div>
            <p className="text-[10px] text-on-surface-variant text-center mt-2 leading-relaxed">
              대화에서 자동 반영되는 값은 투자 목표·기간·유동성뿐입니다.<br />
              성향·경험·자본·승인 방식은 직접 선택합니다.
            </p>
          </div>
        </div>
      </main>
    </div>
  );
}
