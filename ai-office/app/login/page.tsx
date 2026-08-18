"use client";

import { type FormEvent, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useAuth } from "../lib/AuthProvider";
import { safeInternalNextPath } from "../lib/authNavigation";

export default function LoginPage() {
  const auth = useAuth();
  const router = useRouter();
  const search = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const next = safeInternalNextPath(search.get("next"));

  useEffect(() => {
    if (auth.status === "authenticated") router.replace(next);
  }, [auth.status, next, router]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!email.trim() || !password) return;
    setSubmitting(true);
    setError("");
    try {
      await auth.signInWithPassword(email, password);
      router.replace(next);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "로그인에 실패했습니다.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="min-h-screen grid place-items-center bg-surface px-5 py-10 text-on-surface">
      <section className="w-full max-w-md rounded-xl border border-outline-variant bg-surface-container-lowest p-7 shadow-sm">
        <p className="text-label-md font-bold uppercase tracking-widest text-secondary">HgFinance AI Office</p>
        <h1 className="mt-2 text-headline-lg font-bold text-primary">운영자 로그인</h1>
        <p className="mt-3 text-body-md text-on-surface-variant">
          등록된 Supabase 운영 계정으로 로그인하세요. 펀드 접근 권한은 서버가 확인합니다.
        </p>
        <form className="mt-6 grid gap-4" onSubmit={submit}>
          <label className="grid gap-1.5 text-label-md font-semibold">
            이메일
            <input
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              className="rounded-md border border-outline-variant bg-surface px-3 py-2.5 text-body-md outline-none focus:border-primary"
            />
          </label>
          <label className="grid gap-1.5 text-label-md font-semibold">
            비밀번호
            <input
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              className="rounded-md border border-outline-variant bg-surface px-3 py-2.5 text-body-md outline-none focus:border-primary"
            />
          </label>
          {(error || auth.error) && (
            <p role="alert" className="rounded-md bg-error-container px-3 py-2 text-body-sm text-on-error-container">
              {error || auth.error}
            </p>
          )}
          <button
            type="submit"
            disabled={submitting || auth.status === "loading"}
            className="rounded-md bg-primary px-4 py-2.5 font-bold text-on-primary disabled:cursor-wait disabled:opacity-60"
          >
            {submitting ? "로그인 중…" : "로그인"}
          </button>
        </form>
      </section>
    </main>
  );
}
