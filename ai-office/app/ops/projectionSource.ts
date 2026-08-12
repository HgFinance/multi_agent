import type { RuntimeOperations } from "../game/sim";
import type { TradingSnapshot } from "./readModel";

export type ProjectionMode = TradingSnapshot["mode"];

export type ProjectionSource =
  | { mode: "DEMO"; kind: "simulation" }
  | { mode: "PAPER" | "LIVE"; kind: "backend"; snapshot: TradingSnapshot };

/**
 * Mode boundary for the visual Projection. Only DEMO may use local Simulation;
 * PAPER and LIVE require a backend-owned Snapshot.
 */
export function canUseSimulation(mode: ProjectionMode): mode is "DEMO" {
  return mode === "DEMO";
}

export function runtimeForSource(source: ProjectionSource): RuntimeOperations | null {
  if (source.kind === "simulation") return null;
  return source.snapshot.operations?.runtime ?? null;
}

export function assertProjectionSource(source: ProjectionSource): void {
  if (source.mode === "DEMO" && source.kind !== "simulation") {
    throw new Error("DEMO projection must use the simulation source");
  }
  if (source.mode !== "DEMO" && source.kind !== "backend") {
    throw new Error("PAPER/LIVE projection must use the backend source");
  }
}
