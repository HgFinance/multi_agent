import type { ReactNode } from "react";
import type { DiscordMessage } from "./discordClient";

/**
 * Discord 메시지 렌더링 공용 유틸.
 *
 * `agent-logs/AgentLogsView.tsx`의 부서 내부 대화(전체 채널 로그)와
 * `components/ResearchPanel.tsx`의 "최근 답변 한 줄" 발췌가 같은 마크업·시간
 * 표기 규칙을 쓴다 - 여기서 한 번만 구현한다.
 */

/** Discord의 멘션·채널·역할·커스텀 이모지 토큰을 사람이 읽는 말로 바꾼다.
 *  줄바꿈과 마크다운 기호는 그대로 남겨 `renderDiscordMarkup`이 렌더링한다. */
export function messageText(value: string): string {
  return value
    .replace(/<@&\d+>/g, "@역할")
    .replace(/<#\d+>/g, "#채널")
    .replace(/<a?:(\w+):\d+>/g, ":$1:")
    .replace(/<@!?\d+>/g, "@멘션")
    .trim();
}

/** `**bold**`/`*italic*`/`~~strike~~`/`` `code` ``/URL을 한 줄 안에서 React 노드로 바꾼다. */
export function renderInlineMarkup(text: string, keyPrefix: string): ReactNode[] {
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
export function renderDiscordMarkup(text: string): ReactNode[] {
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

export function formatClock(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" });
}

export function formatDay(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString("ko-KR", { year: "numeric", month: "long", day: "numeric" });
}

export function DiscordAvatar({ message }: { message: DiscordMessage }) {
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
