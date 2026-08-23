/**
 * 채택된 페이퍼 전략(mlpipe-paper) client — BFF `/ui/strategy-runtime/spike-fade` 폴링.
 *
 * `strategy.strategies`/`versions`/`evaluations`는 스키마만 있고 실제로 쓰는
 * 코드가 아직 없다(`apps/api/strategy_runtime.py` 머리말 참고, 2026-08-04
 * 실측 - 호출처 0개). 그래서 이 화면은 그 레지스트리를 조회하는 대신, 실제로
 * 떠 있는 컨테이너와 그 컨테이너가 호스트에 남기는 원장·팩 파일을 그대로
 * 읽는다. 전원 조작(start/stop)은 배포마다 기본이 꺼져 있고, 켜져 있을 때만
 * `/power`가 200을 준다 - 꺼져 있으면 503이고, 화면은 그걸 실패로 뭉개지
 * 않고 "이 배포에서 꺼져 있음"으로 그대로 보여준다.
 */

import { BFF, bffFetch } from "./bffClient";

export type ContainerState = {
  found: boolean;
  running: boolean;
  status?: string;
  started_at?: string | null;
  finished_at?: string | null;
  exit_code?: number | null;
  restarting?: boolean;
  detail?: string;
};

export type StrategyLedger = {
  runner_version: string;
  mode: string;
  session: string;
  candidate: string;
  pack: {
    previous_session: string;
    lambda1_gate_threshold: number;
    bar_block_maxima: number[];
  };
  closed_trades: unknown[];
  open_positions: unknown[];
  summary: {
    trades_closed: number;
    trades_open: number;
    mean_trade_bps: number | null;
    session_bps_per_slot: number;
  };
  generated_at: string;
  _source_file: string;
};

export type StrategyPack = {
  pack_version: string;
  generated_at: string;
  target_session: string;
  previous_session: string;
  training_sessions: number;
  lambda1_gate_threshold: number;
  gate_percentile: number;
  pool_sessions: string[];
  features: string[];
  warmup_features: string[];
  sealed_final_sessions_loaded: boolean;
};

export type ModelHead = {
  objective: string | null;
  num_feature: string | null;
  num_trees: string | null;
  base_score: string | null;
};

export type StrategyRuntimeSnapshot = {
  container_name: string;
  container: ContainerState;
  settings: Record<string, string>;
  control_enabled: boolean;
  ledger: StrategyLedger | null;
  pack: StrategyPack | null;
  models: { long?: ModelHead; short?: ModelHead };
};

export class StrategyRuntimeError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

function explain(body: unknown, status: number): string {
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
  }
  return `전략 컨테이너 조회 실패 (HTTP ${status})`;
}

export async function fetchStrategyRuntime(): Promise<StrategyRuntimeSnapshot> {
  let response: Response;
  try {
    response = await bffFetch("/ui/strategy-runtime/spike-fade", {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
  } catch {
    throw new StrategyRuntimeError(`BFF(${BFF})에 연결하지 못했습니다.`, 0);
  }
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new StrategyRuntimeError(explain(body, response.status), response.status);
  return body as StrategyRuntimeSnapshot;
}

/** 전원 조작. 이 배포에서 꺼져 있으면 503 그대로 던진다 - 화면이 그 사유를 보여준다. */
export async function setStrategyPower(action: "start" | "stop"): Promise<ContainerState> {
  let response: Response;
  try {
    response = await bffFetch("/ui/strategy-runtime/spike-fade/power", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ action }),
    });
  } catch {
    throw new StrategyRuntimeError(`BFF(${BFF})에 연결하지 못했습니다.`, 0);
  }
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new StrategyRuntimeError(explain(body, response.status), response.status);
  return (body as { container: ContainerState }).container;
}
