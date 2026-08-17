#!/usr/bin/env python3
"""Claude Code CLI 를 Anthropic Messages API 로 감싸는 호스트 프록시.

소유: 재일 (리서치/퀀트)
근거: 재일님 지시 2026-08-03 "클로드 코드 연동" + "지연되도 한번 써보지".

▶ 왜 필요한가 (2026-08-03 실측)
  같은 Max 구독인데 호출 경로에 따라 결과가 갈린다:
    api.anthropic.com 직접 (OAuth 토큰)  -> HTTP 400
      "Third-party apps now draw from your extra usage, not your plan limits."
    claude -p (CLI 헤드리스)             -> 정상 응답
  플랜 등급 문제가 아니라 **호출자가 누구냐**의 문제다. CLI 로 부르면
  Claude Code 자신이 부르는 것이라 플랜 한도가 적용된다.

  그런데 헤르메스는 이 경로를 모른다 - 헤르메스의 `claude_code` 는 provider 가
  아니라 **자격 출처**일 뿐이고, 추론은 결국 api.anthropic.com 직접 호출이라
  전부 서드파티로 잡힌다. 그래서 다리를 놓는다:

    헤르메스(컨테이너) -> host.docker.internal:8787 -> claude -p -> 플랜 한도

  헤르메스 쪽은 ANTHROPIC_BASE_URL 만 이 프록시로 돌리면 된다.

▶ 반드시 호스트에서 돈다
  컨테이너 안에는 claude 실행 파일도 로그인 자격도 없다. 자격을 컨테이너로
  넣는 것은 금지다 - accessToken 이 1시간마다 만료되며 갱신 때 refreshToken 이
  rotation 되고, 컨테이너가 갱신하는 순간 호스트의 대화형 세션이 무효가 된다
  (2026-08-02 auth.json 복사로 실제 겪음). 자격은 호스트에만 둔다.

▶ 지키는 것
  - **동시 실행을 제한한다.** CLI 한 번이 프로세스 하나다. 부서 8개가 동시에
    때리면 호스트가 죽는다. 세마포어로 막고, 넘치면 429 로 정직하게 거절한다.
  - **한도는 사람과 공유다.** 이 프록시가 쓰는 만큼 재일님의 대화형 Claude Code
    가 못 쓴다. 그래서 사용량(usage)을 응답에 그대로 실어 보이게 한다.
  - 실패를 성공으로 위장하지 않는다. CLI 가 죽으면 5xx 를 낸다 - 호출부가
    자기 폴백(무료 모델)으로 떨어질 수 있게.

사용
  python scripts/claude_code_proxy.py            # 자체 점검 (CLI 호출 없음)
  python scripts/claude_code_proxy.py --serve    # 프록시 기동 (기본 8787)
  python scripts/claude_code_proxy.py --probe    # CLI 왕복 1회 실측
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROXY_VERSION = "hermes-claude-code-proxy-v1"
DEFAULT_PORT = int(os.environ.get("CLAUDE_PROXY_PORT", "8787"))
# 동시 CLI 프로세스 상한. 1 이면 직렬 - 지연이 20초대라 2 를 넘기면 호스트가
# 버벅인다. 넘치는 요청은 대기가 아니라 429 다(대기시키면 헤르메스 타임아웃과
# 겹쳐 어느 쪽이 죽었는지 알 수 없게 된다).
MAX_CONCURRENT = int(os.environ.get("CLAUDE_PROXY_CONCURRENCY", "2"))
CLI_TIMEOUT = float(os.environ.get("CLAUDE_PROXY_TIMEOUT", "300"))
# 슬롯이 빌 때까지 기다려 주는 상한.
# ▶ 2026-08-14 실측 정정: 처음엔 "한 호출 30초"로 보고 120초를 줬는데, 그건 짧은
#   프롬프트였다. 실제 공장 호출 35건의 **중앙값이 408초(6.8분), 최대 1,060초**다
#   (`claude -p` 가 단발 완성이 아니라 도구를 쓰는 에이전트 루프이기 때문).
#   슬롯 3개가 그 길이로 물려 있으면 120초 대기로는 못 기다려 429 가 났다(13건).
#   CLI_TIMEOUT(1200초)보다는 짧게 둬서 "대기하다 클라이언트가 먼저 죽는" 상황은
#   여전히 만들지 않는다.
ACQUIRE_TIMEOUT = float(os.environ.get("CLAUDE_PROXY_ACQUIRE_TIMEOUT", "600"))

# 모델 별칭 - 헤르메스가 보내는 이름을 CLI 가 아는 이름으로.
# CLI 는 sonnet/opus/haiku 별칭을 받는다(정확한 날짜 ID 는 버전마다 갈린다).
# ▶ Claude 5 세대 (2026-08-13, 부서장 fable 전환 실측): 별칭표에 없는 ID 는
#   DEFAULT_ALIAS(sonnet)로 강등된다 - claude-fable-5 요청이 조용히 sonnet
#   으로 돌았다(프록시 로그로 발견). 5세대는 **전체 ID 그대로** CLI 에 넘긴다
#   (CLI 는 전체 모델 ID 도 받는다).
MODEL_ALIASES = {
    "claude-sonnet-4-5": "sonnet", "claude-sonnet-4-5-20250929": "sonnet",
    "claude-opus-4-5": "opus", "claude-haiku-4-5": "haiku",
    "sonnet": "sonnet", "opus": "opus", "haiku": "haiku",
    "claude-fable-5": "claude-fable-5", "fable": "claude-fable-5",
    "claude-opus-5": "claude-opus-5", "claude-sonnet-5": "claude-sonnet-5",
}
DEFAULT_ALIAS = "sonnet"

_sem = threading.Semaphore(MAX_CONCURRENT)
_stats = {"calls": 0, "errors": 0, "rejected": 0, "cost_usd": 0.0}


def find_claude_cli() -> str | None:
    """claude 실행 파일 경로. PATH 우선, 없으면 VSCode 확장 번들에서 찾는다.

    확장은 버전 폴더가 여럿일 수 있어 **최신 하나**를 고른다 - 낡은 버전을
    잡으면 `-p` 동작이 다를 수 있다.
    """
    found = shutil.which("claude")
    if found:
        return found
    env = os.environ.get("CLAUDE_CLI_PATH")
    if env and Path(env).exists():
        return env
    ext = Path(os.path.expanduser("~")) / ".vscode" / "extensions"
    cands = sorted(ext.glob("anthropic.claude-code-*/resources/native-binary/claude*"))
    return str(cands[-1]) if cands else None


def flatten_messages(payload: dict) -> str:
    """Anthropic Messages 요청 -> CLI 에 넣을 단일 프롬프트.

    CLI 는 대화 구조가 아니라 프롬프트 하나를 받는다. system 을 맨 앞에 두고
    역할을 표시해 이어 붙인다 - 역할 표시를 지우면 모델이 자기 발화와 사용자
    발화를 구분하지 못한다.
    """
    parts: list[str] = []
    sysmsg = payload.get("system")
    if isinstance(sysmsg, list):      # content block 형식도 온다
        sysmsg = "\n".join(b.get("text", "") for b in sysmsg if isinstance(b, dict))
    if sysmsg:
        parts.append(f"[SYSTEM]\n{sysmsg}")
    for m in payload.get("messages") or []:
        role = str(m.get("role", "user")).upper()
        c = m.get("content")
        if isinstance(c, list):
            c = "\n".join(b.get("text", "") for b in c
                          if isinstance(b, dict) and b.get("type") == "text")
        if c:
            parts.append(f"[{role}]\n{c}")
    return "\n\n".join(parts)


def to_anthropic_response(text: str, meta: dict, model: str) -> dict:
    """CLI 결과 -> Anthropic Messages 응답 모양.

    usage 를 실제 값으로 채운다 - 이 프록시가 쓰는 만큼 사람이 못 쓰므로
    소모가 보여야 한다(추정치로 채우면 그 감시가 무의미해진다).
    """
    u = meta.get("usage") or {}
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": meta.get("stop_reason") or "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(u.get("input_tokens") or 0),
            "output_tokens": int(u.get("output_tokens") or 0),
            "cache_read_input_tokens": int(u.get("cache_read_input_tokens") or 0),
            "cache_creation_input_tokens": int(u.get("cache_creation_input_tokens") or 0),
        },
        # 표준 밖 필드 - 사람이 보라고 싣는다. 소비자는 무시해도 된다.
        "_proxy": {"version": PROXY_VERSION,
                   "cost_usd": meta.get("total_cost_usd"),
                   "duration_api_ms": meta.get("duration_api_ms")},
    }


def to_openai_response(text: str, meta: dict, model: str) -> dict:
    """CLI 결과 -> OpenAI Chat Completions 응답 모양.

    헤르메스 custom provider(transport=openai_chat)가 이 모양을 기대한다.
    usage 는 Anthropic 쪽과 같은 실제 값을 OpenAI 이름으로 옮긴다.
    """
    u = meta.get("usage") or {}
    pt = int(u.get("input_tokens") or 0)
    ct = int(u.get("output_tokens") or 0)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": ("stop" if (meta.get("stop_reason") or "end_turn")
                              == "end_turn" else "length"),
        }],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct,
                  "total_tokens": pt + ct},
        "_proxy": {"version": PROXY_VERSION,
                   "cost_usd": meta.get("total_cost_usd")},
    }


def parse_cli_json(raw: str) -> tuple[str, dict]:
    """CLI --output-format json 출력 -> (본문 텍스트, 메타).

    실측 봉투에는 is_error/stop_reason/usage/total_cost_usd 와 본문이 함께 온다.
    본문 키 이름이 버전마다 다를 수 있어 후보를 순서대로 본다 - 못 찾으면
    **예외를 낸다.** 빈 문자열로 넘기면 호출부가 '모델이 침묵했다'로 오해한다.
    """
    d = json.loads(raw)
    if d.get("is_error"):
        raise RuntimeError(f"CLI 오류 응답: {str(d)[:200]}")
    for key in ("result", "text", "content", "response", "output"):
        v = d.get(key)
        if isinstance(v, str) and v.strip():
            return v, d
        if isinstance(v, list):      # content block 배열
            t = "".join(b.get("text", "") for b in v if isinstance(b, dict))
            if t.strip():
                return t, d
    raise RuntimeError(f"CLI 응답에서 본문을 못 찾았다 - 키: {sorted(d)[:12]}")


def _kill_tree(proc: "subprocess.Popen") -> None:
    """CLI 프로세스 **트리 전체**를 죽인다.

    ▶ 왜 트리인가 (2026-08-14 실측, 프록시 영구 정지 사고)
      `cli` 는 `claude.CMD` 래퍼다. `Popen.kill()` 은 그 래퍼(cmd.exe)만 죽이고
      실제 일을 하는 손자 `claude.exe` 는 살아남는다. 그 손자가 stdout/stderr
      파이프를 그대로 물고 있어서 EOF 가 영원히 안 오고, 뒤이은 파이프 읽기가
      무한정 막힌다. 그 스레드는 세마포어를 든 채 멈추므로 슬롯이 반납되지
      않는다 - 그런 호출이 3 번 쌓이면 프록시가 통째로 죽는다(성공 15 에서
      정지, 오류·거부만 증가, in-flight 프로세스가 타임아웃 300 초를 훌쩍 넘겨
      15 분째 생존).
    """
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=20, check=False)
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass
    # 파이프를 닫아 준다. 손자가 아직 붙어 있어도 우리 쪽 fd 는 놓아야 한다.
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        try:
            if pipe is not None:
                pipe.close()
        except Exception:
            pass


def call_cli(prompt: str, alias: str, *, cli: str) -> tuple[str, dict]:
    """claude -p 한 번. 프롬프트는 **stdin 으로** 넣는다.

    argv 로 넘기면 Windows 명령줄 길이 상한(~32KB)에 걸린다 - Packet 총괄
    프롬프트는 그보다 쉽게 커진다(분석가 6인 readout 이 들어간다).

    ▶ `subprocess.run(timeout=...)` 을 쓰지 않는다. 그 구현은 타임아웃 뒤
      자식을 죽이고 **다시 파이프를 읽어** 종료를 기다리는데, 손자가 살아서
      파이프를 물고 있으면 그 대기가 안 끝난다(`_kill_tree` 주석 참조).
      타임아웃이 있는데도 슬롯이 영영 반납되지 않는 원인이었다.
    """
    proc = subprocess.Popen(
        [cli, "-p", "--model", alias, "--output-format", "json"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    try:
        stdout, stderr = proc.communicate(prompt, timeout=CLI_TIMEOUT)
    except subprocess.TimeoutExpired:
        # 트리를 죽이고 **즉시** 올린다. 여기서 파이프를 더 읽으면 그 순간
        # 다시 무한 대기가 된다 - 그게 이 사고의 정확한 재현 경로다.
        _kill_tree(proc)
        raise
    except BaseException:
        _kill_tree(proc)
        raise
    if proc.returncode != 0:
        raise RuntimeError(
            f"CLI exit={proc.returncode}: {(stderr or stdout)[:300]}")
    return parse_cli_json(stdout)


class Handler(BaseHTTPRequestHandler):
    server_version = PROXY_VERSION

    def log_message(self, *a):    # 기본 접근 로그는 시끄럽다 - 아래서 직접 찍는다
        pass

    def _json(self, code: int, body: dict):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _sse(self, text: str, meta: dict, model: str, *, openai_style: bool):
        """완성된 답을 SSE 한 덩어리로 흘린다.

        헤르메스가 스트림을 요구하므로 형식만 맞춘다. 마지막에 finish_reason 과
        [DONE] 을 반드시 보낸다 - 그게 없으면 소비자가 '빈 스트림'으로 본다.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def emit(obj):
            self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()

        if openai_style:
            cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
            base = {"id": cid, "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": model}
            emit({**base, "choices": [{"index": 0, "delta": {"role": "assistant"},
                                       "finish_reason": None}]})
            emit({**base, "choices": [{"index": 0, "delta": {"content": text},
                                       "finish_reason": None}]})
            u = meta.get("usage") or {}
            emit({**base,
                  "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": int(u.get("input_tokens") or 0),
                            "completion_tokens": int(u.get("output_tokens") or 0),
                            "total_tokens": int(u.get("input_tokens") or 0)
                                            + int(u.get("output_tokens") or 0)}})
        else:
            msg = to_anthropic_response(text, meta, model)
            emit({"type": "message_start",
                  "message": {**msg, "content": [],
                              "usage": {"input_tokens": msg["usage"]["input_tokens"],
                                        "output_tokens": 0}}})
            emit({"type": "content_block_start", "index": 0,
                  "content_block": {"type": "text", "text": ""}})
            emit({"type": "content_block_delta", "index": 0,
                  "delta": {"type": "text_delta", "text": text}})
            emit({"type": "content_block_stop", "index": 0})
            emit({"type": "message_delta",
                  "delta": {"stop_reason": msg["stop_reason"], "stop_sequence": None},
                  "usage": {"output_tokens": msg["usage"]["output_tokens"]}})
            emit({"type": "message_stop"})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", ""):
            cli = find_claude_cli()
            self._json(200 if cli else 503, {
                "version": PROXY_VERSION, "cli": cli,
                "concurrency": MAX_CONCURRENT, "stats": dict(_stats)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.rstrip("/")
        # 두 방언을 모두 받는다:
        #   /v1/messages          Anthropic Messages (헤르메스 anthropic provider)
        #   /v1/chat/completions  OpenAI Chat (헤르메스 custom provider, transport
        #                         =openai_chat). **실측 2026-08-03: 헤르메스의
        #                         anthropic provider 는 base_url 을 무시하고
        #                         api.anthropic.com 으로 직행한다**(프로필 키·
        #                         환경변수 둘 다 무시). 그래서 custom 경로가
        #                         실제로 쓰이는 길이다.
        # ▶ /anthropic 접두 (2026-08-13 정정): 지금 이미지의 hermes 는
        #   `_anthropic_base_url_override_ok` 가 **경로가 /anthropic 으로 끝나는
        #   오버라이드만** 인정하고, 아니면 조용히 api.anthropic.com 직행으로
        #   폴백한다(부서장 fable 전환 때 400 으로 실측). 그 관례로 들어오는
        #   요청의 접두를 벗겨 같은 핸들러로 받는다 -
        #   base_url=http://host.docker.internal:8787/anthropic 이 정본이다.
        if path.startswith("/anthropic/") or path == "/anthropic":
            path = path[len("/anthropic"):] or "/"
        openai_style = path.endswith("/v1/chat/completions")
        if not (openai_style or path.endswith("/v1/messages")):
            self._json(404, {"type": "error",
                             "error": {"type": "not_found_error",
                                       "message": f"unknown path {self.path}"}})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) or b"{}"
            # UTF-8 이 아닌 바이트가 섞여도 요청 전체를 버리지 않는다 - 한 글자
            # 깨지는 것보다 400 으로 통째로 죽는 쪽이 나쁘다(실측: Windows 셸이
            # cp949 로 보낸 한글). 정상 클라이언트는 이 경로를 안 탄다.
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as e:  # noqa: BLE001
            self._json(400, {"type": "error",
                             "error": {"type": "invalid_request_error",
                                       "message": f"bad body: {e}"}})
            return

        cli = find_claude_cli()
        if not cli:
            self._json(503, {"type": "error",
                             "error": {"type": "api_error",
                                       "message": "claude CLI 를 못 찾았다"}})
            return

        model = str(payload.get("model") or DEFAULT_ALIAS)
        alias = MODEL_ALIASES.get(model, DEFAULT_ALIAS)
        prompt = flatten_messages(payload)
        if not prompt.strip():
            self._json(400, {"type": "error",
                             "error": {"type": "invalid_request_error",
                                       "message": "빈 프롬프트"}})
            return

        # ▶ 즉시 거절이 아니라 **유한 대기** 후 거절한다 (2026-08-14 정정)
        #   원래는 "대기하면 헤르메스 타임아웃과 겹쳐 어느 쪽이 죽었는지 모른다"는
        #   이유로 즉시 429 였다. 그런데 실측에서 그 즉시성이 카드를 죽이고 있었다:
        #     · 한 호출은 30~36초 걸린다(단발, turns=1 실측).
        #     · 헤르메스는 429 를 받으면 3회만 재시도하고 총 대기가 ~15초다.
        #     → 슬롯이 비는 데 30초 걸리는데 15초만 기다리므로, 경합이 조금만
        #       있어도 카드가 crash 2회로 blocked 에 굳는다. 실제로 blocked 183 장
        #       중 상당수가 이 경로였고 429 는 04:25 에도 재발했다.
        #   그래서 클라이언트 타임아웃보다 충분히 짧은 유한 대기로 흡수한다.
        #   대기해도 못 얻으면 그때 429 - "누가 죽었는지 모른다"는 문제는
        #   대기 시간을 로그로 남겨 해소한다.
        wait_started = time.time()
        if not _sem.acquire(timeout=ACQUIRE_TIMEOUT):
            _stats["rejected"] += 1
            print(f"  [{time.strftime('%H:%M:%S')}] 슬롯 대기 {ACQUIRE_TIMEOUT:.0f}초 "
                  f"초과 - 429 (상한 {MAX_CONCURRENT})", file=sys.stderr, flush=True)
            self._json(429, {"type": "error",
                             "error": {"type": "rate_limit_error",
                                       "message": f"동시 실행 상한 {MAX_CONCURRENT} "
                                                  f"- {ACQUIRE_TIMEOUT:.0f}초 대기 후에도 "
                                                  f"슬롯 없음"}})
            return
        waited = time.time() - wait_started
        if waited > 1.0:
            _stats["waited_total_s"] = _stats.get("waited_total_s", 0.0) + waited
            _stats["waited_calls"] = _stats.get("waited_calls", 0) + 1
        t0 = time.time()
        try:
            text, meta = call_cli(prompt, alias, cli=cli)
            _stats["calls"] += 1
            _stats["cost_usd"] += float(meta.get("total_cost_usd") or 0.0)
            print(f"  [{time.strftime('%H:%M:%S')}] {alias} "
                  f"{time.time() - t0:.1f}s in={len(prompt)}자 "
                  f"out={len(text)}자 누적비용=${_stats['cost_usd']:.4f}",
                  flush=True)
            if payload.get("stream"):
                # 헤르메스는 SSE 를 기대한다(실측 2026-08-03: 평문 JSON 을 주면
                # "empty stream with no finish_reason" 로 3회 재시도 후 실패).
                # CLI 는 스트리밍이 아니므로 **완성된 답을 한 덩어리로** 보낸다 -
                # 토큰 단위로 쪼개는 시늉을 하지 않는다(가짜 점진 출력은 지연만
                # 늘리고 얻는 게 없다).
                self._sse(text, meta, model, openai_style=openai_style)
            else:
                self._json(200, to_openai_response(text, meta, model) if openai_style
                           else to_anthropic_response(text, meta, model))
        except subprocess.TimeoutExpired:
            _stats["errors"] += 1
            self._json(504, {"type": "error",
                             "error": {"type": "timeout_error",
                                       "message": f"CLI {CLI_TIMEOUT}초 초과"}})
        except Exception as e:  # noqa: BLE001 - 실패를 성공으로 위장하지 않는다
            _stats["errors"] += 1
            print(f"  ⚠ 실패 {type(e).__name__}: {str(e)[:200]}",
                  file=sys.stderr, flush=True)
            self._json(502, {"type": "error",
                             "error": {"type": "api_error",
                                       "message": f"{type(e).__name__}: {e}"[:400]}})
        finally:
            _sem.release()


def serve(port: int = DEFAULT_PORT) -> int:
    cli = find_claude_cli()
    print(f"{PROXY_VERSION}")
    print(f"  CLI      : {cli or '못 찾음 ← 기동해도 503 이 난다'}")
    print(f"  포트     : {port} (컨테이너는 host.docker.internal:{port})")
    print(f"  동시실행 : {MAX_CONCURRENT} / 타임아웃 {CLI_TIMEOUT:.0f}초")
    print("  ※ 한도는 대화형 Claude Code 와 공유다 - 많이 쓰면 사람이 막힌다")
    # 0.0.0.0 으로 열어야 컨테이너에서 닿는다. 사설망 전제(포트포워딩 금지).
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    return 0


def probe() -> int:
    cli = find_claude_cli()
    if not cli:
        print("claude CLI 를 못 찾았다")
        return 1
    t0 = time.time()
    text, meta = call_cli("한 단어로만 답해라: 대한민국의 수도는?", "sonnet", cli=cli)
    print(f"{PROXY_VERSION} 실측")
    print(f"  CLI    : {cli}")
    print(f"  응답   : {text.strip()[:60]!r}")
    print(f"  소요   : {time.time() - t0:.1f}초 (API {meta.get('duration_api_ms')}ms)")
    print(f"  usage  : {meta.get('usage', {}).get('input_tokens')} in / "
          f"{meta.get('usage', {}).get('output_tokens')} out")
    print(f"  비용   : ${meta.get('total_cost_usd')}")
    return 0


# ---------------------------------------------------------------------------
# 자체 점검 - CLI 호출·네트워크 없음
# ---------------------------------------------------------------------------

def _check_flatten():
    p = flatten_messages({"system": "너는 분석가다",
                          "messages": [{"role": "user", "content": "안녕"},
                                       {"role": "assistant", "content": "네"},
                                       {"role": "user", "content": "분석해"}]})
    assert p.startswith("[SYSTEM]\n너는 분석가다"), p[:40]
    assert "[USER]\n안녕" in p and "[ASSISTANT]\n네" in p and p.endswith("분석해")
    # content block 배열도 받는다
    p2 = flatten_messages({"system": [{"type": "text", "text": "S"}],
                           "messages": [{"role": "user",
                                         "content": [{"type": "text", "text": "U"}]}]})
    assert "[SYSTEM]\nS" in p2 and "[USER]\nU" in p2, p2
    assert flatten_messages({}) == "", "빈 요청은 빈 프롬프트"
    print("  프롬프트 평탄화          OK")


def _check_parse():
    raw = json.dumps({"is_error": False, "result": "파랑", "stop_reason": "end_turn",
                      "usage": {"input_tokens": 2, "output_tokens": 4},
                      "total_cost_usd": 0.06})
    text, meta = parse_cli_json(raw)
    assert text == "파랑" and meta["usage"]["output_tokens"] == 4
    # 오류 응답은 예외 - 빈 텍스트로 넘기지 않는다
    for bad, why in ((json.dumps({"is_error": True, "result": "x"}), "is_error"),
                     (json.dumps({"usage": {}}), "본문 없음"),
                     (json.dumps({"result": "   "}), "공백 본문")):
        try:
            parse_cli_json(bad)
            raise AssertionError(f"{why} 인데 통과했다")
        except RuntimeError:
            pass
    print("  CLI 응답 파싱            OK")


def _check_response_shape():
    r = to_anthropic_response("답", {"usage": {"input_tokens": 10, "output_tokens": 3},
                                     "stop_reason": "end_turn",
                                     "total_cost_usd": 0.01}, "claude-sonnet-4-5")
    for k in ("id", "type", "role", "model", "content", "stop_reason", "usage"):
        assert k in r, k
    assert r["type"] == "message" and r["role"] == "assistant"
    assert r["content"][0]["text"] == "답"
    assert r["usage"]["input_tokens"] == 10, "usage 를 추정치로 채우면 감시가 무의미하다"
    # 결측 usage 도 0 으로 채워 스키마를 지킨다(키 자체가 빠지면 소비자가 죽는다)
    r2 = to_anthropic_response("x", {}, "m")
    assert r2["usage"]["output_tokens"] == 0
    print("  Anthropic 응답 계약      OK")


def _check_openai_shape():
    """헤르메스 custom provider(transport=openai_chat)가 기대하는 모양."""
    r = to_openai_response("답", {"usage": {"input_tokens": 7, "output_tokens": 2},
                                  "stop_reason": "end_turn"}, "claude-sonnet-4-5")
    assert r["object"] == "chat.completion"
    assert r["choices"][0]["message"]["content"] == "답"
    assert r["choices"][0]["message"]["role"] == "assistant"
    assert r["choices"][0]["finish_reason"] == "stop"
    assert r["usage"] == {"prompt_tokens": 7, "completion_tokens": 2,
                          "total_tokens": 9}, r["usage"]
    # OpenAI 요청은 system 을 별도 필드가 아니라 messages 의 role 로 준다
    p = flatten_messages({"messages": [{"role": "system", "content": "S"},
                                       {"role": "user", "content": "U"}]})
    assert "[SYSTEM]\nS" in p and "[USER]\nU" in p, p
    print("  OpenAI 응답 계약         OK")


def _check_model_alias():
    assert MODEL_ALIASES["claude-sonnet-4-5"] == "sonnet"
    assert MODEL_ALIASES.get("모르는모델", DEFAULT_ALIAS) == "sonnet"
    # ▶ 5세대는 강등 금지 (2026-08-13 실측: claude-fable-5 가 조용히 sonnet
    #   으로 돌았다). 부서장 전환의 전제라 여기서 고정한다.
    assert MODEL_ALIASES["claude-fable-5"] == "claude-fable-5"
    assert MODEL_ALIASES["fable"] == "claude-fable-5"
    assert set(MODEL_ALIASES.values()) <= {
        "sonnet", "opus", "haiku",
        "claude-fable-5", "claude-opus-5", "claude-sonnet-5"}
    print("  모델 별칭                OK")


def _check_cli_discovery():
    cli = find_claude_cli()
    # 이 호스트에는 VSCode 확장 번들이 있다(실측) - 못 찾으면 배선이 깨진 것
    assert cli and Path(cli).exists(), f"claude CLI 를 못 찾았다: {cli}"
    print(f"  CLI 탐색                 OK ({Path(cli).parent.parent.parent.name})")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--serve" in sys.argv:
        raise SystemExit(serve())
    if "--probe" in sys.argv:
        raise SystemExit(probe())

    print(f"{PROXY_VERSION} 자체 점검 (CLI 호출·네트워크 없음)")
    _check_flatten()
    _check_parse()
    _check_response_shape()
    _check_openai_shape()
    _check_model_alias()
    _check_cli_discovery()
    print("프록시 6개 영역 통과. 기동은 --serve, 실측은 --probe")
