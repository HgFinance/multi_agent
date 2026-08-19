export function safeInternalNextPath(value: string | null | undefined, fallback = "/dashboard"): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) return fallback;
  if (/^\/(?:%2f|%5c)/i.test(value)) return fallback;
  try {
    const base = "https://ai-office.invalid";
    const parsed = new URL(value, base);
    if (parsed.origin !== base || parsed.pathname.startsWith("//") || parsed.pathname.includes("\\")) return fallback;
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return fallback;
  }
}
