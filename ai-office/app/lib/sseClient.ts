import { bffFetch } from "./bffClient";

export interface ParsedSseEvent {
  id: string | null;
  event: string;
  data: string;
}

export interface SseParser {
  push(chunk: string): void;
  finish(): void;
}

export function createSseParser(onEvent: (event: ParsedSseEvent) => void): SseParser {
  let buffer = "";

  const parseBlock = (block: string) => {
    let id: string | null = null;
    let event = "message";
    const data: string[] = [];
    for (const line of block.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n")) {
      if (!line || line.startsWith(":")) continue;
      const separator = line.indexOf(":");
      const field = separator < 0 ? line : line.slice(0, separator);
      let value = separator < 0 ? "" : line.slice(separator + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "id" && !value.includes("\0")) id = value;
      else if (field === "event") event = value || "message";
      else if (field === "data") data.push(value);
    }
    if (data.length > 0) onEvent({ id, event, data: data.join("\n") });
  };

  const drain = (flush: boolean) => {
    let match = /\r?\n\r?\n/.exec(buffer);
    while (match?.index !== undefined) {
      parseBlock(buffer.slice(0, match.index));
      buffer = buffer.slice(match.index + match[0].length);
      match = /\r?\n\r?\n/.exec(buffer);
    }
    if (flush && buffer.trim()) {
      parseBlock(buffer);
      buffer = "";
    }
  };

  return {
    push(chunk: string) {
      buffer += chunk;
      drain(false);
    },
    finish() {
      drain(true);
    },
  };
}

export function sseReconnectDelay(
  failureCount: number,
  baseMs = 1_000,
  maximumMs = 10_000,
): number {
  return Math.min(maximumMs, baseMs * 2 ** Math.min(Math.max(failureCount - 1, 0), 5));
}

export interface BffSseOptions {
  path(cursor: string | null): string;
  initialCursor?: string;
  onEvent(event: ParsedSseEvent): void;
  onError?(cause: unknown): void;
  reconnectBaseMs?: number;
  reconnectMaxMs?: number;
}

function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const timer = window.setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      window.clearTimeout(timer);
      resolve();
    }, { once: true });
  });
}

/** BFF fetch-stream SSE with cursor recovery and bounded backoff. */
export function subscribeBffSse(options: BffSseOptions): () => void {
  if (typeof window === "undefined") return () => {};
  const controller = new AbortController();
  let cursor = options.initialCursor ?? null;
  let failures = 0;

  const run = async () => {
    while (!controller.signal.aborted) {
      try {
        const decoder = new TextDecoder();
        let receivedEvent = false;
        const response = await bffFetch(options.path(cursor), {
          headers: { Accept: "text/event-stream" },
          signal: controller.signal,
        });
        if (!response.ok || !response.body) throw new Error(`sse_failed_http_${response.status}`);
        const parser = createSseParser((event) => {
          receivedEvent = true;
          if (event.id) cursor = event.id;
          options.onEvent(event);
        });
        const reader = response.body.getReader();
        while (!controller.signal.aborted) {
          const { done, value } = await reader.read();
          if (done) break;
          parser.push(decoder.decode(value, { stream: true }));
        }
        parser.push(decoder.decode());
        parser.finish();
        failures = receivedEvent ? 1 : failures + 1;
        if (!controller.signal.aborted) {
          await delay(
            sseReconnectDelay(failures, options.reconnectBaseMs, options.reconnectMaxMs),
            controller.signal,
          );
        }
      } catch (cause) {
        if (controller.signal.aborted) return;
        options.onError?.(cause);
        failures += 1;
        await delay(
          sseReconnectDelay(failures, options.reconnectBaseMs, options.reconnectMaxMs),
          controller.signal,
        );
      }
    }
  };
  void run();
  return () => controller.abort();
}
