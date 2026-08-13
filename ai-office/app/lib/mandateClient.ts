/**
 * Mandate 최초 생성/갱신 — `POST /ui/mandates` + `POST /ui/mandates/{id}/change-requests`.
 *
 * 근거: docs/02-engineering/USER_INPUT_API_SPEC.md 2.2
 *
 * ## 왜 PUT이 아니라 두 POST 조합인가
 *
 * §2.2가 명시한다 — "신규 Route를 만들지 않는다. `POST .../change-requests`를
 * 그대로 쓴다." Mandate는 덮어쓰기 리소스가 아니라 **버전이 쌓이는** 모델이다
 * (`governance.mandate_versions`, 매번 새 version 행 + 이전 버전은
 * `effective_to`로 닫힘) — "그때 어떤 기준으로 승인됐는가"가 감사 대상이라
 * PUT의 "전체 교체" 의미론과 안 맞는다.
 *
 * 그래서 최초 입력과 이후 수정이 이미 같은 메커니즘이다: 둘 다 "새 정책을
 * 제안"하는 것이고, 차이는 Mandate 껍데기가 있냐 없냐뿐이다.
 *
 * ## 왜 갱신 시 기존 정책을 반드시 함께 보내는가
 *
 * `previous_policy`를 생략하면 서버(`service.py propose_version`)가 무조건
 * `NEUTRAL`로 분류한다 — 실제로 한도를 완화(LOOSEN)하는 변경이어도 Risk/QA
 * 검토(`AWAITING_REVIEW`) 없이 즉시 적용(`FAST_APPLIED`)돼 버린다. F01이 요구하는
 * "장중 Risk 확대는 사용자 재승인 필요"를 깨는 것이다. 그래서 기존 Version이
 * 있으면 반드시 조회해서 `previous_policy`로 함께 보낸다.
 *
 * **최초 활성화 자체는 서버가 따로 강제한다** — `change_workflow.py`가
 * `current_version`을 DB에서 직접 확인해 0이면 direction과 무관하게 항상
 * `AWAITING_REVIEW`로 보낸다("최초 활성화는 항상 검토 필요"). 그래서
 * `previous_policy: undefined`로 첫 제출을 보내도 조용히 스킵되지 않는다.
 */

import { BFF } from "./ceoClient";
import {
  presetFor,
  findConstraintViolations,
  FIXED_POLICY_VALUES,
  type Experience,
  type Mindset,
  type MandatePreset,
  type PolicyConstraintViolation,
} from "./mandatePresets";
import { currentFundId, readStoredAccount, withAccountHeaders } from "./currentAccount";
import type { AssetClassId, MandateDraft, RiskProfile } from "../mandate/MandateConfig";

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

/**
 * 이 화면은 투자 경험을 따로 묻지 않는다(`USER_INPUT_SPEC.md` §2의 온보딩
 * 2단계용 문항이지, 이 구버전 단일 화면 설계엔 없다). 프리셋은 경험×성향
 * 9칸이므로 값이 하나 필요한데, 임의로 낮추거나 높이지 않고 **중간값으로
 * 고정**한다 — 안전 방향 중 어느 쪽으로도 치우치지 않는 선택이다. 이 화면에
 * 경험 질문이 추가되면 여기만 고치면 된다.
 */
const PLACEHOLDER_EXPERIENCE: Experience = "INTERMEDIATE";

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
}

/**
 * 실제로 전송될 4개 한도값. `max_instrument_weight`/`max_gross_exposure`는
 * 화면이 직접 받은 값으로 프리셋을 덮어쓴다 - §3.1의 은닉 대상은
 * `max_sector_weight`/`max_concurrent_positions` 둘뿐이다.
 *
 * `draftToPolicy`와 `validateDraft`가 **같은 이 함수**를 쓰는 이유: 각자
 * 따로 값을 조합하면 한쪽만 고쳤을 때 "검증은 통과했는데 실제로 보낸 값은
 * 다르다"는 어긋남이 생긴다 — 방금 실제로 이 어긋남 때문에 서버가 422를
 * 냈다(슬라이더 30% vs 프리셋 25%, 검증은 프리셋끼리만 봐서 못 잡았다).
 */
function effectiveLimits(draft: MandateDraft): MandatePreset {
  const mindset = MINDSET_BY_RISK_PROFILE[draft.riskProfile];
  const preset = presetFor(mindset, PLACEHOLDER_EXPERIENCE);
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

export class MandateSubmissionError extends Error {}

interface MandateLookup {
  mandateId: string;
  /** 기존 활성 정책. 첫 제출(Version 없음)이면 `null`. */
  previousPolicy: MandatePolicyPayload | null;
}

async function bffJson<T>(path: string, init?: RequestInit): Promise<{ status: number; body: T | null }> {
  let response: Response;
  try {
    response = await fetch(`${BFF}${path}`, {
      ...init,
      cache: "no-store",
      headers: withAccountHeaders({
        Accept: "application/json",
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...init?.headers,
      }),
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
 * 이 Fund의 mandate_id를 찾는다. 없으면 만든다.
 *
 * 409(한 Fund에 Mandate 2개 이상, 모호함)는 여기서 하나를 임의로 고르지 않고
 * 그대로 에러로 던진다 - USER_INPUT_API_SPEC 2.1이 정한 원칙과 같다.
 */
async function lookupOrCreateMandate(fundId: string, ownerUserId: string): Promise<MandateLookup> {
  const { status, body } = await bffJson<{
    mandate_id: string;
    current_version: number;
    policy?: MandatePolicyPayload;
  }>(`/ui/mandates/by-fund/${fundId}/current`);

  if (status === 200 && body) {
    return {
      mandateId: body.mandate_id,
      previousPolicy: body.current_version > 0 && body.policy ? body.policy : null,
    };
  }
  if (status !== 404) {
    throw new MandateSubmissionError(errorMessage(body, status));
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
  return { mandateId: created.body.mandate_id, previousPolicy: null };
}

export interface MandateSubmitResult {
  /** 즉시 적용됐는지, Risk/QA/사용자 승인 대기로 넘어갔는지. */
  stage: "FAST_APPLIED" | "AWAITING_REVIEW" | string;
  version: number;
  caseId: string | null;
}

/**
 * 지침 제출 버튼의 진입점. `currentAccount`의 선택된 계정을 그대로 쓴다 -
 * 호출부가 user_id를 따로 넘기지 않는 이유는 `withAccountHeaders`와 같다:
 * 한 곳에서만 계정을 읽어야 다른 사용자로 잘못 나가는 경로가 안 생긴다.
 */
export async function submitMandateDraft(
  draft: MandateDraft,
  objectiveText: string,
): Promise<MandateSubmitResult> {
  // 네트워크를 타기 전에 먼저 막는다 - 서버가 어차피 같은 제약으로 거절하지만,
  // 여기서 잡으면 어느 슬라이더가 문제인지 구체적으로 말해줄 수 있다.
  const violations = validateDraft(draft);
  if (violations.length > 0) {
    throw new MandateSubmissionError(
      `제출 전 정책 검증 실패: ${violations.map((v) => `${v.rule} (${v.detail})`).join(", ")}`,
    );
  }

  const fundId = currentFundId();
  if (!fundId) {
    throw new MandateSubmissionError(
      "현재 계정에 연결된 Fund가 없습니다. 계정을 전환하거나 관리자에게 문의하세요.",
    );
  }
  const account = readStoredAccount();
  const { mandateId, previousPolicy } = await lookupOrCreateMandate(fundId, account.userId);

  const nowIso = new Date().toISOString();
  const { status, body } = await bffJson<{
    stage: string;
    version: number;
    case_id: string | null;
  }>(`/ui/mandates/${mandateId}/change-requests`, {
    method: "POST",
    body: JSON.stringify({
      fund_id: fundId,
      policy: draftToPolicy(draft),
      objective_text: objectiveText,
      objective: {},
      effective_from: nowIso,
      // Case 감사 표지(자유 텍스트)와 mandate_versions.created_by(uuid FK)는
      // 컬럼 타입이 달라 분리한다 - change_workflow.submit() 계약과 같은 이유.
      created_by: account.userId,
      version_created_by: account.userId,
      trace_id: crypto.randomUUID(),
      now: nowIso,
      previous_policy: previousPolicy,
    }),
  });
  if (status !== 200 && status !== 201 || !body) {
    throw new MandateSubmissionError(errorMessage(body, status));
  }
  return { stage: body.stage, version: body.version, caseId: body.case_id };
}
