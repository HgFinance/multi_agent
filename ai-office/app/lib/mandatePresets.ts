/**
 * 온보딩 프리셋 — 투자 경험 3 × 투자 성향 3 = 9칸.
 *
 * 근거: docs/01-product/USER_INPUT_SPEC.md 3(계층 2 - 프리셋 자동 채움)
 *       docs/02-engineering/USER_INPUT_API_SPEC.md 2.2(온보딩 제출)
 *
 * Risk API의 버전 프리셋이 정본이다. 이 파일의 숫자는 Risk API가
 * 일시적으로 불가용한 경우에만 쓰는 동일 버전 fail-safe 복사본이며,
 * `installAuthoritativePresets()`가 3×3 ACTIVE 행렬을 설치하면 즉시 대체된다.
 *
 * 잠정값을 어떻게 골랐나: 위험 판단을 새로 만들지 않고, 스펙에 **이미 있는 유일한
 * 규칙**에서만 끌어냈다 — `effective_risk_score = min(mindset, experience)`
 * (`suitability.py`). 그래서 9칸이 실질적으로 3개 등급으로 수렴한다. 이건 숨길
 * 것이 아니라 동규님께 드리는 질문이다: **경험과 성향이 min() 말고 다른 방식으로도
 * 한도에 영향을 줘야 하는가?** 그렇다면 9칸을 서로 다르게 채워야 하고, 아니라면
 * 표를 3줄로 줄일 수 있다.
 *
 * 확정되면 이 파일의 수치만 바꾸면 된다 — 구조와 검증은 그대로 쓸 수 있다.
 */

export type Mindset = "SAFETY_FIRST" | "BALANCED" | "RISK_SEEKING";
export type Experience = "BEGINNER" | "INTERMEDIATE" | "EXPERIENCED";

/** USER_INPUT_SPEC 3.1이 프리셋으로 채우는 4개 필드. */
export interface MandatePreset {
  /** 종목 하나의 최대 비중. 0~1 분수. */
  max_instrument_weight: string;
  /** 업종 하나의 최대 비중. 0~1 분수. 사용자 설정값이며 프리셋은 기본값이다(**D**). */
  max_sector_weight: string;
  /** 총 노출. **1.0을 넘을 수 있다**(레버리지). */
  max_gross_exposure: string;
  /** 동시 보유 종목 수 상한. */
  max_concurrent_positions: number;
  /** Risk-owned daily loss fraction. */
  max_daily_loss_pct?: string;
  /** Risk-owned portfolio drawdown fraction. */
  max_drawdown_pct?: string;
  /** Per-trade position loss-budget range. */
  trade_risk_budget_min_pct?: string;
  trade_risk_budget_max_pct?: string;
}

/** `suitability.py` `_MINDSET_SCORE`와 같은 순서. 표를 복제하지 않도록 점수만 맞춘다. */
const MINDSET_SCORE: Record<Mindset, 1 | 2 | 3> = {
  SAFETY_FIRST: 1,
  BALANCED: 2,
  RISK_SEEKING: 3,
};

/** `suitability.py` `_EXPERIENCE_SCORE`와 같은 순서. */
const EXPERIENCE_SCORE: Record<Experience, 1 | 2 | 3> = {
  BEGINNER: 1,
  INTERMEDIATE: 2,
  EXPERIENCED: 3,
};

/**
 * 실질 위험 등급별 잠정 한도. `min(mindset, experience)` 결과로 고른다.
 *
 * **PROVISIONAL** — 동규님 확정 전. 아래 값은 USER_INPUT_SPEC 3.2의 결정론 제약을
 * 만족하고 등급 간 단조성(초보 칸이 고수 칸보다 공격적이지 않음)을 지키지만,
 * 그것만으로 "적정하다"는 뜻은 아니다.
 */
const PROVISIONAL_BY_RISK_SCORE: Record<1 | 2 | 3, MandatePreset> = {
  // LOW — min()이 1인 모든 칸 (안정추구이거나 초보인 경우)
  1: {
    max_instrument_weight: "0.10",
    max_sector_weight: "0.25",
    // 2026-08-13: 0.80(레버리지 없이도 원금의 80%만 투자)이었는데, 화면
    // 슬라이더(MandateConfig.tsx 최대 위험 노출액)의 최소값이 100%로 좁혀지면서
    // 이 값이 슬라이더가 표현할 수 없는 범위가 됐다. 가장 보수적인 등급의
    // "레버리지 없음" 기본값은 1.00(원금만큼만 투자)이 의미상으로도 더 맞다.
    max_gross_exposure: "1.00",
    max_concurrent_positions: 5,
    max_daily_loss_pct: "0.02",
    max_drawdown_pct: "0.15",
    trade_risk_budget_min_pct: "0.0025",
    trade_risk_budget_max_pct: "0.0050",
  },
  // MEDIUM
  2: {
    max_instrument_weight: "0.15",
    max_sector_weight: "0.35",
    max_gross_exposure: "1.50",
    max_concurrent_positions: 8,
    max_daily_loss_pct: "0.03",
    max_drawdown_pct: "0.20",
    trade_risk_budget_min_pct: "0.0050",
    trade_risk_budget_max_pct: "0.0100",
  },
  // HIGH — gross가 1.0을 넘는 유일한 등급
  3: {
    max_instrument_weight: "0.25",
    max_sector_weight: "0.50",
    max_gross_exposure: "2.50",
    max_concurrent_positions: 12,
    max_daily_loss_pct: "0.05",
    max_drawdown_pct: "0.35",
    trade_risk_budget_min_pct: "0.0100",
    trade_risk_budget_max_pct: "0.0200",
  },
};

let authoritativePresets: Map<string, MandatePreset> | null = null;
let authoritativePresetVersion = "risk-mandate-presets.2026-08-25.v1";

export interface RiskPresetApiResponse {
  schema_version: "risk.mandate-presets.v1";
  preset_version: string;
  status: "ACTIVE";
  presets: Array<MandatePreset & { mindset: Mindset; experience: Experience }>;
}

/** Install only a complete 3x3 ACTIVE matrix returned by the Risk API. */
export function installAuthoritativePresets(response: RiskPresetApiResponse): void {
  if (response.status !== "ACTIVE" || response.presets.length !== 9) {
    throw new Error("Risk preset response must contain one ACTIVE 3x3 matrix");
  }
  const matrix = new Map<string, MandatePreset>();
  for (const row of response.presets) {
    matrix.set(`${row.experience}:${row.mindset}`, { ...row });
  }
  if (matrix.size !== 9) throw new Error("Risk preset response contains duplicate cells");
  authoritativePresets = matrix;
  authoritativePresetVersion = response.preset_version;
}

/** 이 파일의 수치가 확정 전임을 화면이 표시할 수 있게 노출한다. */
export const PRESETS_ARE_PROVISIONAL = false;
export const PRESETS_PENDING_OWNER = "Risk API — risk-mandate-presets.2026-08-25.v1";

/** Version bound to the values currently displayed by the UI. */
export function activePresetVersion(): string {
  return authoritativePresetVersion.replace(/^Risk API — /, "");
}

/**
 * USER_INPUT_SPEC 3.4 — 사용자에게 묻지도, 프리셋으로 다루지도 않는 고정값.
 *
 * `risk_expansion_requires_user_approval`이 `true`인 것은 기본값 유지가 안전
 * 방향이기 때문이다(개발 원칙 9).
 */
export const FIXED_POLICY_VALUES = {
  allowed_markets: ["KRX"] as const,
  trading_start: "09:00",
  trading_end: "15:30",
  risk_expansion_requires_user_approval: true,
} as const;

/** 성향·경험 조합의 프리셋. */
export function presetFor(mindset: Mindset, experience: Experience): MandatePreset {
  const authoritative = authoritativePresets?.get(`${experience}:${mindset}`);
  if (authoritative) return { ...authoritative };
  const score = Math.min(
    MINDSET_SCORE[mindset],
    EXPERIENCE_SCORE[experience],
  ) as 1 | 2 | 3;
  return { ...PROVISIONAL_BY_RISK_SCORE[score] };
}

/**
 * 실질 위험 등급. **화면이 이 값을 판정 근거로 쓰지 않는다.**
 *
 * USER_INPUT_API_SPEC 2.3이 "화면이 재계산하지 않는다"고 못박았으므로, 저장 후
 * 실제 등급은 `POST /ui/investor-profiles` 응답의 `effective_risk_band`를 쓴다.
 * 이 함수는 프리셋을 고르기 위한 내부용이고, 사용자에게 등급을 보여줄 때는
 * 서버 응답을 쓴다 — 두 값이 갈라지면 서버가 맞다.
 */
export function provisionalRiskScore(mindset: Mindset, experience: Experience): 1 | 2 | 3 {
  return Math.min(MINDSET_SCORE[mindset], EXPERIENCE_SCORE[experience]) as 1 | 2 | 3;
}

/**
 * 성향 선택 시 화면 슬라이더 4개에 채울 초기값.
 *
 * **`MandatePreset`과 다른 개념이다** — `MandatePreset`은 USER_INPUT_SPEC 3.1이
 * 정의한 "사용자에게 안 묻고 숨기는 2개 필드"(max_sector_weight,
 * max_concurrent_positions)용이고, `max_instrument_weight`/`max_gross_exposure`는
 * 거기서도 실제 제출 시(`mandateClient.ts`) 항상 화면 슬라이더 값으로
 * 덮어써진다 — 스펙상 이 둘은 "직접 선택" 항목이다(§2 5·... 항목류와 같은 층).
 * `max_drawdown_pct`/`max_daily_loss`도 §2 6·7번으로 직접 선택 항목이라
 * `MandatePreset`에는 아예 없다.
 *
 * 그런데도 성향을 고르면 이 4개 슬라이더가 그럴듯한 시작값으로 바뀌길
 * 원한다면(요구사항: "선택하면 프론트에 보이는 기본값이 바뀌게"), 그 시작값도
 * 등급별로 어딘가에 정의돼 있어야 한다 — 그래서 별도 테이블을 둔다. 사용자는
 * 이후 슬라이더를 자유롭게 더 움직일 수 있고, 최종 제출값은 여전히 그 슬라이더
 * 값이다(§3.3 "은닉은 화면의 표현일 뿐 전송 생략이 아니다"와 같은 원칙 —
 * 여기서는 "제안 기본값일 뿐 강제가 아니다").
 *
 * **등급 산출은 `provisionalRiskScore()`를 그대로 재사용한다.** 이 화면엔
 * 투자 경험을 따로 묻는 문항이 없어 `PLACEHOLDER_EXPERIENCE`(INTERMEDIATE)로
 * 고정하는데(`mandateClient.ts`), 여기서 독자적으로 등급을 다시 계산하면 화면
 * 기본값과 실제 제출 시 숨김 필드(§3.1 2개)가 서로 다른 등급을 가리킬 수
 * 있다 - 슬라이더-프리셋 어긋남으로 422가 났던 사고(2026-08-12)와 같은
 * 종류의 문제라 반드시 같은 함수로 등급을 낸다.
 *
 * **PROVISIONAL** — `PROVISIONAL_BY_RISK_SCORE`와 마찬가지로 동규님 확정
 * 전이다. `auditSliderDefaults()`가 9칸 전부 결정론 제약을 지키는지 검증한다.
 */
export interface SliderDefaults {
  maxSingleWeightPct: number;
  grossExposurePct: number;
  maxDrawdownPct: number;
  maxDailyLossPct: number;
}

const SLIDER_DEFAULTS_BY_RISK_SCORE: Record<1 | 2 | 3, SliderDefaults> = {
  1: { maxSingleWeightPct: 10, grossExposurePct: 100, maxDrawdownPct: 15, maxDailyLossPct: 2 },
  2: { maxSingleWeightPct: 15, grossExposurePct: 150, maxDrawdownPct: 20, maxDailyLossPct: 3 },
  3: { maxSingleWeightPct: 25, grossExposurePct: 250, maxDrawdownPct: 35, maxDailyLossPct: 5 },
};

export function sliderDefaultsFor(mindset: Mindset, experience: Experience): SliderDefaults {
  const score = provisionalRiskScore(mindset, experience);
  const fallback = SLIDER_DEFAULTS_BY_RISK_SCORE[score];
  const preset = presetFor(mindset, experience);
  return {
    maxSingleWeightPct: Number(preset.max_instrument_weight) * 100,
    grossExposurePct: Number(preset.max_gross_exposure) * 100,
    maxDrawdownPct: preset.max_drawdown_pct
      ? Number(preset.max_drawdown_pct) * 100
      : fallback.maxDrawdownPct,
    maxDailyLossPct: preset.max_daily_loss_pct
      ? Number(preset.max_daily_loss_pct) * 100
      : fallback.maxDailyLossPct,
  };
}

export interface PolicyConstraintViolation {
  rule: string;
  detail: string;
}

/**
 * USER_INPUT_SPEC 3.2의 결정론 제약을 화면에서 먼저 확인한다.
 *
 * 서버(`policy.py`)가 같은 검증을 하고 위반 시 거절하므로 이건 이중 검사다. 그래도
 * 두는 이유: 고급 설정에서 사용자가 프리셋을 벗어난 값을 넣을 수 있고, 그때 422를
 * 받고 나서야 알려주면 무엇이 틀렸는지 화면이 설명할 수 없다.
 *
 * **여기서 값을 고쳐주지 않는다.** 위반을 조용히 완화하는 것은 USER_INPUT_SPEC
 * 4.1이 LLM에 금지한 것과 같은 이유로 금지다 — 사용자가 정하지 않은 한도가
 * 정책이 된다.
 *
 * `maxDailyLoss`/`maxDrawdown`은 프리셋이 아니라 사용자 슬라이더 값이지만
 * (`max_daily_loss <= max_drawdown_pct`가 3.2의 신규 제약이라) 같이 받는다.
 */
export function findConstraintViolations(input: {
  preset: MandatePreset;
  maxDailyLoss?: string;
  maxDrawdownPct?: string;
}): PolicyConstraintViolation[] {
  const violations: PolicyConstraintViolation[] = [];
  const instrument = Number(input.preset.max_instrument_weight);
  const sector = Number(input.preset.max_sector_weight);
  const gross = Number(input.preset.max_gross_exposure);
  const positions = input.preset.max_concurrent_positions;

  if (!(instrument <= sector && sector <= gross)) {
    violations.push({
      rule: "max_instrument_weight <= max_sector_weight <= max_gross_exposure",
      detail: `${instrument} / ${sector} / ${gross}`,
    });
  }
  // gross는 레버리지라 1.0 초과가 허용된다. 나머지 비중은 아니다.
  for (const [name, value] of [
    ["max_instrument_weight", instrument],
    ["max_sector_weight", sector],
  ] as const) {
    if (!(value > 0 && value <= 1)) {
      violations.push({ rule: `0 < ${name} <= 1`, detail: String(value) });
    }
  }
  if (!(gross > 0)) {
    violations.push({ rule: "max_gross_exposure > 0", detail: String(gross) });
  }
  if (!(positions > 0)) {
    violations.push({ rule: "max_concurrent_positions > 0", detail: String(positions) });
  }
  if (input.maxDailyLoss !== undefined && input.maxDrawdownPct !== undefined) {
    const daily = Number(input.maxDailyLoss);
    const drawdown = Number(input.maxDrawdownPct);
    if (!(daily <= drawdown)) {
      violations.push({
        rule: "max_daily_loss <= max_drawdown_pct",
        detail: `${daily} > ${drawdown}`,
      });
    }
  }
  return violations;
}

/**
 * 이 파일의 잠정값 자체가 제약을 만족하는지 확인한다.
 *
 * 동규님이 수치를 채울 때 실수로 제약을 깨면 **화면이 조용히 잘못된 정책을 보내는
 * 대신 여기서 드러나야 한다.** 모듈 로드 시점에 던지지 않고 목록으로 돌려주는
 * 이유는, 화면이 이 결과를 개발용 경고로 띄울 수 있게 하기 위해서다.
 */
export function auditProvisionalPresets(): {
  mindset: Mindset;
  experience: Experience;
  violations: PolicyConstraintViolation[];
}[] {
  const mindsets: Mindset[] = ["SAFETY_FIRST", "BALANCED", "RISK_SEEKING"];
  const experiences: Experience[] = ["BEGINNER", "INTERMEDIATE", "EXPERIENCED"];
  const failures: {
    mindset: Mindset;
    experience: Experience;
    violations: PolicyConstraintViolation[];
  }[] = [];
  for (const mindset of mindsets) {
    for (const experience of experiences) {
      const violations = findConstraintViolations({
        preset: presetFor(mindset, experience),
      });
      if (violations.length > 0) failures.push({ mindset, experience, violations });
    }
  }
  return failures;
}

/**
 * `SLIDER_DEFAULTS_BY_RISK_SCORE`가 결정론 제약을 지키는지 확인한다.
 *
 * `max_instrument_weight`/`max_gross_exposure`는 슬라이더 기본값에서,
 * `max_sector_weight`/`max_concurrent_positions`는 `MandatePreset`에서 가져와
 * 하나로 합친 뒤 검증한다 - 실제 제출 시에도 정확히 이렇게 섞이기 때문이다
 * (`mandateClient.ts` `effectiveLimits()`와 같은 조합).
 */
export function auditSliderDefaults(): {
  mindset: Mindset;
  experience: Experience;
  violations: PolicyConstraintViolation[];
}[] {
  const mindsets: Mindset[] = ["SAFETY_FIRST", "BALANCED", "RISK_SEEKING"];
  const experiences: Experience[] = ["BEGINNER", "INTERMEDIATE", "EXPERIENCED"];
  const failures: {
    mindset: Mindset;
    experience: Experience;
    violations: PolicyConstraintViolation[];
  }[] = [];
  for (const mindset of mindsets) {
    for (const experience of experiences) {
      const defaults = sliderDefaultsFor(mindset, experience);
      const preset = presetFor(mindset, experience);
      const violations = findConstraintViolations({
        preset: {
          max_instrument_weight: (defaults.maxSingleWeightPct / 100).toFixed(4),
          max_sector_weight: preset.max_sector_weight,
          max_gross_exposure: (defaults.grossExposurePct / 100).toFixed(4),
          max_concurrent_positions: preset.max_concurrent_positions,
        },
        maxDailyLoss: (defaults.maxDailyLossPct / 100).toFixed(4),
        maxDrawdownPct: (defaults.maxDrawdownPct / 100).toFixed(4),
      });
      if (violations.length > 0) failures.push({ mindset, experience, violations });
    }
  }
  return failures;
}
