import { createClient, type Session, type SupabaseClient } from "@supabase/supabase-js";

let browserClient: SupabaseClient | null = null;

export class AuthConfigurationError extends Error {}
export class AuthenticationRequiredError extends Error {}

function decodeJwtPayload(key: string): Record<string, unknown> | null {
  const parts = key.split(".");
  if (parts.length !== 3) return null;
  try {
    const base64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64.padEnd(Math.ceil(base64.length / 4) * 4, "=");
    const payload = JSON.parse(atob(padded)) as unknown;
    return payload && typeof payload === "object" ? payload as Record<string, unknown> : null;
  } catch {
    return null;
  }
}

export function isBrowserSafeSupabaseKey(value: string): boolean {
  const key = value.trim();
  if (/^sb_publishable_[A-Za-z0-9_-]+$/.test(key)) return true;
  if (key.startsWith("sb_secret_")) return false;
  return decodeJwtPayload(key)?.role === "anon";
}

export function validateSupabasePublishableKey(value: string): void {
  if (!isBrowserSafeSupabaseKey(value)) {
    throw new AuthConfigurationError("supabase_browser_key_must_be_publishable_or_anon");
  }
}

function publicSupabaseConfig(): { url: string; publishableKey: string } {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL?.trim() ?? "";
  const publishableKey = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY?.trim() ?? "";
  if (!url || !publishableKey) {
    throw new AuthConfigurationError("supabase_public_auth_configuration_missing");
  }
  validateSupabasePublishableKey(publishableKey);
  return { url, publishableKey };
}

/** Browser-only singleton. A service-role credential is never accepted here. */
export function getSupabaseBrowserClient(): SupabaseClient {
  if (typeof window === "undefined") {
    throw new AuthConfigurationError("supabase_browser_client_requested_on_server");
  }
  if (!browserClient) {
    const { url, publishableKey } = publicSupabaseConfig();
    browserClient = createClient(url, publishableKey, {
      auth: {
        autoRefreshToken: true,
        detectSessionInUrl: true,
        flowType: "pkce",
        persistSession: true,
      },
    });
  }
  return browserClient;
}

async function sessionOrThrow(forceRefresh = false): Promise<Session> {
  const client = getSupabaseBrowserClient();
  if (forceRefresh) {
    const refreshed = await client.auth.refreshSession();
    if (refreshed.error || !refreshed.data.session) {
      throw new AuthenticationRequiredError("supabase_session_refresh_failed");
    }
    return refreshed.data.session;
  }

  const current = await client.auth.getSession();
  if (current.error || !current.data.session) {
    throw new AuthenticationRequiredError("supabase_session_required");
  }
  const session = current.data.session;
  const expiresSoon = (session.expires_at ?? 0) * 1000 <= Date.now() + 60_000;
  return expiresSoon ? sessionOrThrow(true) : session;
}

export async function getSupabaseAccessToken(forceRefresh = false): Promise<string> {
  return (await sessionOrThrow(forceRefresh)).access_token;
}

export async function getSupabaseUserId(): Promise<string> {
  return (await sessionOrThrow()).user.id;
}

export async function clearLocalSupabaseSession(
  client: Pick<SupabaseClient, "auth"> = getSupabaseBrowserClient(),
): Promise<void> {
  const result = await client.auth.signOut({ scope: "local" });
  if (result.error) throw result.error;
}

/** Test-only reset; it does not sign out or mutate a real remote session. */
export function resetSupabaseBrowserClientForTests(): void {
  browserClient = null;
}
