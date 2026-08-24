"use client";

import { useQuery } from "@tanstack/react-query";
import { loadMandateForFund, type MandatePolicyPayload, type StoredMandate } from "../lib/mandateClient";
import { usePortfolioSession } from "../lib/PortfolioSessionProvider";
import { DEFAULT_ACCOUNT } from "../lib/currentAccount";

type RiskBounds = MandatePolicyPayload["risk_bounds"];
type UniversePolicy = MandatePolicyPayload["universe_policy"];
type ApprovalRules = MandatePolicyPayload["approval_rules"];

const PRINCIPLES = [
  ["policy", "Mandate가 허용 범위를 정합니다", "사용자 목표가 구조화된 Mandate Version으로 저장되면 허용 시장·자산군·종목·섹터·거래시간이 주문의 첫 번째 경계가 됩니다."],
  ["account_balance", "위험 노출은 주문 후 상태로 계산합니다", "새 주문만 보지 않고 현재 포지션과 대기 중인 노출을 함께 반영해 종목·섹터·전체 Gross Exposure가 한도를 넘는지 확인합니다."],
  ["rule", "축소 가능한 초과와 차단을 구분합니다", "Notional·섹터·집중도·회전율처럼 수량을 줄여 통과할 수 있는 경우와 유니버스·Restricted List·손실·거래상태 위반처럼 즉시 차단해야 하는 경우를 구분합니다."],
  ["approval", "위험 확대는 사용자 승인으로 묶습니다", "Mandate보다 느슨한 한도나 자동 실행으로 바꾸는 것은 기존 주문에 조용히 적용하지 않고 새 정책 버전과 사용자 재승인을 요구합니다."],
] as const;

const CHECK_STEPS = [
  ["01", "데이터 신선도", "시점 고정 시세와 시장 상태가 유효한지 확인"],
  ["02", "Mandate 범위", "허용 종목·자산군·섹터·거래시간 밖인지 확인"],
  ["03", "주문 가능성", "Restricted List, 가격·수량·Notional, 현금·Buying Power 확인"],
  ["04", "누적 위험", "섹터·Issuer 집중도·회전율·동시 포지션·Gross 노출 확인"],
  ["05", "손실·거래 상태", "일일 손실·Drawdown·Trading State·브로커 상태 확인"],
] as const;

function percent(value: string | number | null | undefined): string {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return `${new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 }).format(number * 100)}%`;
}

function capital(value: string | number | null | undefined, currencyCode: string | undefined): string {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  const currency = currencyCode && /^[A-Z]{3}$/.test(currencyCode) ? currencyCode : "KRW";
  try {
    return new Intl.NumberFormat("ko-KR", { style: "currency", currency, maximumFractionDigits: 0 }).format(number);
  } catch {
    return `${new Intl.NumberFormat("ko-KR").format(number)} ${currency}`;
  }
}

function listValue(list: string[] | undefined, emptyLabel: string): string {
  return list && list.length > 0 ? list.join(" · ") : emptyLabel;
}

function Limit({ label, value }: { label: string; value: string }) {
  return <div className="rounded-md border border-outline-variant bg-surface px-3 py-2.5"><span className="block text-body-sm text-on-surface-variant">{label}</span><strong className="mt-0.5 block font-data-mono text-body-md text-on-surface">{value}</strong></div>;
}

function Snapshot({ mandate }: { mandate: StoredMandate | null }) {
  const policy = mandate?.policy;
  const bounds: RiskBounds | undefined = policy?.risk_bounds;
  const universe: UniversePolicy | undefined = policy?.universe_policy;
  const approval: ApprovalRules | undefined = policy?.approval_rules;

  if (!mandate) return <div className="rounded-lg border border-error/40 bg-error-container px-4 py-3"><strong className="block text-body-sm text-on-error-container">현재 Fund의 저장된 Mandate가 없습니다.</strong><p className="m-0 mt-1 text-body-sm leading-5 text-on-error-container">아래 원칙은 설명용이며, 실제 주문에 적용할 정책은 Mandate Configuration에서 저장된 뒤 Risk Gate에 전달됩니다.</p></div>;
  if (!policy || !bounds || !universe || !approval) return <div className="rounded-lg border border-error/40 bg-error-container px-4 py-3"><strong className="block text-body-sm text-on-error-container">Mandate는 찾았지만 정책 snapshot이 완전하지 않습니다.</strong><p className="m-0 mt-1 text-body-sm leading-5 text-on-error-container">위험 한도를 임의로 보충하지 않습니다. 정책 Version을 확인할 때까지 실제 판정은 서버 Risk Gate가 담당합니다.</p></div>;

  return <div className="space-y-4">
    <div className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-outline-variant bg-surface p-4"><div className="min-w-0"><h4 className="m-0 text-body-md font-bold text-on-surface">현재 사용자 Mandate</h4><strong className="mt-1 block text-body-lg text-primary">{mandate.objectiveText || "투자 목표가 입력되지 않았습니다."}</strong></div></div>
    <div><h4 className="m-0 text-body-md font-bold text-on-surface">현재 적용 중인 Mandate 한도</h4><div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-3"><Limit label="기준 자본" value={capital(bounds.base_capital, bounds.currency)} /><Limit label="종목 최대 비중" value={percent(bounds.max_instrument_weight)} /><Limit label="섹터 최대 비중" value={percent(bounds.max_sector_weight)} /><Limit label="최대 Gross Exposure" value={percent(bounds.max_gross_exposure)} /><Limit label="동시 보유 Position" value={`${bounds.max_concurrent_positions}개`} /><Limit label="일일 손실 / 최대 Drawdown" value={`${percent(bounds.max_daily_loss)} / ${percent(bounds.max_drawdown_pct)}`} /></div></div>
    <div className="grid grid-cols-1 gap-3 lg:grid-cols-2"><div className="rounded-lg border border-outline-variant bg-surface p-4"><h4 className="m-0 text-body-md font-bold text-on-surface">Mandate 유니버스</h4><dl className="mt-3 grid grid-cols-1 gap-2 text-xs leading-5"><div className="flex justify-between gap-2 border-b border-outline-variant/70 pb-2"><dt className="text-on-surface-variant">허용 시장</dt><dd className="m-0 font-data-mono text-on-surface">{listValue(universe.allowed_markets, "기본 시장 없음")}</dd></div><div className="flex justify-between gap-2 border-b border-outline-variant/70 pb-2"><dt className="text-on-surface-variant">허용 자산군</dt><dd className="m-0 text-right font-data-mono text-on-surface">{listValue(universe.allowed_asset_classes, "기본 유니버스")}</dd></div><div className="flex justify-between gap-2 border-b border-outline-variant/70 pb-2"><dt className="text-on-surface-variant">금지 자산군</dt><dd className="m-0 text-right font-data-mono text-on-surface">{listValue(universe.forbidden_asset_classes, "없음")}</dd></div><div className="flex justify-between gap-2 border-b border-outline-variant/70 pb-2"><dt className="text-on-surface-variant">제외 섹터</dt><dd className="m-0 text-right font-data-mono text-on-surface">{listValue(universe.excluded_sectors, "없음")}</dd></div><div className="flex justify-between gap-2"><dt className="text-on-surface-variant">거래 시간</dt><dd className="m-0 font-data-mono text-on-surface">{universe.trading_start}–{universe.trading_end}</dd></div></dl></div><div className="rounded-lg border border-outline-variant bg-surface p-4"><h4 className="m-0 text-body-md font-bold text-on-surface">실행·승인 기준</h4><dl className="mt-3 grid grid-cols-1 gap-2 text-xs leading-5"><div className="flex justify-between gap-2 border-b border-outline-variant/70 pb-2"><dt className="text-on-surface-variant">Paper 주문</dt><dd className="m-0 font-semibold text-on-surface">{approval.paper_order_mode === "AUTO" ? "자동 Paper 실행" : "사용자 승인 후 실행"}</dd></div><div className="flex justify-between gap-2 border-b border-outline-variant/70 pb-2"><dt className="text-on-surface-variant">Risk 확대 재승인</dt><dd className="m-0 font-semibold text-on-surface">{approval.risk_expansion_requires_user_approval ? "필요" : "불필요"}</dd></div><div className="flex justify-between gap-2"><dt className="text-on-surface-variant">종목 직접 허용 목록</dt><dd className="m-0 text-right font-data-mono text-on-surface">{listValue(policy.allowed_assets, "없음")}</dd></div></dl></div></div>
  </div>;
}

export default function RiskMandatePanel() {
  const { activeFundId, profile } = usePortfolioSession();
  const defaultFundId = DEFAULT_ACCOUNT.fundId;
  const mandateFundId = profile?.funds.some((fund) => fund.fundId === defaultFundId) ? defaultFundId : activeFundId;
  const query = useQuery<StoredMandate | null, Error>({ queryKey: ["risk-mandate-snapshot", mandateFundId], queryFn: () => loadMandateForFund(mandateFundId as string), enabled: Boolean(mandateFundId), staleTime: 30_000, refetchInterval: 30_000, retry: false });
  const loading = Boolean(mandateFundId) && query.isPending;
  const error = query.error?.message ?? "";
  return <section className="min-w-0 overflow-hidden rounded-lg border border-outline-variant bg-surface-container-lowest shadow-sm" aria-labelledby="risk-mandate-title">
    <div className="flex items-center justify-between gap-3 border-b border-outline-variant bg-surface-container-low px-4 py-2.5"><span className="flex min-w-0 items-center gap-2 text-label-md font-label-md text-on-surface-variant"><span className="material-symbols-outlined text-[16px]" aria-hidden="true">health_and_safety</span><span id="risk-mandate-title" className="truncate">risk.mandate_guardrails</span></span></div>
    <div className="space-y-5 p-4 md:p-6">
      {error ? <p role="alert" className="m-0 rounded border border-error/40 bg-error-container px-3 py-2 text-body-sm text-on-error-container">현재 사용자 Mandate를 조회하지 못했습니다: {error}</p> : null}
      {!error && !mandateFundId ? <p role="status" className="m-0 rounded border border-outline-variant bg-surface-container px-3 py-2 text-body-sm text-on-surface-variant">현재 선택된 Fund가 없어 Mandate snapshot을 조회할 수 없습니다.</p> : null}
      {!error && loading ? <p role="status" className="m-0 rounded border border-outline-variant bg-surface-container px-3 py-2 text-body-sm text-on-surface-variant">현재 Fund의 Mandate와 Risk Bounds를 조회하는 중입니다…</p> : null}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">{PRINCIPLES.map(([icon, title, body]) => <article key={title} className="rounded-lg border border-outline-variant bg-surface p-4"><div className="flex items-start gap-3"><span className="material-symbols-outlined mt-0.5 text-[20px] text-primary" aria-hidden="true">{icon}</span><div className="min-w-0"><h4 className="m-0 text-body-md font-bold text-on-surface">{title}</h4><p className="m-0 mt-1.5 text-body-sm leading-6 text-on-surface-variant">{body}</p></div></div></article>)}</div>
      {!loading && !error && mandateFundId ? <Snapshot mandate={query.data ?? null} /> : null}
      <div><div className="flex flex-wrap items-baseline justify-between gap-2"><div><h4 className="m-0 text-body-md font-bold text-on-surface">주문 요청이 들어오면 이렇게 확인합니다</h4><p className="m-0 mt-1 text-body-sm leading-5 text-on-surface-variant">Mandate와 현재 계좌 상태를 순서대로 확인하며, 기준을 하나라도 충족하지 못하면 주문을 그대로 실행하지 않습니다.</p></div></div><div className="mt-3 grid grid-cols-1 gap-2 md:grid-cols-5">{CHECK_STEPS.map(([number, title, body]) => <article key={number} className="rounded-lg border border-outline-variant bg-surface p-3 md:min-h-[132px]"><span className="font-data-mono text-body-sm font-bold text-primary">{number}</span><h5 className="m-0 mt-1 text-body-sm font-bold text-on-surface">{title}</h5><p className="m-0 mt-1 text-body-sm leading-5 text-on-surface-variant">{body}</p></article>)}</div></div>
    </div>
  </section>;
}
