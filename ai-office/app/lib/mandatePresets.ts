/**
 * 온보딩 프리셋 — 투자 경험 3 × 투자 성향 3 = 9칸.
 *
 * 근거: docs/01-product/USER_INPUT_SPEC.md 3(계층 2 - 프리셋 자동 채움)
 *       docs/02-engineering/USER_INPUT_API_SPEC.md 2.2(온보딩 제출)
 *
 * ## 왜 프론트엔드 상수인가
 *
 * USER_INPUT_SPEC 3.3의 결정(**H**)이다 — 서버가 관리하는 상수가 아니고, 버전 관리
 * 대상도 아니다. 화면이 값을 채워 서버로 보내고, 서버는 여전히 완전한
 * `MandatePolicy`를 받아 `policy.py`로 전 필드를 검증한다.
 * **은닉은 화면의 표현일 뿐 전송 생략이 아니다** — 사용자에게 안 보여줘도 값은 보낸다.
 *
 * ## ⚠️ 이 수치는 잠정값이다 (PROVISIONAL)
 *
 * USER_INPUT_SPEC 3.2와 8절 미확정 1번: **프리셋 9칸 수치는 동규님(리스크) 확정
 * 사항**이며 이 파일이 정하지 않는다. 아래 값은 화면·API 배선을 먼저 검증하기 위한
 * 자리표시자이고, 리스크 관점 적정성 검토를 받지 않았다.
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
    max_gross_exposure: "0.80",
    max_concurrent_positions: 5,
  },
  // MEDIUM
  2: {
    max_instrument_weight: "0.15",
    max_sector_weight: "0.35",
    max_gross_exposure: "1.00",
    max_concurrent_positions: 8,
  },
  // HIGH — gross가 1.0을 넘는 유일한 등급
  3: {
    max_instrument_weight: "0.25",
    max_sector_weight: "0.50",
    max_gross_exposure: "1.20",
    max_concurrent_positions: 12,
  },
};

/** 이 파일의 수치가 확정 전임을 화면이 표시할 수 있게 노출한다. */
export const PRESETS_ARE_PROVISIONAL = true;
export const PRESETS_PENDING_OWNER = "동규 (리스크) — USER_INPUT_SPEC.md 8절 미확정 1번";

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
