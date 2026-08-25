"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { AUTH_MODE } from "../lib/authMode";
import { useAuth } from "../lib/AuthProvider";

export default function LoginPage() {
  const router = useRouter();
  const auth = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (AUTH_MODE === "fixture" || auth.status === "authenticated") router.replace("/dashboard");
  }, [auth.status, router]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await auth.signInWithPassword(email.trim(), password);
      router.replace("/dashboard");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "로그인에 실패했습니다.");
    } finally {
      setBusy(false);
    }
  }

  if (AUTH_MODE === "fixture") return null;

  return (
    <main className="min-h-screen grid place-items-center bg-surface px-6 text-on-surface">
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 rounded-xl border border-outline-variant bg-surface-container-lowest p-6 shadow-sm">
        <div>
          <h1 className="m-0 text-headline-sm font-bold">AI Office 로그인</h1>
          <p className="mt-2 text-body-sm text-on-surface-variant">Supabase 계정으로 계속합니다.</p>
        </div>
        <label className="block text-body-sm">이메일<input className="mt-1 w-full rounded border border-outline-variant bg-surface px-3 py-2" type="email" autoComplete="email" value={email} onChange={(event) => setEmail(event.target.value)} required /></label>
        <label className="block text-body-sm">비밀번호<input className="mt-1 w-full rounded border border-outline-variant bg-surface px-3 py-2" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
        {error ? <p role="alert" className="m-0 text-body-sm text-error">{error}</p> : null}
        <button className="w-full rounded bg-primary px-4 py-2 font-semibold text-on-primary disabled:opacity-50" type="submit" disabled={busy}>{busy ? "확인 중…" : "로그인"}</button>
      </form>
    </main>
  );
}
