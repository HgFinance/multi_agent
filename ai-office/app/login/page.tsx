"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

/**
 * 로그인 화면은 더 이상 없다(2026-08-19). 이 경로는 대시보드로 넘긴다.
 *
 * ## 왜 폼을 없앴나
 *
 * 이 앱은 실제 Supabase 인증을 붙이지 않기로 했고, 계정은 Fund Owner 하나로
 * 고정돼 있다(`app/lib/currentAccount.ts`). 그래서 `AuthProvider`의
 * `signInWithPassword()`는 이제 항상 throw한다 - 폼을 남겨두면 이메일·비밀번호를
 * 받아놓고 반드시 실패하는 화면이 된다. 연결 안 된 걸 연결된 것처럼 보이지
 * 않게 하는 게 이 앱의 원칙이다(`TopNav.tsx`: 준비 안 된 항목은 링크가 아니라
 * disabled 버튼).
 *
 * 파일을 지우지 않고 리다이렉트로 남기는 이유: 예전 링크나 북마크로 `/login`에
 * 들어오는 경우 404 대신 정상 화면으로 보내기 위해서다.
 *
 * 실제 로그인을 다시 붙이면 이 파일을 되살리되, 그때는 `AuthProvider`의 세션
 * 배선부터 복구해야 한다.
 */
export default function LoginPage() {
  const router = useRouter();

  useEffect(() => {
    router.replace("/dashboard");
  }, [router]);

  return (
    <main className="min-h-screen grid place-items-center bg-surface px-6 text-on-surface">
      <p className="text-body-md text-on-surface-variant">대시보드로 이동합니다.</p>
    </main>
  );
}
