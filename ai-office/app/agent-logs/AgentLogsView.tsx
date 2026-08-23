"use client";

import { useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import {
  fetchOperations,
  subscribeOperationsStream,
  type LlmPerformanceMetric,
  type OperationsDepartment,
  type OperationsView,
} from "../lib/operationsClient";
import DepartmentInspector from "./DepartmentInspector";
import { KANBAN_BASE_URL, resolveKanbanUrl } from "../lib/kanbanUrl";
import { readDiscordMessages, readDiscordThread, type DiscordMessage } from "../lib/discordClient";
import HermesKanbanBoard from "./HermesKanbanBoard";
import { fetchHermesKanban, type HermesKanbanBoard as HermesKanbanBoardData } from "../lib/kanbanClient";

/**
 * Agent Logs — 페이지 상단(전체 부서 실행 현황).
 *
 * 데이터·판정은 main의 `ops/RiskQaPanel.tsx` 로직 그대로이고, 겉모습만
 * 우리 디자인 토큰으로 옮겼다. 부서 수·Worker 수의 출처는 각 Hermes Profile의
 * Worker Registry와 실행 상태는 인증된 BFF `/ui/snapshot` polling으로 받는다.
 */

const STATUS_VIEW: Record<string, { label: string; tone: string }> = {
  RUNNING: { label: "업무 중", tone: "border-primary/30 bg-secondary-container text-primary" },
  QUEUED: { label: "실행 대기", tone: "border-outline-variant bg-surface-container text-on-surface-variant" },
  IDLE: { label: "대기", tone: "border-outline-variant bg-surface-container text-on-surface-variant" },
  WAITING_APPROVAL: { label: "승인 대기", tone: "border-primary/30 bg-secondary-container text-primary" },
  OFFLINE: { label: "미연결", tone: "border-outline-variant bg-surface-container-high text-on-surface-variant" },
  DEGRADED: { label: "안전 보류", tone: "border-error/40 bg-error-container text-on-error-container" },
  BLOCKED: { label: "실행 차단", tone: "border-error/40 bg-error-container text-on-error-container" },
  ERROR: { label: "오류", tone: "border-error/40 bg-error-container text-on-error-container" },
};

const EMPTY_DEPARTMENTS: OperationsDepartment[] = [];
const NO_SUBSCRIBE = () => () => {};

function formatLatency(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "측정값 없음";
  return value >= 1_000 ? `${(value / 1_000).toFixed(value >= 10_000 ? 0 : 1)}초` : `${Math.round(value)}ms`;
}

function percentile(values: number[], percentileValue: number): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((left, right) => left - right);
  return sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * percentileValue) - 1)];
}

function PerformanceMetrics({ metrics }: { metrics: LlmPerformanceMetric[] }) {
  const measured = metrics.filter((item) => Number.isFinite(item.latency_ms) && item.latency_ms >= 0);
  if (measured.length === 0) return null;

  const latencies = measured.map((item) => item.latency_ms);
  const average = Math.round(latencies.reduce((total, value) => total + value, 0) / latencies.length);
  const p95 = percentile(latencies, 0.95);
  const tokenTotal = measured.reduce(
    (total, item) => total + (item.prompt_tokens ?? 0) + (item.completion_tokens ?? 0),
    0,
  );
  const hasTokenMeasurement = measured.some(
    (item) => item.prompt_tokens != null || item.completion_tokens != null,
  );
  const recent = [...measured].slice(-10).reverse();

  return (
    <section className="bg-surface-container-lowest border border-outline-variant rounded-lg p-4" aria-label="최근 Worker 성능">
      <div className="flex justify-between gap-3 flex-wrap items-baseline">
        <div>
          <h2 className="text-title-md font-title-md text-primary m-0">최근 Worker 성능</h2>
          <p className="text-xs text-on-surface-variant mt-1 mb-0">
            현재 요청에서 수집한 실행 지표입니다. 모델 입력·출력 원문은 표시하거나 전송하지 않습니다.
          </p>
        </div>
        <span className="text-xs text-on-surface-variant">표본 {measured.length}건</span>
      </div>
      <div className="mt-3 grid grid-cols-2 md:grid-cols-4 gap-2">
        {[
          ["평균 지연", formatLatency(average)],
          ["P95 지연", p95 === null ? "측정값 없음" : formatLatency(p95)],
          ["최대 지연", formatLatency(Math.max(...latencies))],
          ["입·출력 토큰", hasTokenMeasurement ? tokenTotal.toLocaleString("ko-KR") : "미측정"],
        ].map(([label, value]) => (
          <div key={label} className="rounded border border-outline-variant bg-surface-container-low px-3 py-2">
            <p className="m-0 text-xs text-on-surface-variant">{label}</p>
            <strong className="font-data-mono text-body-md text-on-surface">{value}</strong>
          </div>
        ))}
      </div>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full min-w-[680px] text-left text-xs">
          <thead className="text-on-surface-variant">
            <tr className="border-b border-outline-variant">
              <th className="pb-2 pr-3 font-medium">부서</th>
              <th className="pb-2 pr-3 font-medium">Worker</th>
              <th className="pb-2 pr-3 font-medium">모델</th>
              <th className="pb-2 pr-3 font-medium">지연</th>
              <th className="pb-2 pr-3 font-medium">입력/출력 토큰</th>
              <th className="pb-2 font-medium">상태</th>
            </tr>
          </thead>
          <tbody>
            {recent.map((metric, index) => (
              <tr key={`${metric.stage}-${metric.worker_id}-${index}`} className="border-b border-outline-variant/60 text-on-surface">
                <td className="py-2 pr-3">{metric.stage}</td>
                <td className="py-2 pr-3 font-data-mono">{metric.worker_id}</td>
                <td className="py-2 pr-3 font-data-mono">{metric.model_name || "미측정"}</td>
                <td className="py-2 pr-3 font-data-mono">{formatLatency(metric.latency_ms)}</td>
                <td className="py-2 pr-3 font-data-mono">
                  {metric.prompt_tokens == null && metric.completion_tokens == null
                    ? "미측정"
                    : `${metric.prompt_tokens ?? 0} / ${metric.completion_tokens ?? 0}`}
                </td>
                <td className="py-2">{metric.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function usePageHost(): string {
  return useSyncExternalStore(
    NO_SUBSCRIBE,
    () => window.location.hostname,
    () => "",
  );
}

function DepartmentCard({
  department,
  selected,
  onSelect,
}: {
  department: OperationsDepartment;
  selected: boolean;
  onSelect: () => void;
}) {
  const status = String(department.status).toUpperCase();
  const view = STATUS_VIEW[status] ?? { label: status, tone: "border-outline-variant bg-surface-container text-on-surface-variant" };

  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      aria-controls="department-inspector"
      className={`text-left bg-surface-container-lowest border rounded-lg p-4 flex flex-col gap-3 transition-colors hover:bg-surface-container ${
        selected ? "border-primary ring-1 ring-primary" : "border-outline-variant"
      }`}
    >
      <div className="flex justify-between items-start gap-2">
        <div className="min-w-0">
          <span className="block text-label-md font-label-md text-on-surface-variant uppercase">{department.domain}</span>
          <h3 className="text-body-lg font-body-lg font-bold text-primary mt-1">{department.name}</h3>
          <code className="text-xs text-outline">{department.department_code}</code>
        </div>
        {status !== "OFFLINE" ? (
          <span className={`shrink-0 px-2 py-0.5 rounded-full border text-xs font-medium ${view.tone}`}>{view.label}</span>
        ) : null}
      </div>

      <div className="flex flex-wrap gap-1.5 text-xs">
        <span className="px-2 py-1 rounded border border-outline-variant bg-surface text-on-surface-variant">
          <b className="font-data-mono text-on-surface">{department.active_worker_count}</b>/
          <span className="font-data-mono">{department.worker_count}</span> active
        </span>
        <span className="px-2 py-1 rounded border border-outline-variant bg-surface text-on-surface-variant">
          LLM <span className="font-data-mono">{department.llm_worker_count}</span> · Runner{" "}
          <span className="font-data-mono">{department.deterministic_worker_count}</span>
        </span>
        <span className="px-2 py-1 rounded border border-outline-variant bg-surface text-on-surface-variant">
          {department.current_stage ?? "대기"}
        </span>
      </div>

      {/* executor·model·contract 줄은 카드에서 빼고 선택 상세(DepartmentInspector)로 옮겼다. */}
    </button>
  );
}

/** Discord의 멘션·채널·역할·커스텀 이모지 토큰을 사람이 읽는 말로 바꾼다.
 *  줄바꿈과 마크다운 기호는 그대로 남겨 `renderDiscordMarkup`이 렌더링한다. */
function messageText(value: string): string {
  return value
    .replace(/<@&\d+>/g, "@역할")
    .replace(/<#\d+>/g, "#채널")
    .replace(/<a?:(\w+):\d+>/g, ":$1:")
    .replace(/<@!?\d+>/g, "@멘션")
    .trim();
}

/** `**bold**`/`*italic*`/`~~strike~~`/`` `code` ``/URL을 한 줄 안에서 React 노드로 바꾼다. */
function renderInlineMarkup(text: string, keyPrefix: string): ReactNode[] {
  const pattern = /\*\*(.+?)\*\*|__(.+?)__|~~(.+?)~~|`([^`]+?)`|\*(.+?)\*|_(.+?)_|(https?:\/\/[^\s<>]+)/g;
  const nodes: ReactNode[] = [];
  let lastIndex = 0;
  let key = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text))) {
    if (match.index > lastIndex) nodes.push(text.slice(lastIndex, match.index));
    const [, bold, boldAlt, strike, code, italic, italicAlt, url] = match;
    if (bold !== undefined || boldAlt !== undefined) {
      nodes.push(<strong key={`${keyPrefix}-${key++}`}>{bold ?? boldAlt}</strong>);
    } else if (strike !== undefined) {
      nodes.push(<del key={`${keyPrefix}-${key++}`}>{strike}</del>);
    } else if (code !== undefined) {
      nodes.push(
        <code key={`${keyPrefix}-${key++}`} className="rounded bg-surface-container-high px-1 py-0.5 font-data-mono text-[0.85em]">
          {code}
        </code>,
      );
    } else if (italic !== undefined || italicAlt !== undefined) {
      nodes.push(<em key={`${keyPrefix}-${key++}`}>{italic ?? italicAlt}</em>);
    } else if (url !== undefined) {
      nodes.push(
        <a key={`${keyPrefix}-${key++}`} href={url} target="_blank" rel="noreferrer" className="text-primary underline break-all">
          {url}
        </a>,
      );
    }
    lastIndex = pattern.lastIndex;
  }
  if (lastIndex < text.length) nodes.push(text.slice(lastIndex));
  return nodes;
}

/** 코드 블록(```)과 인용(`>`)을 블록 단위로 가르고, 그 안쪽은 인라인 마크업을 적용한다. */
function renderDiscordMarkup(text: string): ReactNode[] {
  const blocks: ReactNode[] = [];
  const fencePattern = /```(?:\w+\n)?([\s\S]*?)```/g;
  let lastIndex = 0;
  let blockIndex = 0;
  let match: RegExpExecArray | null;

  const renderPlainSegment = (segment: string) => {
    const lines = segment.split("\n");
    let quoteBuffer: string[] = [];
    const flushQuote = () => {
      if (!quoteBuffer.length) return;
      blocks.push(
        <blockquote key={`b-${blockIndex++}`} className="border-l-2 border-outline-variant pl-3 text-on-surface-variant">
          {quoteBuffer.map((line, lineIndex) => (
            <p key={lineIndex} className="m-0">
              {renderInlineMarkup(line, `bq-${blockIndex}-${lineIndex}`)}
            </p>
          ))}
        </blockquote>,
      );
      quoteBuffer = [];
    };
    let textBuffer: string[] = [];
    const flushText = () => {
      if (!textBuffer.length) return;
      const joined = textBuffer.join("\n");
      if (joined.trim()) {
        blocks.push(
          <p key={`b-${blockIndex++}`} className="m-0 whitespace-pre-wrap break-words">
            {renderInlineMarkup(joined, `t-${blockIndex}`)}
          </p>,
        );
      }
      textBuffer = [];
    };
    let bulletBuffer: string[] = [];
    const flushBullets = () => {
      if (!bulletBuffer.length) return;
      blocks.push(
        <ul key={`b-${blockIndex++}`} className="m-0 list-disc space-y-0.5 pl-5">
          {bulletBuffer.map((line, lineIndex) => (
            <li key={lineIndex}>{renderInlineMarkup(line, `li-${blockIndex}-${lineIndex}`)}</li>
          ))}
        </ul>,
      );
      bulletBuffer = [];
    };
    for (const line of lines) {
      const heading = /^(#{1,3})\s+(.+)$/.exec(line);
      const quoted = /^>\s?(.*)$/.exec(line);
      const bulleted = /^[-*]\s+(.+)$/.exec(line);
      if (heading) {
        flushText();
        flushQuote();
        flushBullets();
        blocks.push(
          <p
            key={`b-${blockIndex++}`}
            className={heading[1].length <= 2 ? "m-0 text-body-md font-body-md font-bold text-on-surface" : "m-0 font-bold text-on-surface"}
          >
            {renderInlineMarkup(heading[2], `h-${blockIndex}`)}
          </p>,
        );
      } else if (quoted) {
        flushText();
        flushBullets();
        quoteBuffer.push(quoted[1]);
      } else if (bulleted) {
        flushText();
        flushQuote();
        bulletBuffer.push(bulleted[1]);
      } else {
        flushQuote();
        flushBullets();
        textBuffer.push(line);
      }
    }
    flushText();
    flushQuote();
    flushBullets();
  };

  while ((match = fencePattern.exec(text))) {
    if (match.index > lastIndex) renderPlainSegment(text.slice(lastIndex, match.index));
    blocks.push(
      <pre
        key={`b-${blockIndex++}`}
        className="overflow-x-auto rounded-md bg-surface-container-high px-3 py-2 font-data-mono text-[0.85em] whitespace-pre-wrap break-words"
      >
        <code>{match[1].replace(/\n$/, "")}</code>
      </pre>,
    );
    lastIndex = fencePattern.lastIndex;
  }
  if (lastIndex < text.length) renderPlainSegment(text.slice(lastIndex));
  return blocks;
}

function formatClock(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" });
}

function formatDay(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString("ko-KR", { year: "numeric", month: "long", day: "numeric" });
}

function Avatar({ message }: { message: DiscordMessage }) {
  const className = "w-10 h-10 rounded-full shrink-0 object-cover bg-surface-container-high";
  if (message.avatar_url) {
    // next/image를 쓰지 않는다 - 외부 CDN 한 장에 remotePatterns 설정과 최적화
    // 서버 왕복이 붙는데, 40px 아바타에는 둘 다 필요 없다.
    // eslint-disable-next-line @next/next/no-img-element
    return <img src={message.avatar_url} alt="" width={40} height={40} className={className} />;
  }
  return (
    <span aria-hidden="true" className={`${className} grid place-items-center text-body-sm font-bold text-on-surface-variant`}>
      {message.author.slice(0, 1)}
    </span>
  );
}

/** Discord 한 줄. 채널 목록과 스레드 모달이 같은 것을 쓴다. */
function MessageRow({
  message,
  onOpenThread,
}: {
  message: DiscordMessage;
  onOpenThread?: (message: DiscordMessage) => void;
}) {
  const text = messageText(message.text);
  return (
    <article
      className={`flex gap-3 px-2 py-2 rounded-lg ${
        message.is_department_bot ? "bg-secondary-container/30" : "hover:bg-surface-container"
      }`}
    >
      <Avatar message={message} />
      <div className="min-w-0 flex-1">
        <p className="m-0 flex items-baseline gap-2 flex-wrap">
          <strong className="text-body-sm font-body-sm text-on-surface">{message.author}</strong>
          {message.is_bot ? (
            <span className="px-1.5 py-px rounded bg-primary text-on-primary text-[10px] font-bold leading-4">앱</span>
          ) : null}
          <time className="text-xs text-outline" dateTime={message.created_at}>
            {formatClock(message.created_at)}
          </time>
        </p>
        <div className="mt-1 flex flex-col gap-1.5 text-body-sm font-body-sm leading-6 text-on-surface">
          {text.trim() ? renderDiscordMarkup(text) : <span className="text-outline italic">첨부 또는 임베드만 있는 메시지</span>}
        </div>
        {message.thread_id && onOpenThread ? (
          // Discord의 스레드 미리보기 줄과 같은 자리. 클릭하면 모달이 열린다.
          <button
            type="button"
            onClick={() => onOpenThread(message)}
            className="mt-2 flex items-center gap-2 max-w-full rounded-lg border border-outline-variant bg-surface-container-low px-3 py-2 text-left hover:bg-surface-container transition-colors"
          >
            <span className="material-symbols-outlined text-[16px] text-on-surface-variant" aria-hidden="true">
              forum
            </span>
            <span className="truncate text-body-sm font-body-sm font-bold text-on-surface">
              {message.thread_name ?? "스레드"}
            </span>
            <span className="shrink-0 text-xs text-primary font-bold">
              메시지 {message.thread_message_count ?? 0}개 ›
            </span>
          </button>
        ) : null}
      </div>
    </article>
  );
}

/** 날짜가 바뀌는 자리에 구분선을 넣는다. Discord와 같은 규칙이다. */
function MessageList({
  messages,
  onOpenThread,
}: {
  messages: DiscordMessage[];
  onOpenThread?: (message: DiscordMessage) => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      {messages.map((message, index) => {
        const day = formatDay(message.created_at);
        const previousDay = index > 0 ? formatDay(messages[index - 1].created_at) : "";
        const divider = day && day !== previousDay ? day : "";
        return (
          <div key={message.id}>
            {divider ? (
              <div className="flex items-center gap-3 my-3">
                <hr className="flex-1 border-0 border-t border-outline-variant" />
                <span className="text-xs text-on-surface-variant">{divider}</span>
                <hr className="flex-1 border-0 border-t border-outline-variant" />
              </div>
            ) : null}
            <MessageRow message={message} onOpenThread={onOpenThread} />
          </div>
        );
      })}
    </div>
  );
}

/**
 * 스레드 모달. `<dialog>`의 `showModal()`이 backdrop·Escape 닫기·포커스 트랩을
 * 브라우저에서 주므로 오버레이나 키 핸들러를 직접 만들지 않는다.
 * `m-auto`는 필수 - Tailwind preflight가 `<dialog>`의 기본 중앙 정렬을 지운다.
 */
function ThreadDialog({
  department,
  parent,
  onClose,
}: {
  department: string;
  parent: DiscordMessage;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [messages, setMessages] = useState<DiscordMessage[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    ref.current?.showModal();
  }, []);

  useEffect(() => {
    if (!parent.thread_id) return;
    const controller = new AbortController();
    readDiscordThread(department, parent.thread_id, controller.signal)
      .then((body) => setMessages(body.messages))
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setError(cause instanceof Error ? cause.message : "스레드를 불러오지 못했습니다.");
      });
    return () => controller.abort();
  }, [department, parent.thread_id]);

  return (
    <dialog
      ref={ref}
      onClose={onClose}
      aria-labelledby="discord-thread-title"
      className="m-auto w-[min(46rem,92vw)] max-h-[85vh] p-0 rounded-xl bg-surface-container-lowest text-on-surface border border-outline-variant shadow-sm backdrop:bg-black/60"
    >
      <header className="flex items-start justify-between gap-3 bg-surface-container-lowest border-b border-outline-variant px-5 py-4">
        <div className="min-w-0">
          <h2
            id="discord-thread-title"
            title={parent.thread_name ?? undefined}
            className="m-0 text-title-md font-title-md font-bold text-on-surface break-words line-clamp-2"
          >
            {parent.thread_name ?? "스레드"}
          </h2>
          <p className="m-0 mt-1 text-body-sm font-body-sm text-on-surface-variant">
            시작한 사람: <b className="text-on-surface">{parent.author}</b>
          </p>
        </div>
        <form method="dialog" className="shrink-0">
          <button
            aria-label="스레드 닫기"
            className="grid place-items-center w-8 h-8 rounded-full text-on-surface-variant hover:bg-surface-container-high transition-colors"
          >
            <span className="material-symbols-outlined text-[20px]" aria-hidden="true">
              close
            </span>
          </button>
        </form>
      </header>
      <div className="px-3 py-3 overflow-y-auto max-h-[calc(85vh-6rem)]">
        {error ? (
          <p
            role="alert"
            className="text-body-sm font-body-sm text-on-error-container bg-error-container border border-error/40 rounded px-3 py-2 m-0"
          >
            {error}
          </p>
        ) : messages === null ? (
          <p className="text-body-sm font-body-sm text-on-surface-variant m-0 px-2">스레드를 불러오는 중입니다…</p>
        ) : (
          // 스레드 첫 줄은 스레드를 연 메시지다 - Discord도 그렇게 보여준다.
          // 스레드 종류에 따라 Discord가 시작 메시지를 스레드 안에 같이 주기도
          // 해서, 무조건 붙이지 않고 없을 때만 붙인다(두 번 보이면 안 된다).
          <MessageList
            messages={messages.some((item) => item.id === parent.id) ? messages : [parent, ...messages]}
          />
        )}
      </div>
    </dialog>
  );
}

function DiscordChat({ department }: { department: string }) {
  const [messages, setMessages] = useState<DiscordMessage[] | null>(null);
  const [error, setError] = useState("");
  const [openThread, setOpenThread] = useState<DiscordMessage | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    readDiscordMessages(department, 100, controller.signal)
      .then((body) => setMessages(body.messages))
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setError(cause instanceof Error ? cause.message : "Discord 대화를 불러오지 못했습니다.");
      });
    return () => controller.abort();
  }, [department]);

  // 최신 글이 아래에 있다. 스크롤을 안 내리면 제일 오래된 대화만 보여서
  // "새 메시지가 안 들어온다"로 읽힌다(카카오톡처럼 최신이 먼저 보이고 위로
  // 스크롤해 지난 대화를 본다).
  //
  // 이 패널은 접힌 `<details>` 안에 있다 - 접힌 동안은 숨은 상태라 scrollIntoView가
  // 먹지 않는다. `scrollRef`는 messages가 채워진 뒤에야 DOM에 붙으므로 이 effect를
  // `[messages]`에 걸어야 리스너가 실제로 붙는다(빈 배열이면 아직 없는 ref를 본다).
  useEffect(() => {
    if (!messages?.length) return;
    bottomRef.current?.scrollIntoView({ block: "end" });
    const details = scrollRef.current?.closest("details");
    if (!details) return;
    const handleToggle = () => {
      if (details.open) bottomRef.current?.scrollIntoView({ block: "end" });
    };
    details.addEventListener("toggle", handleToggle);
    return () => details.removeEventListener("toggle", handleToggle);
  }, [messages]);

  if (error) {
    return (
      <p
        role="alert"
        className="text-body-sm font-body-sm text-on-error-container bg-error-container border border-error/40 rounded px-3 py-2 m-0"
      >
        {error}
      </p>
    );
  }
  if (messages === null)
    return <p className="text-body-sm font-body-sm text-on-surface-variant m-0">Discord 대화를 불러오는 중입니다…</p>;
  if (messages.length === 0)
    return <p className="text-body-sm font-body-sm text-on-surface-variant m-0">아직 표시할 Discord 대화가 없습니다.</p>;

  return (
    // 위에서 아래로 흐르는 한 줄짜리 목록. 2열로 쪼개면 좌우 두 카드의 시각이
    // 뒤섞여 읽는 순서가 사라진다.
    <div ref={scrollRef} className="max-h-96 overflow-y-auto pr-1">
      <MessageList messages={messages} onOpenThread={setOpenThread} />
      <div ref={bottomRef} />
      {openThread ? (
        <ThreadDialog
          key={openThread.id}
          department={department}
          parent={openThread}
          onClose={() => setOpenThread(null)}
        />
      ) : null}
    </div>
  );
}

export default function AgentLogsView() {
  const [data, setData] = useState<OperationsView | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [selectedCode, setSelectedCode] = useState<string | null>(null);
  const [streamState, setStreamState] = useState<"connecting" | "connected" | "degraded">("connecting");
  const [lastKeepalive, setLastKeepalive] = useState<string | null>(null);
  const [kanban, setKanban] = useState<HermesKanbanBoardData | null>(null);
  const [kanbanError, setKanbanError] = useState("");
  const [kanbanLoading, setKanbanLoading] = useState(true);
  const pageHost = usePageHost();
  const kanbanUrl = useMemo(
    () => resolveKanbanUrl(KANBAN_BASE_URL, pageHost || undefined),
    [pageHost],
  );

  useEffect(() => {
    let alive = true;
    const refresh = () => {
      fetchOperations()
        .then((next) => alive && setData(next))
        .catch((cause) => alive && setError(cause instanceof Error ? cause.message : String(cause)))
        .finally(() => alive && setLoading(false));
      fetchHermesKanban()
        .then((next) => {
          if (!alive) return;
          setKanban(next);
          setKanbanError("");
        })
        .catch((cause: unknown) => {
          if (!alive) return;
          setKanbanError(cause instanceof Error ? cause.message : String(cause));
        })
        .finally(() => alive && setKanbanLoading(false));
    };

    refresh();
    const unsubscribe = subscribeOperationsStream({
      onOpen: () => alive && setStreamState("connected"),
      onSnapshotRequired: refresh,
      onStatus: refresh,
      onKeepalive: (observedAt) => {
        if (!alive) return;
        setLastKeepalive(observedAt || new Date().toISOString());
        setStreamState("connected");
      },
      onError: () => alive && setStreamState("degraded"),
    });
    return () => {
      alive = false;
      unsubscribe();
    };
  }, []);

  const departments = data?.departments ?? EMPTY_DEPARTMENTS;
  const registeredWorkers = departments.reduce((total, item) => total + item.worker_count, 0);
  const activeWorkers = departments.reduce((total, item) => total + item.active_worker_count, 0);
  const degraded = departments.filter((item) => ["DEGRADED", "BLOCKED", "ERROR"].includes(item.status)).length;

  // 선택한 부서. 아직 안 눌렀으면 아무것도 펼치지 않는다.
  const selected = useMemo(
    () => departments.find((item) => item.department_code === selectedCode) ?? null,
    [departments, selectedCode],
  );
  // 8개 부서 봇이 전부 같은 Discord 채널 하나를 쓴다(discordClient.ts) - 부서
  // 카드를 눌러도 대화내용은 CEO Office 채널로 고정한다. 그래야 다른 부서를
  // 고를 때마다 화면이 깜빡이며 다시 불러오지 않는다.
  const discordDepartment =
    departments.find((item) => item.department_code === "ceo-agent") ?? departments[0] ?? null;

  const summaryMetrics = [
    { label: "부서", value: departments.length || 8 },
    { label: "등록 직원", value: registeredWorkers },
    { label: "실행 중", value: activeWorkers },
    { label: "보류·오류", value: degraded },
  ];

  return (
    <main className="flex-1 w-full max-w-app mx-auto p-margin-mobile md:p-margin-desktop flex flex-col gap-gutter">
      <section className="flex justify-between items-start gap-gutter flex-wrap">
        <div className="min-w-0">
          <p className="text-label-md font-label-md text-on-surface-variant uppercase">
            Backend Read Model · 8 Departments
          </p>
          <h1 className="text-headline-lg font-headline-lg text-primary font-bold tracking-tight mt-2">
            Agent Logs
          </h1>
          <p className="text-body-sm font-body-sm text-on-surface-variant mt-2 max-w-3xl">
            BFF snapshot과 수신된 runtime event를 기준으로 부서 Registry와 최근 로그를 표시합니다.
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <span
            className={`shrink-0 px-3 py-1 rounded-full border text-label-md font-label-md ${
              data?.runtime_connected
                ? "border-tertiary-fixed-dim bg-tertiary-fixed/30 text-on-tertiary-fixed-variant"
                : "border-outline text-on-surface-variant"
            }`}
          >
            {data ? (data.runtime_connected ? "RUNTIME CONNECTED" : "RUNTIME NOT OBSERVED") : "RUNTIME CHECKING"}
          </span>
          <span
            className={`shrink-0 px-3 py-1 rounded-full border text-label-md font-label-md ${
              streamState === "connected"
                ? "border-tertiary-fixed-dim bg-tertiary-fixed/30 text-on-tertiary-fixed-variant"
                : "border-outline text-on-surface-variant"
            }`}
          >
            {streamState === "connected" ? "BFF AUTH POLLING CONNECTED" : streamState === "connecting" ? "BFF POLLING STARTING" : "BFF POLLING DEGRADED"}
          </span>
        </div>
      </section>

      <HermesKanbanBoard board={kanban} error={kanbanError} loading={kanbanLoading} kanbanUrl={kanbanUrl} />

      <section className="flex flex-wrap gap-2" aria-label="전체 부서 요약">
        {summaryMetrics.map((metric) => (
          <span
            key={metric.label}
            className="px-4 py-2 rounded border border-outline-variant bg-surface-container-lowest text-body-sm font-body-sm text-on-surface-variant"
          >
            {metric.label} <b className="font-data-mono text-on-surface">{metric.value}</b>
          </span>
        ))}
      </section>
      <section className="flex flex-wrap gap-2" aria-label="Runtime event status">
        <span className="px-4 py-2 rounded border border-outline-variant bg-surface-container-lowest text-body-sm font-body-sm text-on-surface-variant">
          BFF auth polling <b className="font-data-mono text-on-surface">{streamState === "connected" ? "connected" : "waiting"}</b>
        </span>
        <span className="px-4 py-2 rounded border border-outline-variant bg-surface-container-lowest text-body-sm font-body-sm text-on-surface-variant">
          BFF keepalive <b className="font-data-mono text-on-surface">{lastKeepalive ? new Date(lastKeepalive).toLocaleTimeString("ko-KR") : "waiting"}</b>
        </span>
      </section>

      <PerformanceMetrics metrics={data?.metrics ?? []} />

      {/* 접기는 native `<details>`가 한다 - 열림 상태·키보드·스크린리더가 전부 딸려
          온다. `<details>`에 flex를 주면 닫혀도 내용이 보이므로 레이아웃은 안쪽
          div가 맡는다. `<section aria-label>`은 landmark라 남겨 둔다. */}
      <section className="bg-surface-container-lowest border border-outline-variant rounded-lg p-4" aria-label="부서 내부 메시지">
       <details className="group">
        <summary className="flex justify-between items-center gap-3 flex-wrap cursor-pointer list-none [&::-webkit-details-marker]:hidden">
          <h2 className="text-title-md font-title-md text-primary m-0 flex items-center gap-1.5">
            <span
              className="material-symbols-outlined text-[18px] text-on-surface-variant transition-transform group-open:rotate-180"
              aria-hidden="true"
            >
              expand_more
            </span>
            부서 내부 대화내용
          </h2>
          {discordDepartment ? (
            <span className="text-xs text-on-surface-variant">{discordDepartment.name}</span>
          ) : null}
        </summary>
        <div className="mt-3">
        {discordDepartment ? (
          <DiscordChat key={discordDepartment.department_code} department={discordDepartment.department_code} />
        ) : null}
        </div>
       </details>
      </section>

      {departments.length > 0 ? (
        <section className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4" aria-label="8개 부서 목록">
          {departments.map((department) => (
            <DepartmentCard
              key={department.department_code}
              department={department}
              selected={department.department_code === selectedCode}
              onSelect={() =>
                setSelectedCode((current) =>
                  current === department.department_code ? null : department.department_code,
                )
              }
            />
          ))}
        </section>
      ) : (
        <section
          role="status"
          className="bg-surface-container-lowest border border-outline-variant rounded-lg p-8 text-center flex flex-col gap-2"
        >
          <strong className="text-body-md font-body-md text-on-surface">
            {loading ? "BFF 부서 Registry를 불러오는 중입니다." : "BFF 부서 Registry를 기다리는 중입니다."}
          </strong>
          <p className="text-body-sm font-body-sm text-on-surface-variant m-0">
            연결되면 8개 부서의 Worker 수와 LangGraph 상태가 표시됩니다.
          </p>
          {error ? <p className="text-xs text-error m-0 mt-2">⚠️ {error}</p> : null}
        </section>
      )}

      {selected && data ? (
        <DepartmentInspector department={selected} data={data} />
      ) : departments.length > 0 ? (
        <p className="text-body-sm font-body-sm text-on-surface-variant">
          부서 카드를 누르면 부서 상태와 연결된 결과물이 아래에 펼쳐집니다.
        </p>
      ) : null}

      <p className="text-xs text-outline">
        BFF authenticated polling + keepalive Source: <code>/ui/snapshot</code> · WebSocket은 one-use ticket 도입 전 비활성 · 부서 수와 Worker 수 Source: 각 Hermes Profile의 Worker Registry
      </p>
    </main>
  );
}
