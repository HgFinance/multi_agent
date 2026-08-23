"use client";

import { useEffect, useState } from "react";
import { readDiscordMessages, readDiscordThread, type DiscordMessage } from "../lib/discordClient";
import { DiscordAvatar, formatClock, formatDay, messageText, renderDiscordMarkup } from "../lib/discordRender";

/**
 * 리서치본부 패널.
 *
 * 원래는 이 부서 전용 토큰(`department=research-department`)으로 `is_department_bot`
 * 판정을 받으려 했으나, 이 환경엔 `DISCORD_BOT_TOKEN_RESEARCH`가 아직 없어 503이
 * 난다(2026-08-23 확인, `apps/api/discord_read.py`의 `credentials()`). 그래서
 * 이미 동작하는 `ceo-agent` 채널·토큰으로 같은 채널을 읽고, **표시 이름**
 * (`HERMES-RESEARCH`)으로 리서치 봇 글을 찾는다 - `discord_read.py`가 경고하듯
 * 표시 이름은 서버에서 바뀔 수 있는 임시 방편이라, RESEARCH 토큰이 생기면
 * `is_department_bot` 판정으로 되돌려야 한다.
 *
 * 실제 분석 답변은 채널에 바로 오지 않고 질문이 연 스레드 안에 온다(예:
 * "삼성전자 악재 분석해줘"). 그래서 채널을 최근순으로 훑다가 스레드가 달린
 * 메시지를 만나면 그 스레드를 열어 안에서 리서치 봇 답변을 찾는다.
 *
 * "가장 최근"은 **질문/스레드가 새로 시작된 시각** 기준이다(2026-08-23 확정).
 * 김동규님처럼 며칠 전에 연 스레드를 계속 이어서 새 질문을 물어보는 경우가
 * 있는데, 그 안의 답이 시각상 더 늦더라도 채널에서는 맨 아래로 안 올라와
 * Discord 화면과 어긋난다 - 그래서 "가장 늦게 답한 메시지"가 아니라 "채널에서
 * 가장 최근에 새로 열린 스레드부터" 훑고, 그중 **제대로 완료된 답이 있는
 * 첫 번째**를 쓴다. 도중에 만난 스레드의 답이 아직 실패/미완료
 * (provider 쿼터·인증 오류 등으로 "⚠️ 분석을 완료하지 못했습니다"만 온 경우)면
 * 없는 셈 치고 그다음으로 최근에 열린 스레드로 넘어간다.
 *
 * **답변만 보여주면 무슨 질문에 대한 답인지 알 수 없다.** 스레드 이름은
 * Discord가 그 스레드의 첫 메시지로 자동으로 지어서, 같은 스레드를 며칠에
 * 걸쳐 여러 질문에 재사용하면(2026-08-23 실측 사례) 스레드 이름과 실제
 * 질문이 완전히 달라진다 - 그래서 스레드 이름 대신, 답변 바로 앞에 있는
 * **사람이 쓴 메시지**(`is_bot: false`)를 그 답의 질문으로 찾아 함께 보여준다.
 */

const CHANNEL_DEPARTMENT = "ceo-agent";
const RESEARCH_BOT_NAME = "HERMES-RESEARCH";
const MAX_THREADS_TO_OPEN = 12;

function Badge({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center whitespace-nowrap rounded-full border border-outline-variant bg-surface-container-lowest px-2.5 py-0.5 text-[10px] font-semibold text-on-surface-variant">
      {children}
    </span>
  );
}

function isResearchMessage(message: DiscordMessage): boolean {
  return message.author.trim().toUpperCase() === RESEARCH_BOT_NAME && messageText(message.text).length > 0;
}

/** "✅ 분석을 완료했습니다" 같은 완료 표시가 있어야 "제대로 나온 답"으로 본다.
 *  provider 쿼터·인증 실패로 본문 없이 끝난 "⚠️ 분석을 완료하지 못했습니다" 류는
 *  답이 아직 안 나온 것과 같게 취급해 건너뛴다. */
function isCompletedResearchAnswer(message: DiscordMessage): boolean {
  return isResearchMessage(message) && messageText(message.text).includes("✅");
}

type ResearchResult = {
  message: DiscordMessage;
  /** 답변 바로 앞에 있는, 사람이 쓴 메시지. 못 찾으면 null(질문 없이 답만 온 경우). */
  question: DiscordMessage | null;
};

/** `answerIndex` 앞쪽에서 가장 가까운 사람 메시지(`is_bot: false`)를 찾는다 - 그게 이 답의 질문이다. */
function findPrecedingQuestion(messages: DiscordMessage[], answerIndex: number): DiscordMessage | null {
  for (let index = answerIndex - 1; index >= 0; index -= 1) {
    if (!messages[index].is_bot) return messages[index];
  }
  return null;
}

/**
 * 스레드를 처음 연 메시지는 `thread.messages`에 없다(discord_read.py가 자리표시자를
 * 뺀다 - 진짜 내용은 채널 쪽 메시지에 있다). 그래서 단발성 질문 스레드(예:
 * "삼성전자 악재 분석해줘")는 스레드 안을 아무리 찾아도 사람 메시지가 안 나온다 -
 * 이땐 스레드를 연 채널 메시지 자체가 질문이다.
 */
function resolveQuestion(threadMessages: DiscordMessage[], answerIndex: number, threadStarter: DiscordMessage): DiscordMessage {
  return findPrecedingQuestion(threadMessages, answerIndex) ?? threadStarter;
}

/**
 * 채널을 "질문/스레드가 열린 시각" 최근순으로 훑어, 리서치 봇이 제대로 완료된
 * 답을 남긴 **첫 번째**(=가장 최근에 새로 시작된) 것을 쓴다. 스레드 안에는
 * 여러 부서 봇이 섞여 답하므로(HERMES-CEO → HERMES-RESEARCH → HERMES-RISK →
 * HERMES-CEO 순서 등) 그 안에서도 "마지막 메시지"가 아니라 HERMES-RESEARCH가
 * 남긴, 완료 표시가 있는 메시지만 골라낸다.
 */
async function findLatestResearchAnswer(signal: AbortSignal): Promise<ResearchResult | null> {
  const channel = await readDiscordMessages(CHANNEL_DEPARTMENT, 100, signal);
  const messages = channel.messages;
  let openedThreads = 0;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const candidate = messages[index];
    if (isCompletedResearchAnswer(candidate)) {
      return { message: candidate, question: findPrecedingQuestion(messages, index) };
    }
    if (candidate.thread_id && openedThreads < MAX_THREADS_TO_OPEN) {
      openedThreads += 1;
      const thread = await readDiscordThread(CHANNEL_DEPARTMENT, candidate.thread_id, signal);
      let answerIndex = -1;
      thread.messages.forEach((message, threadIndex) => {
        if (isCompletedResearchAnswer(message)) answerIndex = threadIndex;
      });
      if (answerIndex >= 0) {
        return {
          message: thread.messages[answerIndex],
          question: resolveQuestion(thread.messages, answerIndex, candidate),
        };
      }
    }
  }
  return null;
}

export default function ResearchPanel() {
  const [result, setResult] = useState<ResearchResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    findLatestResearchAnswer(controller.signal)
      .then((found) => setResult(found))
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setError(cause instanceof Error ? cause.message : "Discord 대화를 불러오지 못했습니다.");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  return (
    <section
      className="min-w-0 overflow-hidden rounded-lg border border-outline-variant bg-surface-container-lowest shadow-sm"
      aria-label="리서치본부"
    >
      <div className="flex items-center justify-between gap-3 border-b border-outline-variant bg-surface-container-low px-4 py-2.5">
        <span className="flex min-w-0 items-center gap-2 text-label-md font-label-md text-on-surface-variant">
          <span className="material-symbols-outlined text-[16px]" aria-hidden="true">
            science
          </span>
          <span className="truncate">research.hypothesis_pipeline</span>
        </span>
      </div>

      <div className="space-y-3 p-4 md:p-6">
        <h3 className="m-0 text-title-md font-title-md font-semibold text-primary">최근 분석 결과</h3>

        {error ? (
          <p role="alert" className="m-0 rounded border border-error/40 bg-error-container px-3 py-2 text-xs text-on-error-container">
            {error}
          </p>
        ) : null}

        {!error && loading ? (
          <p className="m-0 text-body-sm font-body-sm text-on-surface-variant">Discord 대화를 불러오는 중입니다…</p>
        ) : null}

        {!error && !loading && !result ? (
          <p className="m-0 text-body-sm font-body-sm text-on-surface-variant">
            {RESEARCH_BOT_NAME}가 최근 대화에 남긴 답변을 찾지 못했습니다.
          </p>
        ) : null}

        {result ? (
          <article className="flex flex-col gap-3 rounded-lg border border-outline-variant bg-surface p-3">
            {result.question ? (
              <div className="rounded-md border border-outline-variant bg-surface-container-low px-3 py-2">
                <p className="m-0 flex items-baseline gap-2 text-[11px] text-on-surface-variant">
                  <span className="font-semibold text-on-surface">질문 · {result.question.author}</span>
                  <time dateTime={result.question.created_at}>
                    {formatDay(result.question.created_at)} {formatClock(result.question.created_at)}
                  </time>
                </p>
                <p className="m-0 mt-1 whitespace-pre-wrap break-words text-body-sm font-body-sm text-on-surface">
                  {messageText(result.question.text)}
                </p>
              </div>
            ) : (
              <p className="m-0 text-[11px] text-on-surface-variant">이 답변 앞에서 질문 메시지를 찾지 못했습니다.</p>
            )}
            <div className="flex gap-3">
              <DiscordAvatar message={result.message} />
              <div className="min-w-0 flex-1">
                <p className="m-0 flex items-baseline gap-2 flex-wrap">
                  <strong className="text-body-sm font-body-sm text-on-surface">{result.message.author}</strong>
                  <span className="px-1.5 py-px rounded bg-primary text-on-primary text-[10px] font-bold leading-4">앱</span>
                  <time className="text-xs text-outline" dateTime={result.message.created_at}>
                    {formatDay(result.message.created_at)} {formatClock(result.message.created_at)}
                  </time>
                </p>
                <div className="mt-1 flex flex-col gap-1.5 text-body-sm font-body-sm leading-6 text-on-surface">
                  {renderDiscordMarkup(messageText(result.message.text))}
                </div>
              </div>
            </div>
          </article>
        ) : null}
      </div>
    </section>
  );
}
