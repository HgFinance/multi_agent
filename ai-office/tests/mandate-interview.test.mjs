import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_DRAFT,
  INTERVIEW,
  applyChoice,
  applySuggestions,
  capitalUnitFor,
  draftToInvestorProfile,
  draftToPolicy,
  nextStep,
  policyToDraft,
  validateDraft,
} from "../app/lib/mandateClient.ts";

/** 대본의 `choices`에서 라벨로 하나 골라 적용한다. */
function pick(draft, stepIndex, label) {
  const choice = INTERVIEW[stepIndex].choices.find((item) => item.label === label);
  assert.ok(choice, `${stepIndex}단계에 "${label}" 선택지가 없다`);
  return applyChoice(draft, choice.patch);
}

test("공격적은 중립적과 다른 등급을 낸다 (min() 자리표시자 제거 회귀)", () => {
  // 예전엔 두 자리가 각각 INTERMEDIATE(=2)를 박아둬서 min(RISK_SEEKING=3, 2)=2가
  // 되어 공격적이 중립적과 완전히 같은 값을 냈다. 등급 3은 도달 자체가 불가능했다.
  const base = { ...DEFAULT_DRAFT, experience: "EXPERIENCED" };
  const aggressive = applyChoice(base, { riskProfile: "aggressive" });
  const neutral = applyChoice(base, { riskProfile: "neutral" });

  assert.equal(aggressive.grossExposurePct, 250);
  assert.equal(aggressive.maxDrawdownPct, 35);
  assert.equal(aggressive.maxSingleWeightPct, 25);
  assert.equal(aggressive.maxDailyLossPct, 5);
  assert.notDeepEqual(
    [aggressive.grossExposurePct, aggressive.maxDrawdownPct],
    [neutral.grossExposurePct, neutral.maxDrawdownPct],
  );
});

test("슬라이더 등급과 제출되는 숨김 필드가 같은 곳에서 나온다 (422 회귀)", () => {
  // 한쪽만 고치면 여기서 깨진다. 2026-08-12에 슬라이더 30% vs 프리셋 25%로
  // 서버가 422를 냈던 사고와 같은 종류다.
  const aggressive = applyChoice(
    { ...DEFAULT_DRAFT, experience: "EXPERIENCED" },
    { riskProfile: "aggressive" },
  );
  const bounds = draftToPolicy(aggressive).risk_bounds;

  assert.equal(bounds.max_sector_weight, "0.50", "등급 3의 숨김 필드가 아니다");
  assert.equal(bounds.max_concurrent_positions, 12);
  assert.equal(bounds.max_instrument_weight, "0.2500");
  assert.equal(bounds.max_gross_exposure, "2.5000");
  assert.deepEqual(validateDraft(aggressive), []);
});

test("경험이 성향보다 낮으면 min()으로 잘린다 - 의도된 동작이다", () => {
  const beginner = applyChoice(
    { ...DEFAULT_DRAFT, experience: "BEGINNER" },
    { riskProfile: "aggressive" },
  );
  assert.equal(beginner.grossExposurePct, 100, "초보는 등급 1로 잘려야 한다");
  assert.equal(beginner.maxDrawdownPct, 15);
});

test("등급과 무관한 선택은 슬라이더를 되돌리지 않는다", () => {
  // 폼이 잠기지 않아 사용자가 인터뷰 중에도 슬라이더를 만질 수 있다.
  const tuned = { ...DEFAULT_DRAFT, maxSingleWeightPct: 42, maxDrawdownPct: 44 };
  const after = applyChoice(tuned, { approvalMode: "auto", baseCapital: 5_000_000 });
  assert.equal(after.maxSingleWeightPct, 42);
  assert.equal(after.maxDrawdownPct, 44);
  assert.equal(after.approvalMode, "auto");
});

test("인터뷰 대본을 순서대로 적용하면 draft가 완성된다", () => {
  let draft = { ...DEFAULT_DRAFT, objective: "10년 뒤 은퇴 자금" };
  draft = pick(draft, 1, "공격적");
  draft = pick(draft, 2, "숙련");
  draft = applyChoice(draft, INTERVIEW[3].parse("10년"));
  draft = pick(draft, 4, "낮음 (당분간 없음)");
  draft = applyChoice(draft, INTERVIEW[5].parse("5,000만원"));
  draft = pick(draft, 6, "관리자 승인 필요");

  assert.equal(draft.riskProfile, "aggressive");
  assert.equal(draft.experience, "EXPERIENCED");
  assert.equal(draft.investmentHorizonYears, 10);
  assert.equal(draft.liquidityNeed, "LOW");
  assert.equal(draft.baseCapital, 50_000_000);
  assert.equal(draft.approvalMode, "manual");
  assert.deepEqual(validateDraft(draft), []);

  // 완성된 draft는 적합성 프로필로도 저장 가능해야 한다.
  const asOf = "2026-08-14T03:00:00.000Z";
  const profile = draftToInvestorProfile(draft, "user-1", "fund-1", asOf);
  assert.equal(profile.mindset, "RISK_SEEKING");
  assert.equal(profile.experience, "EXPERIENCED");
  assert.equal(profile.investment_horizon_years, 10);
  assert.equal(profile.liquidity_need, "LOW");
  assert.equal(profile.max_drawdown_pct, "0.3500");
  // `as_of`는 필수이고 타임존이 없으면 서버가 422로 거절한다(2026-08-14 실측).
  assert.equal(profile.as_of, asOf);
  assert.match(String(profile.as_of), /Z$|[+-]\d{2}:\d{2}$/, "타임존 없는 as_of는 거절당한다");
});

test("기간·유동성이 비면 적합성 프로필을 지어내지 않는다", () => {
  assert.equal(draftToInvestorProfile(DEFAULT_DRAFT, "user-1", "fund-1", "2026-08-14T03:00:00.000Z"), null);
});

test("숫자가 없거나 범위를 벗어난 답은 거절한다", () => {
  assert.equal(INTERVIEW[3].parse("일억원"), null);
  assert.equal(INTERVIEW[3].parse("0"), null, "0년은 InvestorProfileIn ge=1 위반");
  assert.equal(INTERVIEW[3].parse("200"), null, "100년 초과는 le=100 위반");
  assert.deepEqual(INTERVIEW[3].parse("10년"), { investmentHorizonYears: 10 });
  assert.equal(INTERVIEW[5].parse("만원만"), null, "숫자가 없으면 되물어야 한다");
  assert.equal(INTERVIEW[5].parse("0"), null);
});

test("기본 자산은 만원 단위로 받아 원 단위로 저장한다", () => {
  // 서버 `risk_bounds.base_capital`은 원 단위 계약이다. 화면 단위가 바뀌어도
  // 전송값은 원이어야 한다 - 여기가 어긋나면 1만 배 금액이 저장된다.
  assert.deepEqual(INTERVIEW[5].parse("10000"), { baseCapital: 100_000_000 });
  assert.deepEqual(INTERVIEW[5].parse("5,000만원"), { baseCapital: 50_000_000 });

  const draft = applyChoice(DEFAULT_DRAFT, INTERVIEW[5].parse("10000"));
  assert.equal(draftToPolicy(draft).risk_bounds.base_capital, "100000000");
});

test("만원 단위는 KRW일 때만 쓴다", () => {
  // USD인데 "만원"이라고 적혀 있으면 1만 배 오입력이 난다.
  assert.deepEqual(capitalUnitFor("KRW"), { multiplier: 10_000, label: "만원" });
  assert.deepEqual(capitalUnitFor("USD"), { multiplier: 1, label: "USD" });
  assert.equal(DEFAULT_DRAFT.baseCapital / capitalUnitFor("KRW").multiplier, 10_000, "기본 1억원 = 10,000만원");
});

test("LLM은 위험 필드를 건드릴 수 없다 (allow-list 신뢰 경계)", () => {
  const result = applySuggestions(DEFAULT_DRAFT, [
    { field: "objective_text", value: "안정적인 노후 자금 마련" },
    { field: "investment_horizon_years", value: 15 },
    { field: "liquidity_need", value: "LOW" },
    { field: "mindset", value: "RISK_SEEKING" },
    { field: "max_gross_exposure", value: "3.00" },
  ]);

  assert.equal(result.draft.objective, "안정적인 노후 자금 마련");
  assert.equal(result.draft.investmentHorizonYears, 15);
  assert.equal(result.draft.liquidityNeed, "LOW");
  assert.equal(result.draft.riskProfile, DEFAULT_DRAFT.riskProfile, "성향이 LLM으로 바뀌었다");
  assert.equal(result.draft.grossExposurePct, DEFAULT_DRAFT.grossExposurePct);
  assert.deepEqual(result.unapplied, ["mindset", "max_gross_exposure"]);
});

test("범위를 벗어난 제안은 조용히 버리지 않는다", () => {
  const result = applySuggestions(DEFAULT_DRAFT, [
    { field: "investment_horizon_years", value: 0 },
    { field: "liquidity_need", value: "SOMETIMES" },
  ]);
  assert.equal(result.draft.investmentHorizonYears, null);
  assert.equal(result.draft.liquidityNeed, null);
  assert.deepEqual(result.unapplied, ["investment_horizon_years", "liquidity_need"]);
});

test("LLM이 채운 항목은 다시 묻지 않는다", () => {
  const filled = { ...DEFAULT_DRAFT, investmentHorizonYears: 10, liquidityNeed: "LOW" };
  // 3(기간)·4(유동성)를 건너뛰고 5(자본)로 간다.
  assert.equal(nextStep(filled, 3), 5);
  // 아직 안 채웠으면 그대로 3에 머문다.
  assert.equal(nextStep(DEFAULT_DRAFT, 3), 3);
});

test("저장된 정책을 불러오면 폼이 그대로 복원된다 (계정 전환 회귀)", () => {
  let saved = applyChoice({ ...DEFAULT_DRAFT, experience: "EXPERIENCED" }, { riskProfile: "aggressive" });
  saved = {
    ...saved,
    objective: "성장주 중심 장기 보유",
    baseCapital: 250_000_000,
    currency: "KRW",
    // 사용자가 제안값에서 직접 조정한 상태. 불러오기가 이걸 등급 기본값으로
    // 되돌리면 안 된다.
    maxSingleWeightPct: 30,
    allowedAssets: { ...saved.allowedAssets, leverage: true },
  };

  const restored = policyToDraft(DEFAULT_DRAFT, draftToPolicy(saved), saved.objective);

  assert.equal(restored.objective, "성장주 중심 장기 보유");
  assert.equal(restored.baseCapital, 250_000_000);
  assert.equal(restored.currency, "KRW");
  assert.equal(restored.maxSingleWeightPct, 30, "직접 조정한 값이 기본값으로 덮였다");
  assert.equal(restored.grossExposurePct, saved.grossExposurePct);
  assert.equal(restored.maxDrawdownPct, saved.maxDrawdownPct);
  assert.equal(restored.maxDailyLossPct, saved.maxDailyLossPct);
  assert.deepEqual(restored.allowedAssets, saved.allowedAssets);
  assert.equal(restored.approvalMode, saved.approvalMode);
});

test("기본 draft는 정책 제약을 위반하지 않는다", () => {
  assert.deepEqual(validateDraft(DEFAULT_DRAFT), []);
  // KRX 시장에 USD 기본값이면 Fund 기준통화 불일치로 확정 거절당한다.
  assert.equal(DEFAULT_DRAFT.currency, "KRW");
});
