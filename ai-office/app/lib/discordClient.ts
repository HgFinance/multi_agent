/**
 * BFF `/ui/discord/messages`. 부서 Discord 대화 원문을 읽기만 한다.
 *
 * **봇 토큰은 여기 없다.** 토큰이 브라우저 번들에 들어가면 읽기뿐 아니라
 * 발송 권한까지 같이 나가서, 누구나 회사 채널에 부서 봇 이름으로 글을 쓸 수
 * 있다. 그래서 BFF(`apps/api/discord_read.py`)가 토큰을 들고 대신 읽는다.
 *
 * 부서는 `department_code`를 그대로 보낸다 - 짧은 키(`ceo`)로의 변환은 BFF가
 * 소유한다. 여기에 매핑표를 복제하면 부서를 늘릴 때 한쪽만 고쳐진다.
 */

import { bffFetch } from "./bffClient";

export type DiscordMessage = {
  id: string;
  author: string;
  author_id: string;
  is_bot: boolean;
  /** 이 부서 봇이 쓴 글인가. 이름이 아니라 봇 user id로 BFF가 판정한다. */
  is_department_bot: boolean;
  text: string;
  created_at: string;
};

export type DiscordMessagesResponse = {
  schema_version: "ui.discord-messages.v1";
  source: "discord";
  authoritative: false;
  department: string;
  channel_id: string;
  bot_id: string;
  messages: DiscordMessage[];
};

export async function readDiscordMessages(
  department: string,
  limit = 50,
  signal?: AbortSignal,
): Promise<DiscordMessagesResponse> {
  const params = new URLSearchParams({ department, limit: String(limit) });
  const response = await bffFetch(`/ui/discord/messages?${params}`, {
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    // BFF가 detail에 원인을 적어 보낸다(토큰 미설정 503, 권한 없음 502 …).
    // 그대로 화면에 올린다 - "메시지 없음"으로 뭉개면 못 읽은 것과 대화가
    // 없는 것이 같아 보인다.
    const detail = await response
      .json()
      .then((body: { detail?: string }) => body?.detail)
      .catch(() => null);
    throw new Error(detail || `Discord 대화를 불러오지 못했습니다 (HTTP ${response.status})`);
  }
  return (await response.json()) as DiscordMessagesResponse;
}
