/**
 * 로컬 TypeScript 검사에서 사용하는 Cloudflare Worker 런타임 계약이다.
 * 실제 바인딩 값은 배포 환경이 주입하며, 이 선언은 로컬 타입 검사만 보강한다.
 */

interface Fetcher {
  fetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response>;
}

type D1Value = string | number | boolean | null | ArrayBuffer | ArrayBufferView;

interface D1Result<T = Record<string, unknown>> {
  success: boolean;
  meta: Record<string, unknown>;
  results: T[];
}

interface D1ExecResult {
  count: number;
  duration: number;
}

interface D1PreparedStatement {
  bind(...values: D1Value[]): D1PreparedStatement;
  first<T = Record<string, unknown>>(columnName?: string): Promise<T | null>;
  run<T = Record<string, unknown>>(): Promise<D1Result<T>>;
  all<T = Record<string, unknown>>(): Promise<D1Result<T>>;
  raw<T = unknown[]>(): Promise<T[]>;
}

interface D1Database {
  prepare(query: string): D1PreparedStatement;
  dump(): Promise<ArrayBuffer>;
  batch<T = Record<string, unknown>>(statements: D1PreparedStatement[]): Promise<D1Result<T>[]>;
  exec(query: string): Promise<D1ExecResult>;
}

declare module "cloudflare:workers" {
  export const env: {
    DB?: D1Database;
  };
}
