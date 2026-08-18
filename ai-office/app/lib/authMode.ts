export type FrontendAuthMode = "supabase" | "fixture";

export function resolveAuthMode(requested: string | undefined, nodeEnv: string | undefined): FrontendAuthMode {
  const fixtureRequested = requested?.trim().toLowerCase() === "fixture";
  if (fixtureRequested && nodeEnv === "production") {
    throw new Error("fixture_auth_is_disabled_in_production");
  }
  return fixtureRequested ? "fixture" : "supabase";
}

/**
 * Fixture identity is deliberately impossible in a production bundle. Local
 * demos and deterministic tests must opt in with NEXT_PUBLIC_AUTH_MODE=fixture.
 */
export const AUTH_MODE = resolveAuthMode(process.env.NEXT_PUBLIC_AUTH_MODE, process.env.NODE_ENV);

export const fixtureAuthEnabled = AUTH_MODE === "fixture";

export function assertSafeAuthMode(): void {
  resolveAuthMode(process.env.NEXT_PUBLIC_AUTH_MODE, process.env.NODE_ENV);
}

assertSafeAuthMode();
