/**
 * Mandate 저장·조회·인터뷰 로직 — `POST /ui/mandates` + `PUT /ui/mandates/{id}`
 * + `POST /ui/investor-profiles` + `POST /ui/mandate-assistant/suggest`.
 *
 * 근거: docs/02-engineering/USER_INPUT_API_SPEC.md 2.1~2.4
 *
 * ## 현재 Mandate 저장 의미론
 *
 * 이 화면은 Mandate 부모 행의 `metadata` JSONB를 한 행으로 갱신한다. 기존
 * `mandate_versions` 이력 경로는 이 화면에서 사용하지 않는다.
 *
 * 이 호출의 성공은 현재 사용자 입력을 DB에 저장했다는 뜻이며, 주문 제출이나
 * 원장 변경을 수행하지 않는다.
 *
 * ## 왜 화면 로직이 `.tsx`가 아니라 이 파일에 있는가
 *
 * 인터뷰 대본·선택 적용·제안 반영·정책 역변환은 전부 순수 함수인데, 저장소
 * 테스트 러너(`node --experimental-strip-types`)가 **JSX를 파싱하지 못한다**.
 * `.tsx`에 두면 `tests/mandate-interview.test.mjs`가 import할 수 없어 아무것도
 * 검증하지 못한다. 렌더링만 `.tsx`에 남긴다.
 */

import { BFF, bffFetch } from "./bffClient";
import {
  presetFor,
  findConstraintViolations,
  sliderDefaultsFor,
  FIXED_POLICY_VALUES,
  type Experience,
  type Mindset,
  type MandatePreset,
  type PolicyConstraintViolation,
} from "./mandatePresets";
import type {
  AssetClassId,
  LiquidityNeed,
  MandateDraft,
  RiskProfile,
} from "../mandate/MandateConfig";

/**
 * `RiskProfile`(화면 3지선다) -> `Mindset`(서버 계약). 1:1이라 추론이 아니다.
 *
 * `export`인 이유: `MandateConfig.tsx`가 성향 선택 시 슬라이더 기본값을 채울 때도
 * 같은 매핑이 필요하다. 여기서 하나만 두지 않고 화면에 또 하나를 두면, 나중에
 * 성향 3개 중 하나가 바뀔 때 한쪽만 고쳐질 위험이 있다.
 */
export const MINDSET_BY_RISK_PROFILE: Record<RiskProfile, Mindset> = {
  conservative: "SAFETY_FIRST",
  neutral: "BALANCED",
  aggressive: "RISK_SEEKING",
};

/** 저장된 적합성 프로필을 화면 값으로 되돌릴 때 쓰는 역매핑. */
export const RISK_PROFILE_BY_MINDSET: Record<Mindset, RiskProfile> = {
  SAFETY_FIRST: "conservative",
  BALANCED: "neutral",
  RISK_SEEKING: "aggressive",
};

/**
 * 자산군 코드. `USER_INPUT_SPEC.md` §8 미확정 2번 — **표준 코드값이 아직 없다.**
 *
 * 코드베이스에 실제로 쓰이는 값(`apps/api/portfolio_universe.py`)은 3개뿐이다.
 * 화면 체크박스 7개 중 ETF·선물·옵션·가상자산은 대응하는 코드가 어디에도 없어서
 * `PROVISIONAL_` 접두어를 붙인 자리표시자를 쓴다 — 재일/도현님이 표준값을
 * 정하면 이 표만 고치면 된다. 지어내지 않고 표시해 두는 이유는 개발 원칙 9와
 * 같다: 확정되지 않은 값을 확정된 것처럼 흘려보내지 않는다.
 */
const ASSET_CLASS_CODE: Record<AssetClassId, string> = {
  equity: "KOREA_EQUITY",
  leverage: "LEVERAGED_ETF",
  derivatives: "DERIVATIVES_HEDGE",
  etf: "PROVISIONAL_ETF",
  futures: "PROVISIONAL_FUTURES",
  options: "PROVISIONAL_OPTIONS",
  crypto: "PROVISIONAL_CRYPTO",
};

/** 저장된 정책을 화면 토글로 되돌릴 때 쓴다. 위 표에서 파생 - 따로 적지 않는다. */
const ASSET_ID_BY_CODE = new Map<string, AssetClassId>(
  (Object.keys(ASSET_CLASS_CODE) as AssetClassId[]).map((id) => [ASSET_CLASS_CODE[id], id]),
);

const NO_ASSETS: Record<AssetClassId, boolean> = {
  equity: false, etf: false, leverage: false, futures: false,
  options: false, derivatives: false, crypto: false,
};

/**
 * 첫 화면 기본값. **안전 방향으로 시작한다**(개발 원칙 9) — 성향은 보수적,
 * 경험은 초보, 파생·레버리지·가상자산은 꺼둔 현물 Long-only.
 *
 * 슬라이더 4개를 손으로 적지 않고 `sliderDefaultsFor`를 부르는 이유: 페이지를
 * 막 열었을 때 보이는 값이 "conservative를 눌렀을 때"와 달라지면, 성향 선택이
 * 기본값을 바꾼다는 이 화면의 약속과 첫 화면부터 어긋난다.
 */
const DEFAULT_EXPERIENCE: Experience = "BEGINNER";

export const DEFAULT_DRAFT: MandateDraft = {
  objective: "",
  riskProfile: "conservative",
  experience: DEFAULT_EXPERIENCE,
  investmentHorizonYears: null,
  liquidityNeed: null,
  baseCapital: 100_000_000,
  // 신규 Fund를 위한 안전 기본값은 KRW다. 현재 테스트 계정 3개는 저장된 USD
  // policy를 먼저 불러오므로 이 값으로 덮이지 않는다.
  currency: "KRW",
  ...sliderDefaultsFor(MINDSET_BY_RISK_PROFILE.conservative, DEFAULT_EXPERIENCE),
  allowedAssets: { ...NO_ASSETS, equity: true, etf: true },
  approvalMode: "manual",
};

export interface MandatePolicyPayload {
  allowed_assets: string[];
  forbidden_assets: string[];
  risk_bounds: {
    base_capital: string;
    currency: string;
    max_instrument_weight: string;
    max_sector_weight: string;
    max_gross_exposure: string;
    max_concurrent_positions: number;
    max_daily_loss: string;
    max_drawdown_pct: string;
  };
  universe_policy: {
    allowed_markets: string[];
    allowed_asset_classes: string[];
    forbidden_asset_classes: string[];
    preferred_sectors: string[];
    excluded_sectors: string[];
    trading_start: string;
    trading_end: string;
  };
  approval_rules: {
    paper_order_mode: "AUTO" | "USER_APPROVAL";
    risk_expansion_requires_user_approval: boolean;
  };
  /**
   * 화면에 아직 전용 입력칸이 없는 실행 규칙. 조회 응답에는 포함되며, 사용자가
   * 다른 필드를 고쳐 저장해도 그대로 보존해야 한다.
   */
  execution_rules?: Record<string, unknown>;
}

/**
 * 실제로 전송될 4개 한도값. `max_instrument_weight`/`max_gross_exposure`는
 * 화면이 직접 받은 값으로 프리셋을 덮어쓴다 - §3.1의 은닉 대상은
 * `max_sector_weight`/`max_concurrent_positions` 둘뿐이다.
 *
 * `draftToPolicy`와 `validateDraft`가 **같은 이 함수**를 쓰는 이유: 각자
 * 따로 값을 조합하면 한쪽만 고쳤을 때 "검증은 통과했는데 실제로 보낸 값은
 * 다르다"는 어긋남이 생긴다 — 실제로 이 어긋남 때문에 서버가 422를 냈다
 * (슬라이더 30% vs 프리셋 25%, 검증은 프리셋끼리만 봐서 못 잡았다).
 *
 * 등급을 `draft.experience`로 낸다. 예전엔 여기와 화면이 각각 INTERMEDIATE를
 * 자리표시자로 박고 있어서 `min(mindset, experience)`가 RISK_SEEKING을 항상
 * 2로 잘랐다 — 이제 두 자리가 같은 값을 읽으므로 그 어긋남이 불가능하다.
 */
function effectiveLimits(draft: MandateDraft): MandatePreset {
  const mindset = MINDSET_BY_RISK_PROFILE[draft.riskProfile];
  const preset = presetFor(mindset, draft.experience);
  return {
    max_instrument_weight: (draft.maxSingleWeightPct / 100).toFixed(4),
    max_sector_weight: preset.max_sector_weight,
    max_gross_exposure: (draft.grossExposurePct / 100).toFixed(4),
    max_concurrent_positions: preset.max_concurrent_positions,
  };
}

/**
 * 제출 전 클라이언트 검증. 서버(`policy.py`)가 같은 제약을 다시 검증하므로
 * 이건 이중 검사다 - 그래도 두는 이유는 422를 받고 나서야 "무엇이 틀렸는지"
 * 알려주면 사용자가 슬라이더 중 어느 걸 만져야 하는지 알 수 없기 때문이다
 * (`mandatePresets.ts`의 `findConstraintViolations` 문서와 같은 이유).
 */
export function validateDraft(draft: MandateDraft): PolicyConstraintViolation[] {
  return findConstraintViolations({
    preset: effectiveLimits(draft),
    maxDailyLoss: (draft.maxDailyLossPct / 100).toFixed(4),
    maxDrawdownPct: (draft.maxDrawdownPct / 100).toFixed(4),
  });
}

/** `MandateDraft` -> 서버 `MandatePolicy` 계약. 화면 필드 이름과 다르므로 여기서만 변환한다. */
export function draftToPolicy(draft: MandateDraft): MandatePolicyPayload {
  const limits = effectiveLimits(draft);

  const selectedClasses = (Object.keys(draft.allowedAssets) as AssetClassId[]).filter(
    (id) => draft.allowedAssets[id],
  );
  const excludedClasses = (Object.keys(draft.allowedAssets) as AssetClassId[]).filter(
    (id) => !draft.allowedAssets[id],
  );

  return {
    allowed_assets: [],
    forbidden_assets: [],
    risk_bounds: {
      base_capital: String(draft.baseCapital),
      currency: draft.currency,
      max_instrument_weight: limits.max_instrument_weight,
      max_sector_weight: limits.max_sector_weight,
      max_gross_exposure: limits.max_gross_exposure,
      max_concurrent_positions: limits.max_concurrent_positions,
      max_daily_loss: (draft.maxDailyLossPct / 100).toFixed(4),
      max_drawdown_pct: (draft.maxDrawdownPct / 100).toFixed(4),
    },
    universe_policy: {
      allowed_markets: [...FIXED_POLICY_VALUES.allowed_markets],
      allowed_asset_classes: selectedClasses.map((id) => ASSET_CLASS_CODE[id]),
      forbidden_asset_classes: excludedClasses.map((id) => ASSET_CLASS_CODE[id]),
      preferred_sectors: [],
      excluded_sectors: [],
      trading_start: FIXED_POLICY_VALUES.trading_start,
      trading_end: FIXED_POLICY_VALUES.trading_end,
    },
    approval_rules: {
      paper_order_mode: draft.approvalMode === "auto" ? "AUTO" : "USER_APPROVAL",
      risk_expansion_requires_user_approval: FIXED_POLICY_VALUES.risk_expansion_requires_user_approval,
    },
  };
}

const EDITABLE_ASSET_CLASS_CODES = new Set(Object.values(ASSET_CLASS_CODE));

function unique(values: string[]): string[] {
  return [...new Set(values)];
}

/**
 * 현재 폼이 편집할 수 있는 항목만 새 draft 값으로 바꾸고, 나머지 정책은 보존한다.
 *
 * DB에 직접 적재되거나 다른 화면에서 만든 세부 자산군·시장·거래시간·실행 규칙은
 * 이 화면에 대응 입력칸이 없다. `draftToPolicy()` 결과로 통째로 교체하면 사용자가
 * 자본금 하나만 고쳐도 그 정보가 사라지므로, 이 함수가 저장 직전 병합 경계다.
 */
export function mergePolicyForSave(
  draft: MandateDraft,
  previous: MandatePolicyPayload | null,
): MandatePolicyPayload {
  const edited = draftToPolicy(draft);
  if (!previous) return edited;

  const previousUniverse = previous.universe_policy;
  const preservedAllowedClasses = (previousUniverse.allowed_asset_classes ?? []).filter(
    (code) => !EDITABLE_ASSET_CLASS_CODES.has(code),
  );
  const preservedForbiddenClasses = (previousUniverse.forbidden_asset_classes ?? []).filter(
    (code) => !EDITABLE_ASSET_CLASS_CODES.has(code),
  );

  return {
    // 개별 instrument allow/deny 목록은 이 화면에서 편집하지 않는다.
    allowed_assets: [...(previous.allowed_assets ?? [])],
    forbidden_assets: [...(previous.forbidden_assets ?? [])],
    risk_bounds: edited.risk_bounds,
    universe_policy: {
      // 시장·선호/제외 섹터·거래시간도 현재 화면에 입력칸이 없으므로 유지한다.
      ...previousUniverse,
      allowed_asset_classes: unique([
        ...preservedAllowedClasses,
        ...edited.universe_policy.allowed_asset_classes,
      ]),
      forbidden_asset_classes: unique([
        ...preservedForbiddenClasses,
        ...edited.universe_policy.forbidden_asset_classes,
      ]),
    },
    approval_rules: edited.approval_rules,
    execution_rules: { ...(previous.execution_rules ?? {}) },
  };
}

/** 0~1 분수 문자열 -> 화면 % 정수. 슬라이더 범위를 벗어나면 잘라 넣는다. */
function pctFrom(fraction: string | undefined, min: number, max: number, fallback: number): number {
  const value = Math.round(Number(fraction) * 100);
  if (!Number.isFinite(value)) return fallback;
  // ponytail: 저장된 값이 슬라이더 범위 밖이면 잘라서 보여준다. 이 화면이 만들 수
  // 없는 값(예: 다른 경로가 넣은 gross 500%)을 사용자가 손대는 순간 조용히
  // 축소 저장하게 되므로, 슬라이더 범위가 넓어지면 이 clamp도 같이 넓힌다.
  return Math.min(max, Math.max(min, value));
}

/**
 * 저장된 정책 -> 화면 draft. `draftToPolicy`의 역함수다.
 *
 * **`applyChoice`를 타지 않는다.** 저장된 슬라이더 값을 등급 기본값으로 덮어쓰면
 * 사용자가 직접 조정해 저장한 한도가 불러오기 한 번에 사라진다.
 *
 * 성향(`riskProfile`)·경험(`experience`)·기간·유동성은 `MandatePolicy`에 없다 —
 * 적합성 프로필(`GET /ui/investor-profiles/current`)에서 따로 채운다. 그래서
 * `base`를 받아 정책이 나르는 필드만 덮는다.
 */
export function policyToDraft(
  base: MandateDraft,
  policy: MandatePolicyPayload,
  objectiveText: string,
): MandateDraft {
  const bounds = policy.risk_bounds ?? ({} as MandatePolicyPayload["risk_bounds"]);
  const universe = policy.universe_policy ?? ({} as MandatePolicyPayload["universe_policy"]);

  const allowedAssets = { ...NO_ASSETS };
  for (const code of universe.allowed_asset_classes ?? []) {
    const id = ASSET_ID_BY_CODE.get(code);
    // 모르는 코드는 무시한다 - 지어내서 켜지 않는다(개발 원칙 9).
    if (id) allowedAssets[id] = true;
  }

  const capital = Number(bounds.base_capital);
  const maxDrawdownPct = pctFrom(bounds.max_drawdown_pct, 5, 50, base.maxDrawdownPct);

  return {
    ...base,
    objective: objectiveText,
    baseCapital: Number.isFinite(capital) ? capital : base.baseCapital,
    currency: bounds.currency || base.currency,
    maxSingleWeightPct: pctFrom(bounds.max_instrument_weight, 5, 50, base.maxSingleWeightPct),
    grossExposurePct: pctFrom(bounds.max_gross_exposure, 100, 300, base.grossExposurePct),
    maxDrawdownPct,
    // daily <= drawdown 은 서버 제약이다. 불러온 뒤에도 지켜야 슬라이더가 즉시
    // 위반 상태로 열리지 않는다.
    maxDailyLossPct: pctFrom(bounds.max_daily_loss, 1, maxDrawdownPct, base.maxDailyLossPct),
    allowedAssets,
    approvalMode: policy.approval_rules?.paper_order_mode === "AUTO" ? "auto" : "manual",
  };
}

/**
 * 임시 저장 초안을 **서버 저장본 위에** 얹는다.
 *
 * `base`가 `DEFAULT_DRAFT`가 아니라 서버 저장본이어야 하는 이유(2026-08-14):
 * 초안에 없는 키를 공장 기본값으로 채우면, 서버에는 멀쩡히 있는 위험 성향·투자
 * 경험이 "보수적/초보"로 덮여 **일부 항목만 안 불러와지는 것처럼 보인다.**
 * 이 필드들은 나중에 추가돼서 그 전에 저장된 초안에는 키 자체가 없다.
 *
 * `null`/`undefined`는 덮지 않는다 - "아직 답 안 함"이 서버에 저장된 답을 지우면
 * 안 된다. 임시 초안은 **사용자가 고친 것만** 이긴다.
 */
export function mergeLocalDraft(base: MandateDraft, parsed: Partial<MandateDraft>): MandateDraft {
  // `allowedAssets`는 통째로 덮으면 서버가 켠 자산군이 사라지므로 따로 병합한다.
  const edited = Object.fromEntries(
    Object.entries(parsed).filter(
      ([key, value]) => value !== null && value !== undefined && key !== "allowedAssets",
    ),
  ) as Partial<MandateDraft>;
  return {
    ...base,
    ...edited,
    allowedAssets: { ...base.allowedAssets, ...(parsed.allowedAssets ?? {}) },
  };
}

/**
 * 선택 하나를 draft에 적용한다.
 *
 * 성향·경험 중 **어느 쪽이 바뀌든** `min()` 등급이 바뀔 수 있으므로 둘을 한
 * 함수로 묶는다 - 성향 쪽에만 재계산을 달면 경험을 바꿨을 때 슬라이더가 옛
 * 등급에 남는다.
 *
 * 반대로 자본·승인모드처럼 등급과 무관한 선택에서는 재계산하지 않는다. 이
 * 화면은 인터뷰 중에도 폼이 잠기지 않아 사용자가 슬라이더를 먼저 만질 수
 * 있는데, 무관한 선택이 그 값을 되돌리면 안 된다.
 *
 * 슬라이더 값은 **제안일 뿐 강제가 아니다** - 고른 뒤에도 사용자가 자유롭게
 * 움직일 수 있고, 실제 제출값은 그 시점의 슬라이더 값이다.
 */
export function applyChoice(draft: MandateDraft, patch: Partial<MandateDraft>): MandateDraft {
  const next = { ...draft, ...patch };
  if (!("riskProfile" in patch) && !("experience" in patch)) return next;
  return {
    ...next,
    ...sliderDefaultsFor(MINDSET_BY_RISK_PROFILE[next.riskProfile], next.experience),
  };
}

// ── 인터뷰 대본 ───────────────────────────────────────────────────────────────

/**
 * Mandate metadata는 이미 있지만 적합성 프로필이 없는 상태(`profileMissing`)를
 * 알려준다. 이 상태에서 자본·승인방식은 **이미 저장된 Mandate 값**이라 다시
 * 묻지 않는다 - 다시 물으면 기간·유동성만 회수하면 되는 화면이 자본까지
 * 처음부터 다시 받는 화면이 된다.
 */
export interface InterviewContext {
  hasStoredMandate: boolean;
}

export interface InterviewStep {
  /** 어시스턴트가 던지는 질문. */
  prompt: string;
  /** 있으면 칩 버튼으로 렌더한다. 클릭이 결정론적으로 draft를 고친다. */
  choices?: { label: string; patch: Partial<MandateDraft> }[];
  /** 자유 입력을 화면이 직접 해석한다(LLM 아님). `null`이면 되묻는다. */
  parse?: (text: string) => Partial<MandateDraft> | null;
  /** `POST /ui/mandate-assistant/suggest`로 보낸다. */
  llm?: true;
  /** 앞 단계에서 이미 채워졌으면 다시 묻지 않는다. */
  skipIf?: (draft: MandateDraft, ctx: InterviewContext) => boolean;
  /** 되물을 때 쓰는 안내. `parse` 단계에만 있다. */
  retry?: string;
}

/** 화면 입력창과 챗이 "1억"을 다르게 읽지 않도록 숫자 추출을 한 곳에 둔다. */
export function digitsOf(text: string): number | null {
  const digits = text.replace(/[^\d]/g, "");
  return digits ? Number(digits) : null;
}

export const MAN_WON = 10_000;

/**
 * 기본 자산 입력 단위. **저장되는 `risk_bounds.base_capital`은 언제나 원(통화
 * 최소 단위) 그대로다** - 여기서 바꾸는 건 화면이 받고 보여주는 단위뿐이다.
 *
 * KRW일 때만 만원이다. 통화를 USD로 바꿔놓고도 "만원"이라고 적혀 있으면 1만 배
 * 오입력이 나는데, 그게 금액 필드에서 가장 비싼 실수다. 통화 선택이 KRW 하나로
 * 고정되면 이 분기는 지워도 된다.
 */
export function capitalUnitFor(currency: string): { multiplier: number; label: string } {
  return currency === "KRW" ? { multiplier: MAN_WON, label: "만원" } : { multiplier: 1, label: currency };
}

/**
 * 인터뷰 대본. **구조화 값은 전부 사용자의 명시적 선택에서 나온다.**
 *
 * `USER_INPUT_SPEC` 4.1과 `suitability.py`가 LLM의 성향·경험 추론을 영구
 * 금지한다. 그래서 성향·경험·자본·승인모드는 LLM을 거치지 않는
 * `choices`/`parse` 단계이고, LLM이 닿는 단계는 첫 질문 하나뿐이다
 * (서버 allow-list: `objective_text`/`investment_horizon_years`/`liquidity_need`).
 *
 * 기간·유동성은 첫 답변에서 LLM이 뽑아내면 `skipIf`로 건너뛴다 - 이미 말한 걸
 * 다시 묻지 않되, 못 뽑았을 때 값이 비어 저장이 막히지도 않게 한다.
 */
export const INTERVIEW: InterviewStep[] = [
  {
    prompt:
      "먼저 투자 목표를 자유롭게 말씀해 주세요. 기간이나 현금이 필요한 시점까지 함께 적어주시면 더 좋아요.",
    llm: true,
  },
  {
    prompt: "위험 성향을 골라주세요.",
    choices: [
      { label: "보수적", patch: { riskProfile: "conservative" } },
      { label: "중립적", patch: { riskProfile: "neutral" } },
      { label: "공격적", patch: { riskProfile: "aggressive" } },
    ],
  },
  {
    prompt: "투자 경험은 어느 정도이신가요?",
    choices: [
      { label: "초보", patch: { experience: "BEGINNER" } },
      { label: "중급", patch: { experience: "INTERMEDIATE" } },
      { label: "숙련", patch: { experience: "EXPERIENCED" } },
    ],
  },
  {
    prompt: "몇 년 정도 투자하실 계획인가요?",
    retry: "1년에서 100년 사이 숫자로 알려주세요. 예: 10",
    skipIf: (draft) => draft.investmentHorizonYears !== null,
    parse: (text) => {
      const years = digitsOf(text);
      return years !== null && years >= 1 && years <= 100
        ? { investmentHorizonYears: years }
        : null;
    },
  },
  {
    prompt: "투자한 돈을 급하게 현금으로 찾아야 할 가능성은 어느 정도인가요?",
    skipIf: (draft) => draft.liquidityNeed !== null,
    choices: [
      { label: "높음 (며칠 안에)", patch: { liquidityNeed: "HIGH" } },
      { label: "보통 (몇 주 안에)", patch: { liquidityNeed: "MEDIUM" } },
      { label: "낮음 (당분간 없음)", patch: { liquidityNeed: "LOW" } },
    ],
  },
  {
    // 이 단계에 오는 시점의 통화는 항상 기본값 KRW다 - 통화는 인터뷰가 묻지
    // 않고, 저장본을 불러온 경우엔 인터뷰 자체를 건너뛴다. 그래서 여기서는
    // `capitalUnitFor`를 보지 않고 만원으로 고정해도 어긋나지 않는다.
    prompt: "운용할 기본 자산은 얼마인가요? (만원 단위로 입력해 주세요)",
    retry: "만원 단위 숫자로 입력해 주세요. 예: 10000 (= 1억원)",
    // Mandate가 이미 저장돼 있으면 자본은 이미 답한 값이다(`policyToDraft`가
    // 채워뒀다) - 적합성 프로필만 없어서 기간·유동성만 회수하는 화면에서
    // 이미 정해진 자본까지 다시 묻지 않는다.
    skipIf: (_draft, ctx) => ctx.hasStoredMandate,
    parse: (text) => {
      const manWon = digitsOf(text);
      return manWon !== null && manWon > 0 ? { baseCapital: manWon * MAN_WON } : null;
    },
  },
  {
    prompt: "마지막입니다. 주문 승인 방식을 골라주세요.",
    // 위와 같은 이유 - 승인 방식도 이미 저장된 Mandate 값이다.
    skipIf: (_draft, ctx) => ctx.hasStoredMandate,
    choices: [
      { label: "자동 실행", patch: { approvalMode: "auto" } },
      { label: "관리자 승인 필요", patch: { approvalMode: "manual" } },
    ],
  },
];

/**
 * Mandate는 있는데 적합성 프로필이 없을 때(`profileMissing`) 재개할 지점.
 * 목표·성향·경험은 이미 값이 있거나 좌측 버튼으로 바로 고를 수 있어 다시
 * 묻지 않고, 기간 질문(유일하게 `skipIf`와 `parse`를 동시에 갖는 단계)부터
 * 다시 시작한다 - 인덱스를 그대로 적으면 대본 순서가 바뀔 때 조용히
 * 어긋난다.
 */
export const PROFILE_RECOVERY_STEP_INDEX = INTERVIEW.findIndex(
  (step) => Boolean(step.skipIf) && Boolean(step.parse),
);

export const INTERVIEW_DONE =
  "사용자 mandate를 저장하겠습니다. 페이지 좌측의 mandate 페이지를 통해서 세부적인 내용을 변경할 수 있습니다.";

/** `skipIf`가 붙은 단계를 건너뛰고 실제로 물어볼 다음 단계를 찾는다. */
export function nextStep(
  draft: MandateDraft,
  from: number,
  ctx: InterviewContext = { hasStoredMandate: false },
): number {
  let index = from;
  while (index < INTERVIEW.length && INTERVIEW[index].skipIf?.(draft, ctx)) index += 1;
  return index;
}

// ── 챗봇 제안 (USER_INPUT_API_SPEC 2.4) ───────────────────────────────────────

export interface AssistantSuggestion {
  field: string;
  value: string | number;
  label: string;
  confidence: string;
  source: string;
}

export interface AssistantReply {
  reply: string;
  suggestions: AssistantSuggestion[];
  requires_user_confirmation: true;
  dropped_fields: string[];
}

const LIQUIDITY_VALUES: LiquidityNeed[] = ["HIGH", "MEDIUM", "LOW"];

/**
 * 제안을 draft에 반영한다.
 *
 * 서버가 이미 allow-list를 강제하는데 여기서 또 거르는 이유: **신뢰 경계다.**
 * 서버 프롬프트나 allow-list가 바뀌어도 `mindset` 같은 값이 이 화면의 위험
 * 필드를 움직이는 경로는 없어야 한다(`USER_INPUT_SPEC` 4.1).
 *
 * 반영하지 못한 필드는 조용히 버리지 않고 `unapplied`로 돌려준다 - 서버가
 * `dropped_fields`를 남기는 것과 같은 이유다. 값이 범위를 벗어난 경우도
 * 여기에 들어간다(예: 기간 0년).
 */
export function applySuggestions(
  draft: MandateDraft,
  suggestions: { field: string; value: string | number }[],
): { draft: MandateDraft; unapplied: string[] } {
  let next = draft;
  const unapplied: string[] = [];

  for (const suggestion of suggestions) {
    const { field, value } = suggestion;
    if (field === "objective_text" && String(value).trim()) {
      next = { ...next, objective: String(value).trim() };
    } else if (field === "investment_horizon_years") {
      const years = Math.round(Number(value));
      if (Number.isFinite(years) && years >= 1 && years <= 100) {
        next = { ...next, investmentHorizonYears: years };
      } else {
        unapplied.push(field);
      }
    } else if (field === "liquidity_need") {
      const need = String(value).toUpperCase() as LiquidityNeed;
      if (LIQUIDITY_VALUES.includes(need)) next = { ...next, liquidityNeed: need };
      else unapplied.push(field);
    } else {
      unapplied.push(field);
    }
  }
  return { draft: next, unapplied };
}

export class MandateSubmissionError extends Error {}

async function bffJson<T>(path: string, init?: RequestInit): Promise<{ status: number; body: T | null }> {
  let response: Response;
  try {
    response = await bffFetch(path, {
      ...init,
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...init?.headers,
      },
    });
  } catch {
    throw new MandateSubmissionError(`BFF(${BFF})에 연결하지 못했습니다.`);
  }
  const body = (await response.json().catch(() => null)) as T | null;
  return { status: response.status, body };
}

function errorMessage(body: unknown, status: number): string {
  if (body && typeof body === "object") {
    const message = (body as { message?: unknown }).message;
    const detail = (body as { detail?: unknown }).detail;
    if (typeof message === "string" && message.trim()) return message;
    if (typeof detail === "string" && detail.trim()) return detail;
  }
  return `Mandate 요청이 거부됐습니다 (HTTP ${status})`;
}

/**
 * `POST /ui/mandate-assistant/suggest`. **Stateless — 아무것도 저장하지 않는다.**
 *
 * 서버 `AssistantMessage`가 `extra="forbid"` + `content` 1~4000자라 `{role, content}`
 * 외의 키나 빈 문자열, 4000자 초과는 그대로 422다. 호출부가 매번 신경 쓰지 않도록
 * 여기서 한 번 다듬는다.
 *
 * LLM 장애는 서버가 빈 제안 200으로 감싸므로(`app.py`의 fail-closed 경로)
 * 이 함수의 예외는 사실상 네트워크·게이트웨이 오류다.
 */
export async function requestMandateSuggestion(
  messages: { role: "user" | "assistant"; content: string }[],
  draft: MandateDraft,
  fundId: string,
): Promise<AssistantReply> {
  if (!fundId) {
    throw new MandateSubmissionError(
      "현재 계정에 연결된 Fund가 없습니다. 계정을 전환하거나 관리자에게 문의하세요.",
    );
  }
  const trimmed = messages
    .map((message) => ({ role: message.role, content: message.content.trim().slice(0, 4000) }))
    .filter((message) => message.content.length > 0);
  if (trimmed.length === 0) throw new MandateSubmissionError("보낼 대화 내용이 없습니다.");

  const { status, body } = await bffJson<AssistantReply>("/ui/mandate-assistant/suggest", {
    method: "POST",
    body: JSON.stringify({
      fund_id: fundId,
      messages: trimmed,
      // 맥락용일 뿐 저장 대상이 아니다 - 이미 적어둔 목표를 다시 제안하지 않게 한다.
      current_draft: { objective_text: draft.objective },
    }),
  });
  if (status !== 200 || !body) throw new MandateSubmissionError(errorMessage(body, status));
  return body;
}

// ── 저장된 지침 불러오기 (계정 전환 시 그 사용자 기록) ─────────────────────────

export interface StoredMandate {
  mandateId: string;
  objectiveText: string;
  objective: Record<string, unknown>;
  policy: MandatePolicyPayload | null;
}

export interface StoredProfile {
  riskProfile: RiskProfile;
  experience: Experience;
  investmentHorizonYears: number | null;
  liquidityNeed: LiquidityNeed | null;
  /** 서버가 정한 실질 등급. **화면이 재계산하지 않는다**(USER_INPUT_API_SPEC 2.3). */
  effectiveRiskBand: string;
  effectiveRiskReason: string;
}

/**
 * 이 Fund의 현재 Mandate. 없으면(404) `null` — **여기서 만들지 않는다.**
 *
 * 생성은 사용자가 실제로 저장을 누를 때만 일어난다(`lookupOrCreateMandate`).
 * 화면을 열기만 해도 빈 Mandate 껍데기가 생기면 Fund마다 쓰레기 행이 쌓인다.
 */
export async function loadMandateForFund(fundId: string): Promise<StoredMandate | null> {
  const { status, body } = await bffJson<{
    mandate_id: string;
    objective_text?: string;
    objective?: Record<string, unknown>;
    policy?: MandatePolicyPayload;
  }>(`/ui/mandates/by-fund/${fundId}/current`);

  if (status === 404) return null;
  if (status !== 200 || !body) throw new MandateSubmissionError(errorMessage(body, status));
  return {
    mandateId: body.mandate_id,
    objectiveText: body.objective_text ?? "",
    objective: body.objective ?? {},
    policy: body.policy ?? null,
  };
}

/** 이 사용자·Fund의 적합성 프로필. 없으면(404) `null`. */
export async function loadInvestorProfile(
  userId: string,
  fundId: string,
): Promise<StoredProfile | null> {
  const query = `user_id=${encodeURIComponent(userId)}&fund_id=${encodeURIComponent(fundId)}`;
  const { status, body } = await bffJson<{
    mindset: Mindset;
    experience: Experience;
    investment_horizon_years: number;
    liquidity_need: LiquidityNeed;
    effective_risk_band: string;
    effective_risk_reason: string;
  }>(`/ui/investor-profiles/current?${query}`);

  if (status === 404) return null;
  if (status !== 200 || !body) throw new MandateSubmissionError(errorMessage(body, status));
  return {
    riskProfile: RISK_PROFILE_BY_MINDSET[body.mindset] ?? DEFAULT_DRAFT.riskProfile,
    experience: body.experience ?? DEFAULT_DRAFT.experience,
    investmentHorizonYears: body.investment_horizon_years ?? null,
    liquidityNeed: body.liquidity_need ?? null,
    effectiveRiskBand: body.effective_risk_band ?? "",
    effectiveRiskReason: body.effective_risk_reason ?? "",
  };
}

// ── 저장 ─────────────────────────────────────────────────────────────────────

interface MandateLookup {
  mandateId: string;
  existing: StoredMandate | null;
}

/**
 * 이 Fund의 mandate_id를 찾는다. 없으면 만든다.
 *
 * 409(한 Fund에 Mandate 2개 이상, 모호함)는 여기서 하나를 임의로 고르지 않고
 * 그대로 에러로 던진다 - USER_INPUT_API_SPEC 2.1이 정한 원칙과 같다.
 */
async function lookupOrCreateMandate(fundId: string, ownerUserId: string): Promise<MandateLookup> {
  const existing = await loadMandateForFund(fundId);
  if (existing) {
    return { mandateId: existing.mandateId, existing };
  }

  // 404 - 이 Fund에 Mandate가 아직 없다. 최초 1회이므로 껍데기를 만든다.
  const created = await bffJson<{ mandate_id: string }>("/ui/mandates", {
    method: "POST",
    body: JSON.stringify({
      fund_id: fundId,
      owner_user_id: ownerUserId,
      name: `${ownerUserId} 운용 지침`,
    }),
  });
  if (created.status !== 201 || !created.body) {
    throw new MandateSubmissionError(errorMessage(created.body, created.status));
  }
  return { mandateId: created.body.mandate_id, existing: null };
}

/**
 * `InvestorProfileIn`(`extra="forbid"`) 형태. 성향·경험은 **사용자가 고른 값
 * 그대로** 보낸다 - 서버가 `effective_risk_band`를 계산하고 화면은 재계산하지
 * 않는다(USER_INPUT_API_SPEC 2.3).
 *
 * 기간·유동성이 아직 비어 있으면 `null`을 돌려 저장을 건너뛴다. 0년이나
 * MEDIUM 같은 값을 지어 넣으면 사용자가 답한 적 없는 적합성 정보가 저장된다.
 */
export function draftToInvestorProfile(
  draft: MandateDraft,
  userId: string,
  fundId: string,
  asOf: string,
): Record<string, unknown> | null {
  if (draft.investmentHorizonYears === null || draft.liquidityNeed === null) return null;
  return {
    user_id: userId,
    fund_id: fundId,
    mindset: MINDSET_BY_RISK_PROFILE[draft.riskProfile],
    experience: draft.experience,
    investment_horizon_years: draft.investmentHorizonYears,
    max_drawdown_pct: (draft.maxDrawdownPct / 100).toFixed(4),
    liquidity_need: draft.liquidityNeed,
    // 필수 필드이고 **타임존이 없으면 서버가 거절한다**(`suitability.py`
    // `validate_as_of`). `toISOString()`은 항상 Z가 붙는다.
    // 프로필 자체의 기준 시각이다. Mandate metadata는 version을 만들지 않지만,
    // 두 서비스에 걸친 저장 결과를 화면에서 같은 저장 시점으로 설명한다.
    as_of: asOf,
  };
}

export interface MandateSubmitResult {
  saved: true;
  /**
   * 적합성 프로필이 저장되지 않은 사유. `undefined`면 둘 다 저장됐다.
   * **Mandate metadata는 이미 저장된 상태다** - 호출부가 "둘 다 성공"으로
   * 뭉뚱그리지 않도록 분리해서 돌려준다.
   */
  profileError?: string;
}

/**
 * 지침 저장 버튼의 진입점. verifiedUserId는 현재 데모 계정 ID이고, BFF가
 * 같은 subject와 Fund membership을 다시 확인한다.
 *
 * `fundId`는 호출부(`MandateConfig.tsx`)가 이 폼을 읽어올 때 쓴 것과 **같은
 * 값**을 그대로 넘겨야 한다. 예전에는 여기서 `currentFund.ts`의 전역
 * `currentFundId()`를 따로 불렀는데, 그 값은 계정 전환 UI가 있던 시절
 * `localStorage`에 남은 선택을 그대로 읽어 화면이 실제로 불러온 Fund와
 * 어긋날 수 있었다 - 저장은 옛 선택의 Fund로, 조회는 고정 계정의 Fund로 갈라져
 * "저장했는데 안 불러와진다"는 증상이 났다(2026-08-24 실측).
 *
 * 쓰기가 둘이다 — 현재 Mandate metadata(거버넌스)와 적합성 프로필. 순서는 정책이 먼저다:
 * 정책이 주 산출물이고, 적합성 저장소 장애가 거버넌스 저장을 막는 것은 방향이
 * 반대다.
 *
 * ponytail: 두 서비스에 걸친 트랜잭션이 없다. 정책만 저장되고 프로필이 실패하는
 * 부분 성공이 가능하며, 그때 롤백하지 않고 `profileError`로 사실대로 알린다.
 */
export async function submitMandateDraft(
  draft: MandateDraft,
  objectiveText: string,
  verifiedUserId: string,
  fundId: string,
): Promise<MandateSubmitResult> {
  // 네트워크를 타기 전에 먼저 막는다 - 서버가 어차피 같은 제약으로 거절하지만,
  // 여기서 잡으면 어느 슬라이더가 문제인지 구체적으로 말해줄 수 있다.
  const violations = validateDraft(draft);
  if (violations.length > 0) {
    throw new MandateSubmissionError(
      `제출 전 정책 검증 실패: ${violations.map((v) => `${v.rule} (${v.detail})`).join(", ")}`,
    );
  }

  if (!fundId) {
    throw new MandateSubmissionError(
      "현재 계정에 연결된 Fund가 없습니다. 계정을 전환하거나 관리자에게 문의하세요.",
    );
  }
  const { mandateId, existing } = await lookupOrCreateMandate(fundId, verifiedUserId);
  const policy = mergePolicyForSave(draft, existing?.policy ?? null);

  const nowIso = new Date().toISOString();
  const { status, body } = await bffJson<{
    mandate_id: string;
    updated_at: string;
  }>(`/ui/mandates/${mandateId}`, {
    method: "PUT",
    body: JSON.stringify({
      policy,
      objective_text: objectiveText,
      // 구조화 objective는 아직 폼에서 직접 편집하지 않으므로 기존 값을 보존한다.
      objective: existing?.objective ?? {},
      // Governance API가 최종 metadata를 만들 때 top-level 값을 사용하므로 별도로
      // 전달한다. policy 안에만 두면 기본값 `{}`가 기존 실행 규칙을 덮는다.
      execution_rules: policy.execution_rules ?? {},
      created_by: verifiedUserId,
    }),
  });
  if ((status !== 200 && status !== 201) || !body) {
    throw new MandateSubmissionError(errorMessage(body, status));
  }

  return { saved: true, profileError: await saveInvestorProfile(draft, fundId, nowIso, verifiedUserId) };
}

/**
 * 적합성 프로필 저장. **실패해도 던지지 않는다** - Mandate metadata는 이미 저장된
 * 뒤라, 여기서 예외를 던지면 호출부가 "저장 실패"로만 보고해 사용자가 이미
 * 저장된 지침을 다시 저장하려 든다. 사유 문자열을 돌려 사실대로 알린다.
 */
async function saveInvestorProfile(
  draft: MandateDraft,
  fundId: string,
  asOf: string,
  verifiedUserId: string,
): Promise<string | undefined> {
  const profile = draftToInvestorProfile(draft, verifiedUserId, fundId, asOf);
  if (!profile) return "투자 기간·유동성 응답이 없어 적합성 프로필은 저장하지 않았습니다.";

  try {
    const { status, body } = await bffJson<unknown>("/ui/investor-profiles", {
      method: "POST",
      body: JSON.stringify(profile),
    });
    if (status !== 200 && status !== 201) return errorMessage(body, status);
  } catch (cause) {
    return cause instanceof Error ? cause.message : "적합성 프로필 저장에 실패했습니다.";
  }
  return undefined;
}
