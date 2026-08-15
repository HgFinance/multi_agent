"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

/**
 * 앱 전역 TanStack Query Provider.
 *
 * 도입 이유(2026-08-14, Dashboard 채팅 이력 버그 2건):
 * 1. 이력 조회가 완료된 Task마다 `await`를 순차로 거는 `for` 루프라 N개면
 *    N번 왕복을 직렬로 기다렸다 - `useQueries`로 병렬화한다.
 * 2. 계정 전환·재조회가 실패하면 `setMessages([INITIAL_AI_MESSAGE, ...])`가
 *    `try` 밖에서 무조건 실행되어 이미 떠 있던 이전 이력까지 지워졌다 -
 *    TanStack Query는 refetch 실패 시 `data`를 마지막 성공값 그대로 두고
 *    `isError`만 별도로 알려주므로 이 문제가 구조적으로 재발하지 않는다.
 *
 * `useState(() => new QueryClient())`인 이유: 렌더마다 새 인스턴스를 만들면
 * 캐시가 매번 초기화된다 - Provider 생명주기 동안 하나만 유지해야 한다.
 */
function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // BFF는 Kanban CLI 서브프로세스를 감싼 읽기 전용 API라 값이 자주
        // 안 바뀐다. staleTime을 두지 않으면 매 마운트마다 재조회해 캐싱
        // 이득이 사라진다.
        staleTime: 5_000,
        retry: 1,
      },
    },
  });
}

export function QueryProvider({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(makeQueryClient);
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
